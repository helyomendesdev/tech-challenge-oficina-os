"""Testes do timeout de orçamento (contrato, seção 8)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from django.test import TestCase

from os_service.application.saga.dto import (
    SagaEventDTO,
    SagaEventType,
    SagaInstanceStateDTO,
    SagaStatus,
    SagaStep,
)
from os_service.application.saga.orquestrador import SagaOrquestrador
from os_service.application.saga.timeout_orcamento import (
    TimeoutOrcamentoService,
    timeout_padrao,
)


def _dto(os_id, dados):
    return SagaInstanceStateDTO(
        os_id=os_id,
        status=SagaStatus(dados["status"]),
        passo_atual=dados.get("passo_atual"),
        passos_concluidos=tuple(dados.get("passos_concluidos", ())),
    )


class FakeStateRepository:
    """State repository fake (mesmo shape do teste do orquestrador)."""

    def __init__(self):
        self.dados: dict[int, dict] = {}

    def criar(self, os_id, passo_atual):
        self.dados[os_id] = {
            "status": SagaStatus.EM_ANDAMENTO,
            "passo_atual": passo_atual,
            "passos_concluidos": [],
        }
        return _dto(os_id, self.dados[os_id])

    def get_by_os(self, os_id):
        d = self.dados.get(os_id)
        return _dto(os_id, d) if d else None

    def avancar(self, os_id, novo_passo):
        d = self.dados[os_id]
        if d["passo_atual"]:
            d["passos_concluidos"].append(d["passo_atual"])
        d["passo_atual"] = novo_passo
        return _dto(os_id, d)

    def iniciar_compensacao(self, os_id):
        self.dados[os_id]["status"] = SagaStatus.COMPENSANDO
        return _dto(os_id, self.dados[os_id])

    def marcar_compensada(self, os_id):
        self.dados[os_id]["status"] = SagaStatus.COMPENSADA
        self.dados[os_id]["passo_atual"] = None
        return _dto(os_id, self.dados[os_id])

    def marcar_concluida(self, os_id):
        self.dados[os_id]["status"] = SagaStatus.CONCLUIDA
        self.dados[os_id]["passo_atual"] = None
        return _dto(os_id, self.dados[os_id])

    def marcar_falha(self, os_id, motivo):
        self.dados[os_id]["status"] = SagaStatus.FALHA
        self.dados[os_id]["passo_atual"] = None
        return _dto(os_id, self.dados[os_id])


class FakeTimeoutPort:
    def __init__(self, expiradas):
        self.expiradas = expiradas
        self.marcadas = []

    def listar_aguardando_antes_de(self, limite):
        return self.expiradas

    def marcar_orcamento_expirado(self, os_id):
        self.marcadas.append(os_id)


class TimeoutOrcamentoTestCase(TestCase):
    def setUp(self):
        self.state = FakeStateRepository()
        self.publisher = MagicMock()
        self.orquestrador = SagaOrquestrador(self.state, self.publisher)

    def test_timeout_padrao_e_30_minutos(self):
        self.assertEqual(timeout_padrao(), timedelta(minutes=30))

    def test_os_expirada_compensa_e_marca_diagnostico(self):
        port = FakeTimeoutPort([(5, 250.0)])
        service = TimeoutOrcamentoService(self.orquestrador, port)

        # saga já rodando pra OS 5 (estoque reservado, orçamento pendente)
        from os_service.application.saga.dto import (
            SagaEventDTO,
            SagaEventType,
            SagaStep,
        )
        ev = SagaEventDTO(
            event_id="e1", event_type=SagaEventType.ORDEM_SERVICO_CRIADA,
            occurred_at=datetime.now(timezone.utc), correlation_id="os-5", os_id=5,
        )
        self.orquestrador.iniciar(ev)
        # passo RESERVA_ESTOQUE concluído, ORCAMENTE em andamento:
        # compensação devolve estoque; orçamento cancelado (não gerado ainda)
        self.state.avancar(5, SagaStep.ORCAMENTO)

        resumo = service.executar(agora=datetime.now(timezone.utc))

        self.assertEqual(resumo, {"expiradas": 1, "compensadas": 1, "erro": 0})
        self.assertEqual(port.marcadas, [5])
        # Compensação só publica eventos de passos JÁ CONCLUÍDOS:
        # RESERVA_ESTOQUE concluído → EstoqueLiberado; ORCAMENTO em curso
        # não concluído → OrcamentoCancelado não entra nessa varredura.
        chamadas = self.publisher.publish.call_args_list
        tipos_publicados = [c.args[0].event_type.value for c in chamadas if c.args]
        self.assertEqual(tipos_publicados, ["EstoqueLiberado"])

    def test_nada_expirado_zera_resumo(self):
        port = FakeTimeoutPort([])
        service = TimeoutOrcamentoService(self.orquestrador, port)
        resumo = service.executar(agora=datetime.now(timezone.utc))
        self.assertEqual(resumo, {"expiradas": 0, "compensadas": 0, "erro": 0})
