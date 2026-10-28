"""Fatia C — persistência e transporte do contrato v1.

- SagaInstance.saga_id (UUID, unique) + estado da máquina; os_id indexado
  SEM unique (uma OS pode ter várias sagas — questão 2 do contrato).
- DjangoSagaStateRepositoryV1: criar/get/salvar_estado/salvar_motivo.
- SQSSagaPublisherV1: envelope do contrato (messageId/sagaId/.../payload)
  com MessageDeduplicationId = messageId.
- build_saga_orquestrador_v1: fiação do orquestrador v1.
"""
import json
import uuid

from django.db import transaction as db_transaction
from django.test import TestCase

from os_service.application.saga import dto
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    SQSSagaPublisherV1,
)
from os_service.models import SagaInstance

ENVELOPE_KEYS = {
    "messageId", "type", "version", "sagaId", "osId", "causationId",
    "requestId", "occurredAt", "payload",
}


class SagaInstanceSagaIdTestCase(TestCase):
    """Model: saga_id único, os_id reutilizável."""

    def test_cria_duas_sagas_para_a_mesma_os(self):
        SagaInstance.objects.create(
            saga_id=str(uuid.uuid4()), os_id=7, estado="INICIADA",
        )
        segunda = SagaInstance.objects.create(
            saga_id=str(uuid.uuid4()), os_id=7, estado="INICIADA",
        )
        self.assertEqual(
            SagaInstance.objects.filter(os_id=7).count(), 2
        )
        self.assertTrue(segunda.saga_id)

    def test_saga_id_unico(self):
        mesmo = str(uuid.uuid4())
        SagaInstance.objects.create(saga_id=mesmo, os_id=1, estado="INICIADA")
        with self.assertRaises(Exception):
            SagaInstance.objects.create(saga_id=mesmo, os_id=2, estado="INICIADA")


class DjangoSagaStateRepositoryV1TestCase(TestCase):
    def setUp(self):
        self.repo = DjangoSagaStateRepositoryV1()

    def test_ciclo_de_vida_completo(self):
        saga_id = str(uuid.uuid4())
        self.repo.criar(saga_id, os_id=9, estado_inicial=dto.EstadoSaga.INICIADA)

        estado = self.repo.get(saga_id)
        self.assertEqual(estado.estado, dto.EstadoSaga.INICIADA)
        self.assertEqual(estado.os_id, 9)
        self.assertEqual(estado.status, dto.StatusSaga.EM_ANDAMENTO)

        self.repo.salvar_estado(saga_id, dto.EstadoSaga.ORCAMENTO_SOLICITADO)
        estado = self.repo.get(saga_id)
        self.assertEqual(estado.estado, dto.EstadoSaga.ORCAMENTO_SOLICITADO)

        self.repo.salvar_motivo(saga_id, "EXECUCAO_FALHOU")
        estado = self.repo.get(saga_id)
        self.assertEqual(estado.motivo, "EXECUCAO_FALHOU")

        self.repo.salvar_estado(saga_id, dto.EstadoSaga.CANCELADA)
        estado = self.repo.get(saga_id)
        self.assertEqual(estado.status, dto.StatusSaga.FALHA)

    def test_get_inexistente_retorna_none(self):
        self.assertIsNone(self.repo.get(str(uuid.uuid4())))


class SQSSagaPublisherV1TestCase(TestCase):
    def test_publish_usa_envelope_do_contrato_e_dedup_id(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.send_message.return_value = {"MessageId": "m-1"}
        publisher = SQSSagaPublisherV1(
            queue_url="https://sqs.sa-east-1.amazonaws.com/123/saga.fifo",
            client=client,
        )
        msg = dto.MensagemSaga(
            message_id=str(uuid.uuid4()),
            type="GerarOrcamento",
            saga_id=str(uuid.uuid4()),
            os_id=101,
            request_id="req-1",
            occurred_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            causation_id=None,
            payload={"clienteId": 7},
        )

        publisher.publish(msg)

        _, kwargs = client.send_message.call_args
        self.assertEqual(kwargs["MessageGroupId"], "os-101")
        self.assertEqual(kwargs["MessageDeduplicationId"], msg.message_id)
        body = json.loads(kwargs["MessageBody"])
        self.assertEqual(set(body.keys()), ENVELOPE_KEYS)
        self.assertEqual(body["sagaId"], msg.saga_id)
        self.assertEqual(body["payload"], {"clienteId": 7})


    def test_publish_leva_traceparent_w3c_nos_message_attributes(self):
        """Trace atravessa a fila: traceparent vai em MessageAttributes (contrato v1)."""
        from unittest.mock import MagicMock

        from oficina_saga import rastreio

        client = MagicMock()
        client.send_message.return_value = {"MessageId": "m-2"}
        publisher = SQSSagaPublisherV1(
            queue_url="https://sqs.sa-east-1.amazonaws.com/123/saga.fifo",
            client=client,
        )
        msg = dto.MensagemSaga(
            message_id=str(uuid.uuid4()),
            type="GerarOrcamento",
            saga_id=str(uuid.uuid4()),
            os_id=102,
            request_id="req-2",
            occurred_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            causation_id=None,
            payload={"clienteId": 7},
        )

        publisher.publish(msg)

        _, kwargs = client.send_message.call_args
        attrs = kwargs["MessageAttributes"]
        self.assertEqual(attrs["traceparent"]["DataType"], "String")
        self.assertIsNotNone(
            rastreio.parse_traceparent(attrs["traceparent"]["StringValue"])
        )
        # O header proprietário do agente não vai para a fila.
        self.assertNotIn("newrelic", attrs)
        # Contexto de transporte fica fora do corpo (envelope fechado).
        body = json.loads(kwargs["MessageBody"])
        self.assertNotIn("traceparent", body)
