"""Orquestrador da Saga — máquina de estados do fluxo de OS.

Fluxo (CONTRATO_SAGA_V1.md):
    RESERVA_ESTOQUE → ORCAMENTO → APROVACAO → EXECUCAO → PAGAMENTO → ENTREGA

Falha em qualquer passo dispara compensação dos passos anteriores em ordem
inversa (seção 5 do contrato). Idempotência por `event_id` (seção 6).

Este módulo contém só a lógica de decisão da saga (camada de aplicação).
Transporte (SQS) e persistência entram por ports — adapters em infrastructure/.
"""
from os_service.application.saga.dto import (
    COMPENSACOES,
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

# Sequência do fluxo de felicidade
SEQUENCIA: tuple[SagaStep, ...] = (
    SagaStep.RESERVA_ESTOQUE,
    SagaStep.ORCAMENTO,
    SagaStep.APROVACAO,
    SagaStep.EXECUCAO,
    SagaStep.PAGAMENTO,
    SagaStep.ENTREGA,
)

# event_type que marca a conclusão de cada passo
EVENTO_CONCLUSAO: dict[SagaStep, set[SagaEventType]] = {
    SagaStep.RESERVA_ESTOQUE: {
        SagaEventType.ORDEM_SERVICO_CRIADA,
    },
    SagaStep.ORCAMENTO: {
        SagaEventType.ORCAMENTO_GERADO,
    },
    SagaStep.APROVACAO: {
        SagaEventType.ORCAMENTO_APROVADO,
        SagaEventType.ORCAMENTO_RECUSADO,
    },
    SagaStep.EXECUCAO: {
        SagaEventType.EXECUCAO_CONCLUIDA,
    },
    SagaStep.PAGAMENTO: {
        SagaEventType.PAGAMENTO_APROVADO,
        SagaEventType.PAGAMENTO_RECUSADO,
    },
    SagaStep.ENTREGA: {
        SagaEventType.ORDEM_SERVICO_ENTREGUE,
    },
}


class EventoDuplicadoError(Exception):
    """event_id já processado — skip silencioso (contrato, seção 6)."""


class PassoNaoConcluidoError(Exception):
    """Evento recebido não corresponde ao passo atual da saga."""


class SagaOrquestrador:
    """Decide transições da saga a partir dos eventos recebidos."""

    def __init__(
        self,
        state_repository: SagaStateRepositoryPort,
        publisher: SagaPublisherPort,
    ):
        self.state_repository = state_repository
        self.publisher = publisher

    # ------------------------------------------------------------------
    # Início
    # ------------------------------------------------------------------
    def iniciar(self, event: SagaEventDTO) -> SagaInstanceStateDTO:
        """Abre a saga para uma OS (primeiro passo: RESERVA_ESTOQUE)."""
        if event.event_type != SagaEventType.ORDEM_SERVICO_CRIADA:
            raise PassoNaoConcluidoError(
                f"Saga inicia com OrdemServicoCriada, recebido {event.event_type}"
            )
        return self.state_repository.criar(event.os_id, SagaStep.RESERVA_ESTOQUE)

    # ------------------------------------------------------------------
    # Processamento de evento
    # ------------------------------------------------------------------
    def processar(self, event: SagaEventDTO) -> SagaInstanceStateDTO:
        """Avança a saga a partir de um evento de conclusão de passo.

        - Recusado de orçamento/pagamento não compensa: são fins de fluxo
          legítimos decididos pelo cliente/sistema (contrato, seção 4).
        - Falha técnica de passo dispara compensação em ordem inversa.
        """
        estado = self.state_repository.get_by_os(event.os_id)
        if estado is None:
            raise PassoNaoConcluidoError(
                f"Saga não iniciada para OS {event.os_id}"
            )

        passo_atual = estado.passo_atual
        if passo_atual is None:
            raise PassoNaoConcluidoError(
                f"Saga sem passo atual para OS {event.os_id}"
            )
        esperados = EVENTO_CONCLUSAO.get(passo_atual, set())
        if event.event_type not in esperados:
            raise PassoNaoConcluidoError(
                f"Passo {passo_atual} espera {sorted(str(e.value) for e in esperados)}, "
                f"recebido {event.event_type}"
            )

        # Recusas legítimas encerram sem sucesso e sem compensação
        if event.event_type in (
            SagaEventType.ORCAMENTO_RECUSADO,
            SagaEventType.PAGAMENTO_RECUSADO,
        ):
            return self._encerrar_sem_sucesso(event, estado)

        # Avança para o próximo passo
        proximo = self._proximo_passo(passo_atual)
        if proximo is None:
            estado = self.state_repository.marcar_concluida(event.os_id)
        else:
            estado = self.state_repository.avancar(event.os_id, proximo)
            self._publicar_avanco(event, proximo)

        return estado

    # ------------------------------------------------------------------
    # Compensação
    # ------------------------------------------------------------------
    def compensar(self, os_id: int, motivo: str) -> SagaInstanceStateDTO:
        """Inicia compensação dos passos concluídos, em ordem inversa."""
        estado = self.state_repository.iniciar_compensacao(os_id)
        passos_concluidos = [
            p for p in reversed(list(estado.passos_concluidos))
            if p in COMPENSACOES
        ]
        for passo in passos_concluidos:
            evento_compensacao = self._evento_compensacao(
                os_id, passo, motivo,
            )
            self.publisher.publish(evento_compensacao)
        return self.state_repository.marcar_compensada(os_id)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _proximo_passo(self, passo_atual: SagaStep | None) -> SagaStep | None:
        if passo_atual not in SEQUENCIA:
            return None
        indice = SEQUENCIA.index(passo_atual)
        if indice + 1 >= len(SEQUENCIA):
            return None
        return SEQUENCIA[indice + 1]

    def _publicar_avanco(self, event: SagaEventDTO, proximo: SagaStep) -> None:
        """Publica o comando que inicia o próximo passo.

        EXECUCAO → execucao inicia por ExecucaoIniciada (comando direto).
        Demais passos esperam gatilho do próprio serviço responsable.
        """
        if proximo == SagaStep.EXECUCAO:
            self.publisher.publish(
                SagaEventDTO(
                    event_id=event.event_id,
                    event_type=SagaEventType.EXECUCAO_INICIADA,
                    occurred_at=event.occurred_at,
                    correlation_id=event.correlation_id,
                    os_id=event.os_id,
                    payload=event.payload,
                )
            )

    def _encerrar_sem_sucesso(
        self, event: SagaEventDTO, estado: SagaInstanceStateDTO
    ) -> SagaInstanceStateDTO:
        return self.state_repository.marcar_falha(
            event.os_id,
            motivo=f" Terminado sem sucesso por {event.event_type.value}",
        )

    def _evento_compensacao(
        self, os_id: int, passo: SagaStep, motivo: str
    ) -> SagaEventDTO:
        from datetime import datetime, timezone

        return SagaEventDTO(
            event_id=f"comp-{os_id}-{passo.value}",
            event_type=COMPENSACOES[passo],
            occurred_at=datetime.now(timezone.utc),
            correlation_id=f"os-{os_id}",
            os_id=os_id,
            payload={"motivo": motivo},
        )
