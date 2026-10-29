"""Testes do alinhamento do orquestrador ao contrato v1 (PR #2 do -saga).

Fatia A: dto v1 (estados, tipos, motivos, envelope) + máquina de estados
carregada de saga-estados.yaml (vendida em contrato_v1.py).

Fatia B: SagaOrquestrador v1 — comandos com causationId, transições da
máquina, compensação com motivo herdado, cancelamento externo.

Fatia C: SagaInstance.saga_id único (migração), repositório Django e
publisher com envelope v1, abertura e timeout adaptados.
"""
import unittest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from os_service.application.saga import dto as dto_v1
from os_service.application.saga.contrato_v1 import CONTRATO_SAGA_ESTADOS_V1


def _evento(**kwargs):
    base = dict(
        message_id=str(uuid4()),
        type="OrcamentoGerado",
        version=1,
        saga_id=str(uuid4()),
        os_id=101,
        causation_id=str(uuid4()),
        request_id="req-test",
        occurred_at=datetime.now(timezone.utc),
        payload={},
    )
    base.update(kwargs)
    return dto_v1.MensagemSaga(**base)


class DtoContratoV1TestCase(unittest.TestCase):
    """Fatia A — enums e envelope do contrato."""

    def test_tipos_de_mensagem_batem_com_o_enum_do_envelope(self):
        esperados = {
            "GerarOrcamento", "CancelarOrcamento", "SolicitarPagamento",
            "EstornarPagamento", "IniciarExecucao", "CancelarExecucao",
            "OrcamentoGerado", "OrcamentoFalhou", "OrcamentoAprovado",
            "OrcamentoRecusado", "OrcamentoCancelado",
            "PagamentoConfirmado", "PagamentoFalhou", "PagamentoEstornado",
            "ExecucaoIniciada", "ExecucaoFalhou", "ExecucaoFinalizada",
            "ExecucaoCancelada",
        }
        atual = {t.value for t in dto_v1.TipoMensagem}
        self.assertEqual(atual, esperados)

    def test_estados_da_saga_batem_com_a_maquina(self):
        self.assertEqual(
            {e.value for e in dto_v1.EstadoSaga},
            set(CONTRATO_SAGA_ESTADOS_V1["estados"].keys()),
        )

    def test_motivos_de_reversao_e_cancelamento(self):
        self.assertEqual(
            {m.value for m in dto_v1.MotivoReversao},
            {"EXECUCAO_FALHOU", "CANCELADO_PELO_CLIENTE", "TIMEOUT"},
        )
        self.assertEqual(
            {m.value for m in dto_v1.MotivoCancelamento},
            {"EXECUCAO_FALHOU", "CANCELADO_PELO_CLIENTE", "TIMEOUT",
             "PAGAMENTO_FALHOU"},
        )

    def test_envelope_serializa_campos_do_contrato(self):
        msg = _evento()
        env = dto_v1.envelope(msg)
        self.assertEqual(
            set(env.keys()),
            {"messageId", "type", "version", "sagaId", "osId", "causationId",
             "requestId", "occurredAt", "payload"},
        )
        UUID(env["messageId"])
        UUID(env["sagaId"])
        self.assertEqual(env["type"], "OrcamentoGerado")
        self.assertEqual(env["osId"], 101)


class MaquinaEstadosTestCase(unittest.TestCase):
    """Fatia A — carregamento e consulta da máquina de estados."""

    def setUp(self):
        self.maquina = dto_v1.MaquinaEstados(CONTRATO_SAGA_ESTADOS_V1)

    def test_estado_inicial_e_finais(self):
        self.assertEqual(self.maquina.inicial, dto_v1.EstadoSaga.INICIADA)
        self.assertEqual(
            self.maquina.finais,
            {dto_v1.EstadoSaga.CONCLUIDA, dto_v1.EstadoSaga.CANCELADA},
        )

    def test_transicao_feliz_orcamento(self):
        alvo, comando, motivo = self.maquina.transicao(
            dto_v1.EstadoSaga.ORCAMENTO_SOLICITADO, "OrcamentoGerado",
        )
        self.assertEqual(alvo, dto_v1.EstadoSaga.AGUARDANDO_APROVACAO)
        self.assertIsNone(comando)
        self.assertIsNone(motivo)

    def test_transicao_que_envia_comando_com_motivo(self):
        alvo, comando, motivo = self.maquina.transicao(
            dto_v1.EstadoSaga.PAGAMENTO_SOLICITADO, "PagamentoFalhou",
        )
        self.assertEqual(alvo, dto_v1.EstadoSaga.COMPENSANDO)
        self.assertEqual(comando, "CancelarOrcamento")
        self.assertEqual(motivo, dto_v1.MotivoCancelamento.PAGAMENTO_FALHOU)

    def test_transicao_com_motivo_herdado(self):
        _, comando, motivo = self.maquina.transicao(
            dto_v1.EstadoSaga.COMPENSANDO, "ExecucaoCancelada",
        )
        self.assertEqual(comando, "EstornarPagamento")
        self.assertEqual(motivo, "herdado")

    def test_mensagem_fora_do_estado_levanta(self):
        with self.assertRaises(dto_v1.TransicaoInvalidaError):
            self.maquina.transicao(
                dto_v1.EstadoSaga.AGUARDANDO_APROVACAO, "PagamentoConfirmado",
            )

    def test_gatilho_externo_cancelamento(self):
        alvo, comando, motivo = self.maquina.transicao(
            dto_v1.EstadoSaga.EM_EXECUCAO, "cancelamento",
        )
        self.assertEqual(alvo, dto_v1.EstadoSaga.COMPENSANDO)
        self.assertEqual(comando, "CancelarExecucao")
        self.assertEqual(motivo, dto_v1.MotivoReversao.CANCELADO_PELO_CLIENTE)

    def test_gatilho_externo_timeout(self):
        """ADR-003 do -saga: orçamento sem resposta compensa com motivo TIMEOUT."""
        alvo, comando, motivo = self.maquina.transicao(
            dto_v1.EstadoSaga.AGUARDANDO_APROVACAO, "timeout",
        )
        self.assertEqual(alvo, dto_v1.EstadoSaga.COMPENSANDO)
        self.assertEqual(comando, "CancelarOrcamento")
        self.assertEqual(motivo, "TIMEOUT")

    def test_transicoes_cobrem_todos_os_estados_da_maquina(self):
        """Toda transição referencia estados e mensagens conhecidos."""
        gatilhos = set(CONTRATO_SAGA_ESTADOS_V1["gatilhos_externos"])
        tipos = {t.value for t in dto_v1.TipoMensagem}
        for transicao in CONTRATO_SAGA_ESTADOS_V1["transicoes"]:
            dto_v1.EstadoSaga(transicao["de"])
            dto_v1.EstadoSaga(transicao["para"])
            self.assertIn(transicao["recebe"], tipos | gatilhos)
            if "envia" in transicao:
                self.assertIn(transicao["envia"], tipos)


if __name__ == "__main__":
    unittest.main()
