"""Testes do orquestrador da Saga (camada de aplicação, sem Django).

Publisher e state repository fakes — foco na lógica de transição,
compensação e idempotência do CONTRATO_SAGA_V1.md.
"""
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from os_service.application.saga.dto import (
    SagaEventDTO,
    SagaEventType,
    SagaInstanceStateDTO,
    SagaStatus,
    SagaStep,
)
from os_service.application.saga.ports import (
    SagaPublisherPort,
    SagaStateRepositoryPort,
)
from os_service.application.saga.orquestrador import (
    EVENTO_CONCLUSAO,
    PassoNaoConcluidoError,
    SagaOrquestrador,
    SEQUENCIA,
)


class FakeStateRepository(SagaStateRepositoryPort):
    """State repository fake em memória."""

    def __init__(self):
        self.dados: dict[int, dict] = {}

    def _dto(self, os_id):
        d = self.dados[os_id]
        return SagaInstanceStateDTO(
            os_id=os_id,
            status=d["status"],
            passo_atual=d["passo_atual"],
            passos_concluidos=tuple(d["passos_concluidos"]),
            compensation_pendente=d.get("compensation_pendente"),
        )

    def criar(self, os_id, passo_atual):
        from datetime import datetime, timezone

        self.dados[os_id] = {
            "status": SagaStatus.EM_ANDAMENTO,
            "passo_atual": passo_atual,
            "passos_concluidos": [],
        }
        return self._dto(os_id)

    def get_by_os(self, os_id):
        if os_id not in self.dados:
            return None
        return self._dto(os_id)

    def avancar(self, os_id, novo_passo):
        d = self.dados[os_id]
        if d["passo_atual"]:
            d["passos_concluidos"].append(d["passo_atual"])
        d["passo_atual"] = novo_passo
        return self._dto(os_id)

    def iniciar_compensacao(self, os_id):
        self.dados[os_id]["status"] = SagaStatus.COMPENSANDO
        return self._dto(os_id)

    def marcar_compensada(self, os_id):
        self.dados[os_id]["status"] = SagaStatus.COMPENSADA
        self.dados[os_id]["passo_atual"] = None
        return self._dto(os_id)

    def marcar_concluida(self, os_id):
        self.dados[os_id]["status"] = SagaStatus.CONCLUIDA
        self.dados[os_id]["passo_atual"] = None
        return self._dto(os_id)

    def marcar_falha(self, os_id, motivo):
        self.dados[os_id]["status"] = SagaStatus.FALHA
        self.dados[os_id]["passo_atual"] = None
        self.dados[os_id]["motivo"] = motivo
        return self._dto(os_id)


class FakePublisher(SagaPublisherPort):
    def __init__(self):
        self.publicados = []

    def publish(self, event):
        self.publicados.append(event)
        return {"MessageId": event.event_id}


def evento(tipo, os_id=1):
    return SagaEventDTO(
        event_id=str(uuid4()),
        event_type=tipo,
        occurred_at=datetime.now(timezone.utc),
        correlation_id=f"os-{os_id}",
        os_id=os_id,
        payload={},
    )


class SagaOrquestradorTestCase(unittest.TestCase):
    def setUp(self):
        self.state = FakeStateRepository()
        self.publisher = FakePublisher()
        self.saga = SagaOrquestrador(self.state, self.publisher)

    def test_iniciar_com_evento_errado_rejeita(self):
        with self.assertRaises(PassoNaoConcluidoError):
            self.saga.iniciar(evento(SagaEventType.EXECUCAO_INICIADA))

    def test_iniciar_cria_estado_no_primeiro_passo(self):
        estado = self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.assertEqual(estado.status, SagaStatus.EM_ANDAMENTO)
        self.assertEqual(estado.passo_atual, SagaStep.RESERVA_ESTOQUE)

    def test_fluxo_de_felicidade_completo_concluida(self):
        self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))

        self.saga.processar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))  # passo 1 conclui
        estado = self.saga.processar(evento(SagaEventType.ORCAMENTO_GERADO))
        self.assertEqual(estado.passo_atual, SagaStep.APROVACAO)

        estado = self.saga.processar(evento(SagaEventType.ORCAMENTO_APROVADO))
        self.assertEqual(estado.passo_atual, SagaStep.EXECUCAO)
        # EXECUCAO dispara comando ExecucaoIniciada
        tipos = [e.event_type for e in self.publisher.publicados]
        self.assertIn(SagaEventType.EXECUCAO_INICIADA, tipos)

        estado = self.saga.processar(evento(SagaEventType.EXECUCAO_CONCLUIDA))
        self.assertEqual(estado.passo_atual, SagaStep.PAGAMENTO)

        estado = self.saga.processar(evento(SagaEventType.PAGAMENTO_APROVADO))
        self.assertEqual(estado.passo_atual, SagaStep.ENTREGA)

        estado = self.saga.processar(evento(SagaEventType.ORDEM_SERVICO_ENTREGUE))
        self.assertEqual(estado.status, SagaStatus.CONCLUIDA)

    def test_orcamento_recusado_termina_sem_compensacao(self):
        self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORCAMENTO_GERADO))
        estado = self.saga.processar(evento(SagaEventType.ORCAMENTO_RECUSADO))

        self.assertEqual(estado.status, SagaStatus.FALHA)
        # Recusa legítima não publica compensação
        tipos = [e.event_type for e in self.publisher.publicados]
        self.assertNotIn(SagaEventType.ESTOQUE_LIBERADO, tipos)

    def test_evento_fora_do_passo_rejeita(self):
        self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        with self.assertRaises(PassoNaoConcluidoError):
            self.saga.processar(evento(SagaEventType.PAGAMENTO_APROVADO))

    def test_saga_nao_iniciada_rejeita_processamento(self):
        with self.assertRaises(PassoNaoConcluidoError):
            self.saga.processar(evento(SagaEventType.ORCAMENTO_GERADO, os_id=999))

    def test_compensar_publica_compensacoes_em_ordem_inversa(self):
        self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORCAMENTO_GERADO))
        self.saga.processar(evento(SagaEventType.ORCAMENTO_APROVADO))
        # A partir daqui EXECUCAO em progresso; simular falha técnica e compensar
        estado = self.saga.compensar(1, motivo="falha técnica na execução")

        self.assertEqual(estado.status, SagaStatus.COMPENSADA)
        tipos = [e.event_type for e in self.publisher.publicados]
        # Em ordem inversa: antes APROVACAO/ORCAMENTO, depois RESERVA_ESTOQUE
        self.assertIn(SagaEventType.ORCAMENTO_CANCELADO, tipos)
        self.assertIn(SagaEventType.ESTOQUE_LIBERADO, tipos)
        if SagaEventType.ORCAMENTO_CANCELADO in tipos and SagaEventType.ESTOQUE_LIBERADO in tipos:
            self.assertLess(
                tipos.index(SagaEventType.ORCAMENTO_CANCELADO),
                tipos.index(SagaEventType.ESTOQUE_LIBERADO),
            )

    def test_sequencia_conhecida(self):
        self.assertEqual(
            SEQUENCIA,
            (
                SagaStep.RESERVA_ESTOQUE,
                SagaStep.ORCAMENTO,
                SagaStep.APROVACAO,
                SagaStep.EXECUCAO,
                SagaStep.PAGAMENTO,
                SagaStep.ENTREGA,
            ),
        )


if __name__ == "__main__":
    unittest.main()
