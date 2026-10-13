"""Ports da Saga — contratos de transporte (SQS, Kafka, RabbitMQ) e estado.

Conforme CONTRATO_SAGA_V1.md: nenhum acesso direto a banco alheio; toda
comunicação entre serviços sai por porta de mensageria.
"""
from typing import Any, Protocol

from os_service.application.saga.dto import SagaEventDTO, SagaInstanceStateDTO


class SagaPublisherPort(Protocol):
    """Publica eventos no barramento da saga."""

    def publish(self, event: SagaEventDTO) -> dict[str, Any]:
        """Publica o evento e retorna metadados do broker (message_id etc.)."""
        ...


class SagaStateRepositoryPort(Protocol):
    """Persistência do estado da saga (banco do próprio -os)."""

    def criar(self, os_id: int, passo_atual: Any) -> SagaInstanceStateDTO:
        """Cria instância de saga para uma OS."""
        ...

    def get_by_os(self, os_id: int) -> SagaInstanceStateDTO | None:
        """Estado atual da saga da OS (None = não iniciada)."""
        ...

    def avancar(self, os_id: int, novo_passo: Any) -> SagaInstanceStateDTO:
        """Marca passo atual e registra concluído o anterior."""
        ...

    def iniciar_compensacao(self, os_id: int) -> SagaInstanceStateDTO:
        """Estado → COMPENSANDO e define compensation_pendente."""
        ...

    def marcar_compensada(self, os_id: int) -> SagaInstanceStateDTO:
        """Estado → COMPENSADA."""
        ...

    def marcar_concluida(self, os_id: int) -> SagaInstanceStateDTO:
        """Estado → CONCLUIDA."""
        ...

    def marcar_falha(self, os_id: int, motivo: str) -> SagaInstanceStateDTO:
        """Estado → FALHA (erro irrecuperável de infra/domínio)."""
        ...


class SagaClockPort(Protocol):
    """Tempo injetado — facilita testes de timeout."""

    def agora(self) -> Any:
        """Datetime com tz."""
        ...
