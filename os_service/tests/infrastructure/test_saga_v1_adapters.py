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
from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.db import transaction as db_transaction
from django.test import TestCase
from oficina_saga import rastreio

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
            billing_queue_url="https://sqs.sa-east-1.amazonaws.com/123/oficina-billing-comandos.fifo",
            execution_queue_url="https://sqs.sa-east-1.amazonaws.com/123/oficina-execucao-comandos.fifo",
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
        self.assertEqual(
            kwargs["QueueUrl"],
            "https://sqs.sa-east-1.amazonaws.com/123/oficina-billing-comandos.fifo",
        )
        self.assertEqual(kwargs["MessageGroupId"], msg.saga_id)
        self.assertEqual(kwargs["MessageDeduplicationId"], msg.message_id)
        body = json.loads(kwargs["MessageBody"])
        self.assertEqual(set(body.keys()), ENVELOPE_KEYS)
        self.assertEqual(body["sagaId"], msg.saga_id)
        self.assertEqual(body["payload"], {"clienteId": 7})


    def test_publish_leva_traceparent_w3c_nos_message_attributes(self):
        """Trace atravessa a fila: traceparent vai em MessageAttributes (contrato v1)."""
        client = MagicMock()
        client.send_message.return_value = {"MessageId": "m-2"}
        publisher = SQSSagaPublisherV1(
            billing_queue_url="https://sqs.sa-east-1.amazonaws.com/123/oficina-billing-comandos.fifo",
            execution_queue_url="https://sqs.sa-east-1.amazonaws.com/123/oficina-execucao-comandos.fifo",
            client=client,
        )
        msg = dto.MensagemSaga(
            message_id=str(uuid.uuid4()),
            type="GerarOrcamento",
            saga_id=str(uuid.uuid4()),
            os_id=102,
            request_id="req-2",
            occurred_at=datetime.now(timezone.utc),
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


COMANDOS_EXECUCAO = "https://sqs.sa-east-1.amazonaws.com/123/oficina-execucao-comandos.fifo"
COMANDOS_BILLING = "https://sqs.sa-east-1.amazonaws.com/123/oficina-billing-comandos.fifo"


def _msg(tipo: str) -> dto.MensagemSaga:
    return dto.MensagemSaga(
        message_id=str(uuid.uuid4()),
        type=tipo,
        saga_id=str(uuid.uuid4()),
        os_id=103,
        request_id="req-3",
        occurred_at=datetime.now(timezone.utc),
        causation_id=None,
        payload={},
    )


class RotreamentoPorDestinatarioTestCase(TestCase):
    """Issue #17: fila única fazia todo comando ir para o mesmo destino."""

    TIPOS_BILLING = ("GerarOrcamento", "SolicitarPagamento", "CancelarOrcamento", "EstornarPagamento")
    TIPOS_EXECUCAO = ("IniciarExecucao", "CancelarExecucao")

    def _publisher(self, client):
        return SQSSagaPublisherV1(
            billing_queue_url=COMANDOS_BILLING,
            execution_queue_url=COMANDOS_EXECUCAO,
            client=client,
        )

    def test_comandos_da_execucao_vao_para_a_fila_da_execucao(self):
        client = MagicMock()
        client.send_message.return_value = {"MessageId": "m-3"}
        publisher = self._publisher(client)
        for tipo in self.TIPOS_EXECUCAO:
            publisher.publish(_msg(tipo))
            _, kwargs = client.send_message.call_args
            self.assertEqual(kwargs["QueueUrl"], COMANDOS_EXECUCAO, tipo)

    def test_comandos_do_billing_vao_para_a_fila_do_billing(self):
        client = MagicMock()
        client.send_message.return_value = {"MessageId": "m-4"}
        publisher = self._publisher(client)
        for tipo in self.TIPOS_BILLING:
            publisher.publish(_msg(tipo))
            _, kwargs = client.send_message.call_args
            self.assertEqual(kwargs["QueueUrl"], COMANDOS_BILLING, tipo)

    def test_todos_os_comandos_do_contrato_tem_destinatario(self):
        """Se um tipo novo entrar sem rota, o publish levanta (não silencia)."""
        client = MagicMock()
        publisher = self._publisher(client)
        with self.assertRaises(ValueError):
            publisher.publish(_msg("MensagemDesconhecida"))

    def test_fila_sem_url_configurada_levanta_runtime(self):
        client = MagicMock()
        publisher = SQSSagaPublisherV1(
            billing_queue_url="",
            execution_queue_url=COMANDOS_EXECUCAO,
            client=client,
        )
        with self.assertRaises(RuntimeError):
            publisher.publish(_msg("GerarOrcamento"))


class FiacaoDoOrquestradorTestCase(TestCase):
    """build_saga_orquestrador_v1: SQS com as duas filas; InMemory sem elas."""

    def _com_settings(self, **kwargs):
        from django.test import override_settings

        return override_settings(**kwargs)

    def test_build_com_as_duas_filas_monta_publisher_sqs(self):
        with self._com_settings(
            SAGA_BILLING_QUEUE_URL=COMANDOS_BILLING,
            SAGA_EXECUTION_QUEUE_URL=COMANDOS_EXECUCAO,
        ):
            from unittest.mock import patch

            from os_service.infrastructure.saga_adapters import build_saga_orquestrador_v1

            with patch("os_service.infrastructure.saga_adapters.boto3") as mock_boto:
                orquestrador = build_saga_orquestrador_v1()
                publisher = orquestrador.publisher
                mock_boto.client.assert_called_once_with("sqs")

        self.assertIsInstance(publisher, SQSSagaPublisherV1)
        self.assertEqual(publisher.fila_de("GerarOrcamento"), COMANDOS_BILLING)
        self.assertEqual(publisher.fila_de("IniciarExecucao"), COMANDOS_EXECUCAO)

    def test_build_sem_filas_monta_publisher_em_memoria(self):
        from os_service.infrastructure.saga_adapters import (
            InMemorySagaPublisherV1,
            build_saga_orquestrador_v1,
        )
        from unittest.mock import patch

        with self._com_settings(SAGA_BILLING_QUEUE_URL="", SAGA_EXECUTION_QUEUE_URL=""):
            orquestrador = build_saga_orquestrador_v1()
        self.assertIsInstance(orquestrador.publisher, InMemorySagaPublisherV1)
