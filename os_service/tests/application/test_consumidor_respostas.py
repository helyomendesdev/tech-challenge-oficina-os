"""Consumidor de respostas da saga v1: parse, dedupe, delegação e trace.

Fluxo testado com o orquestrador real (repo Django + publisher em memória):
- envelope camelCase → transição na máquina;
- duplicado por messageId não reprocessa (at-least-once da fila);
- saga ausente → ignorada (sem levantar);
- body inválido → ValueError; trace W3C aceito dos MessageAttributes.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.test import TestCase
from oficina_saga import rastreio

from os_service.application.saga import dto
from os_service.application.saga.consumidor import ConsumidorRespostasSaga
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    InMemorySagaPublisherV1,
)
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1

TRACEPARENT_FIXO = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _orquestrador():
    return SagaOrquestradorV1(
        state_repository=DjangoSagaStateRepositoryV1(),
        publisher=InMemorySagaPublisherV1(),
    )


def _resposta(message_id, tipo, saga_id, os_id, payload=None, causation=None):
    msg = dto.MensagemSaga(
        message_id=message_id,
        type=tipo,
        saga_id=saga_id,
        os_id=os_id,
        request_id="req-1",
        occurred_at=datetime.now(timezone.utc),
        causation_id=causation,
        payload=payload or {},
    )
    return json.dumps(dto.envelope(msg), ensure_ascii=False)


class ConsumidorRespostasSagaTestCase(TestCase):
    def setUp(self):
        self.orquestrador = _orquestrador()
        self.consumidor = ConsumidorRespostasSaga(
            orquestrador=self.orquestrador
        )

    def test_resposta_valida_avanca_a_saga_e_publica_comando(self):
        saga_id = self.orquestrador.iniciar(5, "req-1", {"clienteId": 1})
        body = _resposta(
            "m-1", "OrcamentoGerado", saga_id, 5, {"orcamentoId": 88}
        )
        saida = self.consumidor.processar(body)

        estado = self.orquestrador.state_repository.get(saga_id)
        self.assertEqual(saida["resultado"], "processado")
        self.assertEqual(saida["estado"], "AGUARDANDO_APROVACAO")
        self.assertEqual(estado.estado, dto.EstadoSaga.AGUARDANDO_APROVACAO)
        publicados = self.orquestrador.publisher.publicados
        self.assertEqual(
            [m.type for m in publicados], ["GerarOrcamento"]
        )  # aprovação ainda não veio: nada a publicar por enquanto

    def test_dedupe_marca_so_a_primeira_vez(self):
        from os_service.models import ProcessedEvent

        saga_id = self.orquestrador.iniciar(5, "req-1", {})
        body = _resposta("m-dup", "OrcamentoGerado", saga_id, 5,
                         {"orcamentoId": 88})
        consumidor = ConsumidorRespostasSaga(
            orquestrador=self.orquestrador,
            eventos_processados=_DedupeFake(),
        )
        primeira = consumidor.processar(body)
        segunda = consumidor.processar(body)

        self.assertEqual(primeira["resultado"], "processado")
        self.assertEqual(segunda["resultado"], "duplicado")
        # dedupe impede o reprocesso: publicou só o GerarOrcamento da abertura
        self.assertEqual(
            [m.type for m in self.orquestrador.publisher.publicados],
            ["GerarOrcamento"],
        )
        self.assertEqual(ProcessedEvent.objects.filter(os_id=5).count(), 0)

    def test_saga_desconhecida_ignorada_sem_levantar(self):
        saga_fantasma = str(uuid.uuid4())  # UUID válido, saga inexistente
        body = _resposta("m-9", "OrcamentoGerado", saga_fantasma, 5)
        saida = self.consumidor.processar(body)

        self.assertEqual(saida["resultado"], "ignorada")
        self.assertIsNone(saida["estado"])

    def test_body_nao_json_e_valueerror(self):
        with self.assertRaises(ValueError):
            self.consumidor.processar("definitivamente nao json")

    def test_body_json_sem_campos_e_valueerror(self):
        with self.assertRaises(ValueError):
            self.consumidor.processar(json.dumps({"foo": "bar"}))

    def test_traceparent_valido_vem_no_resultado(self):
        saga_id = self.orquestrador.iniciar(5, "req-1", {})
        body = _resposta("m-1", "OrcamentoGerado", saga_id, 5, {"orcamentoId": 1})
        saida = self.consumidor.processar(
            body, {"traceparent": {"DataType": "String", "StringValue": TRACEPARENT_FIXO}}
        )
        parse = rastreio.parse_traceparent(TRACEPARENT_FIXO)
        self.assertEqual(saida["trace"]["trace_id"], parse.trace_id)
        self.assertIn("span_id", saida["trace"])

    def test_sem_traceparent_trace_vazio(self):
        saga_id = self.orquestrador.iniciar(5, "req-1", {})
        body = _resposta("m-1", "OrcamentoGerado", saga_id, 5, {"orcamentoId": 1})
        saida = self.consumidor.processar(body)
        self.assertEqual(saida.get("trace"), {})


class _DedupeFake:
    """Falso registro de eventos processados: marca e diz a primeira vez."""

    def __init__(self):
        self.vistos = set()

    def marcar(self, msg) -> bool:
        novo = msg.message_id not in self.vistos
        self.vistos.add(msg.message_id)
        return novo
