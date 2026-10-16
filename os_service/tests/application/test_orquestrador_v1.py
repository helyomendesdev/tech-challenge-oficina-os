"""Testes do orquestrador v1 (contrato -saga PR #2) — fatia B.

Comportamentos da máquina de estados aplicados pelo SagaOrquestrador:
- iniciar: gatilho `inicio` → estado ORCAMENTO_SOLICITADO + comando GerarOrcamento
- processar: resposta aplicada via transição da máquina; comando seguinte
  sai com causationId = messageId da resposta; compensação herda motivo;
- mensagem fora de estado: registra e descarta (semântica do contrato);
- cancelar: gatilho externo `cancelamento` (só em EM_EXECUCAO).
"""
import unittest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from os_service.application.saga import dto
from os_service.application.saga.contrato_v1 import CONTRATO_SAGA_ESTADOS_V1
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1


class FakeStateRepository:
    """Estado em memória, sem Django."""

    def __init__(self):
        self.dados: dict[str, dict] = {}

    def criar(self, saga_id, os_id, estado_inicial):
        self.dados[saga_id] = {
            "saga_id": saga_id,
            "os_id": os_id,
            "status": dto.StatusSaga.EM_ANDAMENTO,
            "estado": estado_inicial,
            "motivo": None,
        }
        return self._dto(saga_id)

    def get(self, saga_id):
        if saga_id not in self.dados:
            return None
        return self._dto(saga_id)

    def salvar_estado(self, saga_id, novo_estado):
        self.dados[saga_id]["estado"] = novo_estado
        if novo_estado in (dto.EstadoSaga.CONCLUIDA, dto.EstadoSaga.CANCELADA):
            self.dados[saga_id]["status"] = (
                dto.StatusSaga.CONCLUIDA
                if novo_estado is dto.EstadoSaga.CONCLUIDA
                else dto.StatusSaga.FALHA
            )
        return self._dto(saga_id)

    def salvar_motivo(self, saga_id, motivo):
        self.dados[saga_id]["motivo"] = motivo

    def _dto(self, saga_id):
        d = self.dados[saga_id]
        return dto.SagaV1StateDTO(
            saga_id=d["saga_id"],
            os_id=d["os_id"],
            status=d["status"],
            estado=d["estado"],
            motivo=d["motivo"],
        )


class FakePublisher:
    def __init__(self):
        self.publicados: list[dto.MensagemSaga] = []

    def publish(self, event):
        self.publicados.append(event)
        return {"MessageId": event.message_id}


def _mensagem(**kwargs):
    base = dict(
        message_id=str(uuid4()),
        type="OrcamentoGerado",
        saga_id=None,  # preenchido nos testes
        os_id=101,
        request_id="req-test",
        occurred_at=datetime.now(timezone.utc),
        payload={},
    )
    base.update(kwargs)
    return dto.MensagemSaga(**base)


