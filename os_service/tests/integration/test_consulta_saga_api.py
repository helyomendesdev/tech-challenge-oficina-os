"""Rota de consulta da Saga: GET api/v1/ordens-servico/<id>/saga/.

Contrato: os_service/tests/fixtures/os-consulta-saga.openapi.yaml (copia do
-saga: contrato/os-consulta-saga.openapi.yaml). O historico e gravado de
verdade (orquestrador + consumidor), com o publisher SQS e um client fake.
"""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from django.test import TestCase
from jsonschema import Draft202012Validator, FormatChecker
from oficina_saga import rastreio
from rest_framework.test import APIClient

from os_service.application.saga.consumidor import ConsumidorRespostasSaga
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
from os_service.authentication import ClientPrincipal
from os_service.infrastructure.saga_adapters import (
    DjangoRegistroMensagensSaga,
    DjangoSagaStateRepositoryV1,
    InMemorySagaPublisherV1,
    SQSSagaPublisherV1,
)
from os_service.infrastructure.saga_consumidor import DjangoEventosProcessados
from os_service.models import SagaInstance, SagaMensagem
from os_service.tests.helpers import (
    criar_cliente,
    criar_ordem_servico,
    criar_usuario,
    criar_veiculo,
)

CONTRATO = Path(__file__).resolve().parent.parent / "fixtures" / "os-consulta-saga.openapi.yaml"
INICIO = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def _sem_nullable(no):
    """OpenAPI 3.0 `nullable: true` -> JSON Schema `type: [<tipo>, "null"]`."""
    if isinstance(no, dict):
        no = {k: _sem_nullable(v) for k, v in no.items()}
        if no.pop("nullable", False) and "type" in no:
            no["type"] = [no["type"], "null"]
    elif isinstance(no, list):
        no = [_sem_nullable(v) for v in no]
    return no


def validar_contra_contrato(teste, componente, dados):
    """Valida `dados` contra components.schemas.<componente> do YAML do contrato."""
    spec = yaml.safe_load(CONTRATO.read_text(encoding="utf-8"))
    schema = _sem_nullable(
        {
            "$ref": f"#/components/schemas/{componente}",
            "components": copy.deepcopy(spec["components"]),
        }
    )
    erros = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(dados),
        key=lambda e: list(e.path),
    )
    teste.assertEqual([], [f"{list(e.path)}: {e.message}" for e in erros])


class FakeSQS:
    def __init__(self):
        self.enviados = []

    def send_message(self, **kwargs):
        self.enviados.append(kwargs)
        return {"MessageId": f"sqs-{len(self.enviados)}"}


class SagaFluxoBase(TestCase):
    """Orquestrador + consumidor reais; so a fila (boto) e falsa."""

    def setUp(self):
        self.relogio = INICIO
        self.sqs = FakeSQS()
        registro = DjangoRegistroMensagensSaga()
        self.orquestrador = SagaOrquestradorV1(
            state_repository=DjangoSagaStateRepositoryV1(),
            publisher=SQSSagaPublisherV1("fila-billing", "fila-execution", self.sqs),
            clock=self._tick,
            registro_mensagens=registro,
        )
        self.consumidor = ConsumidorRespostasSaga(
            orquestrador=self.orquestrador,
            eventos_processados=DjangoEventosProcessados(),
            registro_mensagens=registro,
        )
        self.atendente = criar_usuario(username="atendente_saga", is_staff=True)
        self.dono = criar_cliente(usuario=self.atendente)
        veiculo = criar_veiculo(self.dono, usuario=self.atendente, placa="SGA1A11")
        self.os = criar_ordem_servico(self.dono, veiculo, usuario=self.atendente)

    def _tick(self):
        self.relogio += timedelta(seconds=1)
        return self.relogio

    def abrir_saga(self, request_id="req-bdd-a1b2c3"):
        return self.orquestrador.iniciar(
            os_id=self.os.id,
            request_id=request_id,
            payload={"clienteId": self.dono.id, "veiculoId": 1},
        )

    def responder(self, saga_id, tipo, payload=None, com_trace=True):
        """Resposta de Billing/Execution: eco do traceparent do ultimo comando."""
        ultimo = self.sqs.enviados[-1]
        comando = json.loads(ultimo["MessageBody"])
        corpo = {
            "messageId": str(uuid.uuid4()),
            "type": tipo,
            "version": 1,
            "sagaId": saga_id,
            "osId": self.os.id,
            "causationId": comando["messageId"],
            "requestId": comando["requestId"],
            "occurredAt": self._tick().isoformat(),
            "payload": payload or {},
        }
        atributos = {}
        if com_trace:
            filho = rastreio.novo_span_filho(
                ultimo["MessageAttributes"]["traceparent"]["StringValue"]
            )
            atributos = rastreio.injetar(None, filho)
        return corpo, atributos


