"""Persistência e transporte do consumidor de respostas v1.

- DjangoEventosProcessados: primeira vez True, repetida False;
- SQSConsumidorRespostas: receive_message com MessageAttributeNames do
  W3C, delete das encerradas, poison apagada, erro de infra sem delete.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.test import TestCase

from os_service.application.saga import dto
from os_service.application.saga.consumidor import ConsumidorRespostasSaga
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    InMemorySagaPublisherV1,
)
from os_service.infrastructure.saga_consumidor import (
    ATRIBUTOS_DE_TRACE,
    DjangoEventosProcessados,
    SQSConsumidorRespostas,
)
from os_service.models import ProcessedEvent

TRACEPARENT_FIXO = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
ATRIBUTOS_TRACE = {
    "traceparent": {"DataType": "String", "StringValue": TRACEPARENT_FIXO}
}


def _corpo(tipo, saga_id, os_id, message_id, payload=None):
    msg = dto.MensagemSaga(
        message_id=message_id,
        type=tipo,
        saga_id=saga_id,
        os_id=os_id,
        request_id="req-1",
        occurred_at=datetime.now(timezone.utc),
        payload=payload or {},
    )
    return json.dumps(dto.envelope(msg), ensure_ascii=False)


def _orquestrador():
    return SagaOrquestradorV1(
        state_repository=DjangoSagaStateRepositoryV1(),
        publisher=InMemorySagaPublisherV1(),
    )


class DjangoEventosProcessadosTestCase(TestCase):
    def test_primeira_vez_true_repetida_false(self):
        started = _orquestrador().iniciar(7, "req-1", {})
        msg = dto.MensagemSaga(
            message_id="09c2b9c5-1e77-4a5b-9a8d-2b0d4a9b5f11",
            type="OrcamentoGerado",
            saga_id=started,
            os_id=7,
            request_id="req-1",
            occurred_at=datetime.now(timezone.utc),
            payload={"orcamentoId": 9},
        )
        registro = DjangoEventosProcessados()

        self.assertTrue(registro.marcar(msg))
        self.assertFalse(registro.marcar(msg))
        self.assertTrue(
            ProcessedEvent.objects.filter(event_id=msg.message_id).exists()
        )


class SQSConsumidorRespostasTestCase(TestCase):
    def setUp(self):
        self.orquestrador = _orquestrador()
        self.saga_id = self.orquestrador.iniciar(7, "req-1", {})
        self.cliente = MagicMock()
        self.sqs = SQSConsumidorRespostas(
            queue_url="https://sqs/respostas",
            client=self.cliente,
            consumidor=ConsumidorRespostasSaga(
                orquestrador=self.orquestrador,
                eventos_processados=DjangoEventosProcessados(),
            ),
        )

    def test_receive_message_pede_atributos_do_trace(self):
        self.cliente.receive_message.return_value = {}
        resumo = self.sqs.recebe_onda()
        kwargs = self.cliente.receive_message.call_args.kwargs
        self.assertEqual(kwargs["MessageAttributeNames"], ATRIBUTOS_DE_TRACE)
        self.assertEqual(kwargs["MaxNumberOfMessages"], 10)
        self.assertEqual(resumo, {"recebidos": 0, "processados": 0,
                                  "ignoradas": 0, "duplicados": 0,
                                  "invalidas": 0, "erros": 0})

    def test_mensagem_processada_e_apagada(self):
        self._fila_com("m-1", _corpo("OrcamentoGerado", self.saga_id, 7,
                                     str(uuid.uuid4()), {"orcamentoId": 9}))
        resumo = self.sqs.recebe_onda()
        self.assertEqual(resumo["processados"], 1)
        self.cliente.delete_message.assert_called_once_with(
            QueueUrl="https://sqs/respostas", ReceiptHandle="rh-m-1"
        )

    def test_mensagem_duplicada_contada_e_apagada(self):
        cross = str(uuid.uuid4())
        corpo = _corpo("OrcamentoGerado", self.saga_id, 7, cross,
                       {"orcamentoId": 9})
        self._fila_com("m-1", corpo)
        self.sqs.recebe_onda()
        self._fila_com("m-2", corpo)
        resumo = self.sqs.recebe_onda()
        self.assertEqual(resumo["duplicados"], 1)
        self.assertEqual(resumo["processados"], 0)
        self.assertEqual(self.cliente.delete_message.call_count, 2)

    def test_body_lixo_e_poison_message_apagada(self):
        self._fila_com("m-3", "definitivamente nao sou json")
        resumo = self.sqs.recebe_onda()
        self.assertEqual(resumo["invalidas"], 1)
        self.cliente.delete_message.assert_called_once()

    def test_erro_de_processamento_nao_apaga_e_redeliveria(self):
        self.orquestrador.processar = MagicMock(
            side_effect=RuntimeError("conexão caiu")
        )
        self._fila_com("m-4", _corpo("OrcamentoGerado", self.saga_id, 7,
                                     str(uuid.uuid4())))
        resumo = self.sqs.recebe_onda()
        self.assertEqual(resumo["erros"], 1)
        self.cliente.delete_message.assert_not_called()

    def test_sem_fila_configurada_e_runtimeerror(self):
        vazio = SQSConsumidorRespostas(
            queue_url="", client=self.cliente,
            consumidor=self.sqs.consumidor,
        )
        with self.assertRaises(RuntimeError):
            vazio.recebe_onda()
        self.cliente.receive_message.assert_not_called()

    def _fila_com(self, receipt, corpo):
        self.cliente.receive_message.return_value = {
            "Messages": [{
                "ReceiptHandle": f"rh-{receipt}",
                "Body": corpo,
                "MessageAttributes": ATRIBUTOS_TRACE,
            }]
        }