class SagaOrquestradorV1TestCase(unittest.TestCase):
    def setUp(self):
        self.state = FakeStateRepository()
        self.publisher = FakePublisher()
        self.clock = lambda: datetime.now(timezone.utc)
        self.saga = SagaOrquestradorV1(
            state_repository=self.state,
            publisher=self.publisher,
            clock=self.clock,
        )
        self.saga_id = self.saga.iniciar(
            os_id=101,
            request_id="req-abc",
            payload={"clienteId": 7, "veiculoId": 15, "servicos": [], "pecas": []},
        )

    # ------------------------------------------------------------------
    # iniciar
    # ------------------------------------------------------------------
    def test_iniciar_abre_saga_e_manda_gerar_orcamento(self):
        estado = self.state.get(self.saga_id)
        self.assertEqual(estado.estado, dto.EstadoSaga.ORCAMENTO_SOLICITADO)
        self.assertEqual(estado.os_id, 101)

        (comando,) = self.publisher.publicados
        self.assertEqual(comando.type, "GerarOrcamento")
        self.assertEqual(comando.saga_id, self.saga_id)
        self.assertEqual(comando.os_id, 101)
        self.assertEqual(comando.request_id, "req-abc")
        self.assertIsNone(comando.causation_id)  # gatilho externo: causa nula

    def test_iniciar_gera_saga_id_novo_a_cada_abertura(self):
        outro = self.saga.iniciar(os_id=102, request_id="req-x", payload={})
        self.assertNotEqual(outro, self.saga_id)
        UUID(outro)

    # ------------------------------------------------------------------
    # fluxo feliz
    # ------------------------------------------------------------------
    def _responder(self, type_, payload=None, **kwargs):
        msg = _mensagem(
            type=type_,
            saga_id=self.saga_id,
            payload=payload or {},
            **kwargs,
        )
        self._ultima_msg = msg
        return self.saga.processar(msg)

    def test_fluxo_feliz_completo_com_comandos_e_causation(self):
        r1 = self._responder("OrcamentoGerado", {"orcamentoId": str(uuid4()), "valorTotalCentavos": 10000, "validoAte": "2026-10-20T12:00:00Z"})
        self.assertEqual(r1.estado, dto.EstadoSaga.AGUARDANDO_APROVACAO)
        self.assertEqual([c.type for c in self.publisher.publicados], ["GerarOrcamento"])

        r2 = self._responder("OrcamentoAprovado", {"orcamentoId": str(uuid4()), "aprovadoEm": "2026-10-16T13:00:00Z"})
        self.assertEqual(r2.estado, dto.EstadoSaga.PAGAMENTO_SOLICITADO)
        solicitou = self.publisher.publicados[-1]
        self.assertEqual(solicitou.type, "SolicitarPagamento")
        self.assertEqual(solicitou.causation_id, self._ultima_msg.message_id)

        r3 = self._responder("PagamentoConfirmado", {"pagamentoId": str(uuid4()), "valorPagoCentavos": 10000, "mercadoPagoPaymentId": "mp-1", "confirmadoEm": "2026-10-16T13:05:00Z"})
        self.assertEqual(r3.estado, dto.EstadoSaga.EXECUCAO_SOLICITADA)
        iniciou = self.publisher.publicados[-1]
        self.assertEqual(iniciou.type, "IniciarExecucao")
        self.assertEqual(iniciou.causation_id, self._ultima_msg.message_id)

        r4 = self._responder("ExecucaoIniciada", {"execucaoId": str(uuid4()), "orcamentoId": str(uuid4()), "iniciadaEm": "2026-10-16T13:06:00Z"})
        self.assertEqual(r4.estado, dto.EstadoSaga.EM_EXECUCAO)

        r5 = self._responder("ExecucaoFinalizada", {"execucaoId": str(uuid4()), "finalizadaEm": "2026-10-16T14:00:00Z"})
        self.assertEqual(r5.estado, dto.EstadoSaga.CONCLUIDA)
        self.assertEqual(r5.status, dto.StatusSaga.CONCLUIDA)
        # Nenhum comando depois do IniciarExecucao no fluxo feliz
        self.assertEqual([c.type for c in self.publisher.publicados[3:]], [])

    # ------------------------------------------------------------------
    # falhas e compensação
    # ------------------------------------------------------------------
    def test_pagamento_falhou_compensa_com_motivo_herdado(self):
        self._responder("OrcamentoGerado", {"orcamentoId": str(uuid4()), "valorTotalCentavos": 10000, "validoAte": "2026-10-20T12:00:00Z"})
        self._responder("OrcamentoAprovado", {"orcamentoId": str(uuid4()), "aprovadoEm": "2026-10-16T13:00:00Z"})
        r = self._responder("PagamentoFalhou", {"pagamentoId": str(uuid4()), "erro": {"codigo": "MP_RECUSADO", "mensagem": "recusado"}})

        self.assertEqual(r.estado, dto.EstadoSaga.COMPENSANDO)
        cancelou = self.publisher.publicados[-1]
        self.assertEqual(cancelou.type, "CancelarOrcamento")
        self.assertEqual(cancelou.payload["motivo"], "PAGAMENTO_FALHOU")

        # trilha falha-pagamento: OrcamentoCancelado encerra em CANCELADA
        r2 = self._responder("OrcamentoCancelado", {"orcamentoId": str(uuid4()), "canceladoEm": "2026-10-16T13:10:00Z"})
        self.assertEqual(r2.estado, dto.EstadoSaga.CANCELADA)
        self.assertEqual(r2.status, dto.StatusSaga.FALHA)

    def test_compensacao_herda_motivo_da_trilha(self):
        self._responder("OrcamentoGerado", {"orcamentoId": str(uuid4()), "valorTotalCentavos": 10000, "validoAte": "2026-10-20T12:00:00Z"})
        self._responder("OrcamentoAprovado", {"orcamentoId": str(uuid4()), "aprovadoEm": "2026-10-16T13:00:00Z"})
        self._responder("PagamentoConfirmado", {"pagamentoId": str(uuid4()), "valorPagoCentavos": 10000, "mercadoPagoPaymentId": "mp-1", "confirmadoEm": "2026-10-16T13:05:00Z"})
        self._responder("ExecucaoIniciada", {"execucaoId": str(uuid4()), "orcamentoId": str(uuid4()), "iniciadaEm": "2026-10-16T13:06:00Z"})
        # Falha na execução → CancelarExecucao (EXECUCAO_FALHOU)
        self._responder("ExecucaoFalhou", {"orcamentoId": str(uuid4()), "execucaoId": str(uuid4()), "erro": {"codigo": "PECAS_FALTANDO", "mensagem": "x"}})
        estornou = self.publisher.publicados[-1]
        self.assertEqual(estornou.type, "CancelarExecucao")
        self.assertEqual(estornou.payload["motivo"], "EXECUCAO_FALHOU")

        # ExecucaoCancelada → EstornarPagamento com motivo HERDADO
        self._responder("ExecucaoCancelada", {"execucaoId": str(uuid4()), "canceladaEm": "2026-10-16T13:20:00Z"})
        estorno = self.publisher.publicados[-1]
        self.assertEqual(estorno.type, "EstornarPagamento")
        self.assertEqual(estorno.payload["motivo"], "EXECUCAO_FALHOU")

        # PagamentoEstornado → CancelarOrcamento com motivo HERDADO
        self._responder("PagamentoEstornado", {"pagamentoId": str(uuid4()), "estornadoEm": "2026-10-16T13:25:00Z"})
        cancelou = self.publisher.publicados[-1]
        self.assertEqual(cancelou.type, "CancelarOrcamento")
        self.assertEqual(cancelou.payload["motivo"], "EXECUCAO_FALHOU")

    # ------------------------------------------------------------------
    # recusas, falha de orçamento e mensagens fora de estado
    # ------------------------------------------------------------------
    def test_orcamento_recusado_cancela_saga_sem_compensar(self):
        self._responder("OrcamentoGerado", {"orcamentoId": str(uuid4()), "valorTotalCentavos": 10000, "validoAte": "2026-10-20T12:00:00Z"})
        n_antes = len(self.publisher.publicados)
        r = self._responder("OrcamentoRecusado", {"orcamentoId": str(uuid4()), "recusadoEm": "2026-10-16T13:00:00Z", "motivo": "CLIENTE"})
        self.assertEqual(r.estado, dto.EstadoSaga.CANCELADA)
        self.assertEqual(len(self.publisher.publicados), n_antes)

    def test_mensagem_fora_do_estado_registra_e_descarta(self):
        r = self._responder("ExecucaoFinalizada", {"execucaoId": str(uuid4()), "finalizadaEm": "2026-10-16T13:00:00Z"})
        # ORCAMENTO_SOLICITADO não aceita ExecucaoFinalizada: estado mantido
        self.assertEqual(r.estado, dto.EstadoSaga.ORCAMENTO_SOLICITADO)
        self.assertEqual(len(self.publisher.publicados), 1)  # só GerarOrcamento

    # ------------------------------------------------------------------
    # cancelamento externo
    # ------------------------------------------------------------------
    def test_cancelamento_externo_soment_em_execucao(self):
        r = self.saga.cancelar(self.saga_id, request_id="req-cancel")
        # em ORCAMENTO_SOLICITADO não há transição de cancelamento: descarta
        self.assertEqual(r.estado, dto.EstadoSaga.ORCAMENTO_SOLICITADO)

        # avança até EM_EXECUCAO
        self._responder("OrcamentoGerado", {"orcamentoId": str(uuid4()), "valorTotalCentavos": 10000, "validoAte": "2026-10-20T12:00:00Z"})
        self._responder("OrcamentoAprovado", {"orcamentoId": str(uuid4()), "aprovadoEm": "2026-10-16T13:00:00Z"})
        self._responder("PagamentoConfirmado", {"pagamentoId": str(uuid4()), "valorPagoCentavos": 10000, "mercadoPagoPaymentId": "mp-1", "confirmadoEm": "2026-10-16T13:05:00Z"})
        self._responder("ExecucaoIniciada", {"execucaoId": str(uuid4()), "orcamentoId": str(uuid4()), "iniciadaEm": "2026-10-16T13:06:00Z"})

        r2 = self.saga.cancelar(self.saga_id, request_id="req-cancel")
        self.assertEqual(r2.estado, dto.EstadoSaga.COMPENSANDO)
        comando = self.publisher.publicados[-1]
        self.assertEqual(comando.type, "CancelarExecucao")
        self.assertEqual(comando.payload["motivo"], "CANCELADO_PELO_CLIENTE")
        self.assertIsNone(comando.causation_id)  # gatilho externo

    def test_saga_inexistente_rejeita(self):
        with self.assertRaises(dto.SagaNaoEncontradaError):
            self.saga.processar(_mensagem(saga_id=str(uuid4())))


if __name__ == "__main__":
    unittest.main()