class HistoricoDaSagaTest(SagaFluxoBase):
    def test_trace_unico_por_saga_da_abertura_ao_recebimento(self):
        """Abertura -> publish -> recebimento: um unico traceId no historico."""
        saga_id = self.abrir_saga()
        orcamento_id = str(uuid.uuid4())

        corpo, attrs = self.responder(
            saga_id, "OrcamentoGerado", {"orcamentoId": orcamento_id}
        )
        self.consumidor.processar(corpo, attrs)
        corpo, attrs = self.responder(saga_id, "OrcamentoAprovado")
        self.consumidor.processar(corpo, attrs)
        corpo, attrs = self.responder(
            saga_id, "PagamentoConfirmado", {"pagamentoId": str(uuid.uuid4())}
        )
        self.consumidor.processar(corpo, attrs)

        api = APIClient()
        api.force_authenticate(user=self.atendente)
        resposta = api.get(f"/api/v1/ordens-servico/{self.os.id}/saga/")
        self.assertEqual(resposta.status_code, 200)
        historico = resposta.json()["sagas"][0]["historico"]

        self.assertEqual(
            [(m["type"], m["direcao"]) for m in historico],
            [
                ("GerarOrcamento", "ENVIADA"),
                ("OrcamentoGerado", "RECEBIDA"),
                ("OrcamentoAprovado", "RECEBIDA"),
                ("SolicitarPagamento", "ENVIADA"),
                ("PagamentoConfirmado", "RECEBIDA"),
                ("IniciarExecucao", "ENVIADA"),
            ],
        )
        self.assertEqual(len({m["traceId"] for m in historico}), 1)
        # o trace e o da abertura da Saga, guardado nas referencias
        guardado = SagaInstance.objects.get(saga_id=saga_id).referencias["traceparent"]
        self.assertEqual(historico[0]["traceId"], rastreio.parse_traceparent(guardado).trace_id)
        # e e o que de fato foi para a fila (3 comandos, mesmo trace-id)
        self.assertEqual(len(self.sqs.enviados), 3)
        for enviado in self.sqs.enviados:
            tp = enviado["MessageAttributes"]["traceparent"]["StringValue"]
            self.assertEqual(rastreio.parse_traceparent(tp).trace_id, historico[0]["traceId"])

    def test_reentrega_da_mesma_resposta_nao_duplica_a_linha(self):
        saga_id = self.abrir_saga()
        corpo, attrs = self.responder(
            saga_id, "OrcamentoGerado", {"orcamentoId": str(uuid.uuid4())}
        )

        primeira = self.consumidor.processar(corpo, attrs)
        segunda = self.consumidor.processar(corpo, attrs)

        self.assertEqual(primeira["resultado"], "processado")
        self.assertEqual(segunda["resultado"], "duplicado")
        self.assertEqual(
            SagaMensagem.objects.filter(message_id=corpo["messageId"]).count(), 1
        )
        self.assertEqual(SagaMensagem.objects.filter(saga_id=saga_id).count(), 2)

    def test_resposta_de_saga_inexistente_nao_grava(self):
        self.abrir_saga()
        corpo, attrs = self.responder(str(uuid.uuid4()), "OrcamentoGerado")
        resultado = self.consumidor.processar(corpo, attrs)
        self.assertEqual(resultado["resultado"], "ignorada")
        self.assertFalse(SagaMensagem.objects.filter(message_id=corpo["messageId"]).exists())

    def test_resposta_sem_traceparent_usa_o_trace_da_saga(self):
        saga_id = self.abrir_saga()
        corpo, _ = self.responder(saga_id, "OrcamentoGerado", com_trace=False)
        self.consumidor.processar(corpo, {})
        guardado = SagaInstance.objects.get(saga_id=saga_id).referencias["traceparent"]
        recebida = SagaMensagem.objects.get(message_id=corpo["messageId"])
        self.assertEqual(recebida.trace_id, rastreio.parse_traceparent(guardado).trace_id)

    def test_registro_sem_trace_nem_saga_gera_trace_id_de_32_hex(self):
        from os_service.application.saga import dto

        msg = dto.MensagemSaga(
            message_id=str(uuid.uuid4()), type="OrcamentoGerado",
            saga_id=str(uuid.uuid4()), os_id=1, request_id="r",
            occurred_at=INICIO,
        )
        DjangoRegistroMensagensSaga().registrar(msg, "RECEBIDA")
        trace_id = SagaMensagem.objects.get(message_id=msg.message_id).trace_id
        self.assertRegex(trace_id, r"^[0-9a-f]{32}$")

    def test_falha_do_registro_nao_derruba_orquestrador_nem_consumidor(self):
        class Quebrado:
            def registrar(self, *args, **kwargs):
                raise RuntimeError("banco fora")

        self.orquestrador.registro_mensagens = Quebrado()
        self.consumidor.registro_mensagens = Quebrado()
        with self.assertLogs("os_service.saga", level="ERROR"):
            saga_id = self.abrir_saga()
            corpo, attrs = self.responder(saga_id, "OrcamentoGerado")
            resultado = self.consumidor.processar(corpo, attrs)
        self.assertEqual(resultado["resultado"], "processado")
        self.assertEqual(len(self.sqs.enviados), 1)

    def test_publisher_em_memoria_devolve_o_trace_da_saga(self):
        """O historico vale para qualquer publisher (aqui o de teste/local)."""
        orq = SagaOrquestradorV1(
            state_repository=DjangoSagaStateRepositoryV1(),
            publisher=InMemorySagaPublisherV1(),
            registro_mensagens=DjangoRegistroMensagensSaga(),
        )
        saga_id = orq.iniciar(self.os.id, "req-mem", {})
        enviada = SagaMensagem.objects.get(saga_id=saga_id)
        guardado = SagaInstance.objects.get(saga_id=saga_id).referencias["traceparent"]
        self.assertEqual(enviada.direcao, "ENVIADA")
        self.assertEqual(enviada.trace_id, rastreio.parse_traceparent(guardado).trace_id)


class ConsultaSagaApiTest(SagaFluxoBase):
    def url(self, os_id=None):
        return f"/api/v1/ordens-servico/{os_id or self.os.id}/saga/"

    def como_atendente(self):
        api = APIClient()
        api.force_authenticate(user=self.atendente)
        return api

    def como_cliente(self, cliente):
        api = APIClient()
        api.force_authenticate(user=ClientPrincipal(cliente_id=cliente.id, cliente=cliente))
        return api

    def test_200_valida_contra_o_schema_consulta_saga(self):
        saga_id = self.abrir_saga()
        orcamento_id = str(uuid.uuid4())
        corpo, attrs = self.responder(
            saga_id, "OrcamentoGerado", {"orcamentoId": orcamento_id}
        )
        self.consumidor.processar(corpo, attrs)

        resposta = self.como_atendente().get(self.url())

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        validar_contra_contrato(self, "ConsultaSaga", dados)
        self.assertEqual(dados["osId"], self.os.id)
        self.assertEqual(dados["sagaAtual"], saga_id)
        saga = dados["sagas"][0]
        self.assertEqual(saga["estado"], "AGUARDANDO_APROVACAO")
        self.assertEqual(saga["statusOs"], "AGUARDANDO")
        self.assertEqual(saga["orcamentoId"], orcamento_id)
        enviada, recebida = saga["historico"]
        self.assertEqual((enviada["type"], enviada["direcao"]), ("GerarOrcamento", "ENVIADA"))
        self.assertIsNone(enviada["causationId"])
        self.assertEqual((recebida["type"], recebida["direcao"]), ("OrcamentoGerado", "RECEBIDA"))
        self.assertEqual(recebida["causationId"], enviada["messageId"])
        self.assertEqual(recebida["requestId"], "req-bdd-a1b2c3")
        self.assertEqual(saga["atualizadaEm"], recebida["occurredAt"])

    def test_saga_sem_mensagens_omite_orcamento_e_atualizada_igual_iniciada(self):
        saga = SagaInstance.objects.create(os_id=self.os.id, estado="INICIADA")
        dados = self.como_atendente().get(self.url()).json()
        validar_contra_contrato(self, "ConsultaSaga", dados)
        self.assertNotIn("orcamentoId", dados["sagas"][0])
        self.assertEqual(dados["sagas"][0]["historico"], [])
        self.assertEqual(dados["sagas"][0]["atualizadaEm"], dados["sagas"][0]["iniciadaEm"])
        self.assertEqual(dados["sagaAtual"], str(saga.saga_id))

    def test_varias_sagas_da_mais_antiga_para_a_mais_recente(self):
        primeira = self.abrir_saga()
        segunda = self.abrir_saga()

        dados = self.como_atendente().get(self.url()).json()

        validar_contra_contrato(self, "ConsultaSaga", dados)
        self.assertEqual([s["sagaId"] for s in dados["sagas"]], [primeira, segunda])
        self.assertEqual(dados["sagaAtual"], segunda)

    def test_status_os_segue_o_estado_da_maquina(self):
        from os_service.application.saga.contrato_v1 import CONTRATO_SAGA_ESTADOS_V1

        self.assertEqual(
            set(CONTRATO_SAGA_ESTADOS_V1["status_os"]),
            set(CONTRATO_SAGA_ESTADOS_V1["estados"]),
        )
        SagaInstance.objects.create(os_id=self.os.id, estado="EM_EXECUCAO")
        dados = self.como_atendente().get(self.url()).json()
        self.assertEqual(dados["sagas"][0]["statusOs"], "EXECUCAO")

    def test_cliente_dono_le_a_saga(self):
        self.abrir_saga()
        resposta = self.como_cliente(self.dono).get(self.url())
        self.assertEqual(resposta.status_code, 200)

    def test_401_sem_token(self):
        self.abrir_saga()
        resposta = APIClient().get(self.url())
        self.assertEqual(resposta.status_code, 401)
        validar_contra_contrato(self, "Erro", resposta.json())

    def test_401_token_invalido(self):
        self.abrir_saga()
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION="Bearer isto-nao-e-um-jwt")
        resposta = api.get(self.url())
        self.assertEqual(resposta.status_code, 401)
        validar_contra_contrato(self, "Erro", resposta.json())

    def test_403_cliente_de_outra_os(self):
        self.abrir_saga()
        outro = criar_cliente(usuario=self.atendente, documento="49.648.573/0001-22")
        resposta = self.como_cliente(outro).get(self.url())
        self.assertEqual(resposta.status_code, 403)
        validar_contra_contrato(self, "Erro", resposta.json())
        self.assertNotIn("sagas", resposta.json())

    def test_403_usuario_que_nao_e_atendente(self):
        self.abrir_saga()
        comum = criar_usuario(username="comum_saga")
        api = APIClient()
        api.force_authenticate(user=comum)
        resposta = api.get(self.url())
        self.assertEqual(resposta.status_code, 403)
        validar_contra_contrato(self, "Erro", resposta.json())

    def test_404_os_inexistente(self):
        resposta = self.como_atendente().get(self.url(os_id=999999))
        self.assertEqual(resposta.status_code, 404)
        validar_contra_contrato(self, "Erro", resposta.json())
        self.assertEqual(resposta.json()["codigo"], "SAGA_NAO_ENCONTRADA")

    def test_404_os_sem_saga(self):
        resposta = self.como_atendente().get(self.url())
        self.assertEqual(resposta.status_code, 404)
        validar_contra_contrato(self, "Erro", resposta.json())
        self.assertEqual(resposta.json()["codigo"], "SAGA_NAO_ENCONTRADA")

    def test_404_cliente_para_os_inexistente(self):
        resposta = self.como_cliente(self.dono).get(self.url(os_id=999999))
        self.assertEqual(resposta.status_code, 404)
        validar_contra_contrato(self, "Erro", resposta.json())
