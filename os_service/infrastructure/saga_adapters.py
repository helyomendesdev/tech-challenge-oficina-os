"""Adapters de infraestrutura da Saga.

- SQSSagaPublisher: publica no barramento (boto3 personificado).
- DjangoSagaStateRepository: estado da saga no banco do próprio -os.

SQS entra de fato quando Sophia subir a infra (SAGA_QUEUE_URL); endpoints
locais usam o publisher fake dos testes.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any

from os_service.application.saga.dto import (
    SagaEventDTO,
    SagaInstanceStateDTO,
    SagaStatus,
    SagaStep,
)
from os_service.application.saga.ports import (
    SagaPublisherPort,
    SagaStateRepositoryPort,
)

logger = logging.getLogger("os_service.saga")


class SQSSagaPublisher(SagaPublisherPort):
    """Publisher SQS do barramento da saga (envelope do contrato v1)."""

    def __init__(self, queue_url: str, client: Any):
        self.queue_url = queue_url
        self.client = client

    def _envelope(self, event: SagaEventDTO) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "event_version": event.event_version,
            "occurred_at": event.occurred_at.isoformat(),
            "correlation_id": event.correlation_id,
            "payload": event.payload,
            "metadata": {"producer": event.producer, "schema": "v1"},
        }

    def publish(self, event: SagaEventDTO) -> dict[str, Any]:
        if not self.queue_url:
            raise RuntimeError("SAGA_QUEUE_URL não configurada")
        response = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(self._envelope(event), ensure_ascii=False),
            MessageGroupId=os_id_group(event.os_id),
        )
        logger.info(
            "saga_evento_publicado",
            extra={
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "os_id": event.os_id,
                "message_id": response.get("MessageId"),
            },
        )
        return response


def os_id_group(os_id: int) -> str:
    """FIFO group por OS — eventos de uma mesma OS em ordem."""
    return f"os-{os_id}"


class InMemorySagaPublisher(SagaPublisherPort):
    """Publisher de teste/local — guarda eventos em memória."""

    def __init__(self):
        self.publicados: list[SagaEventDTO] = []

    def publish(self, event: SagaEventDTO) -> dict[str, Any]:
        self.publicados.append(event)
        return {"MessageId": f"local-{len(self.publicados)}"}


class DjangoSagaStateRepository(SagaStateRepositoryPort):
    """Estado da saga no banco do -os (novo app django `os_service` models)."""

    def _criar_dto(self, instance: Any) -> SagaInstanceStateDTO:
        return SagaInstanceStateDTO(
            os_id=instance.os_id,
            status=SagaStatus(instance.status),
            passo_atual=(
                SagaStep(instance.passo_atual) if instance.passo_atual else None
            ),
            passos_concluidos=tuple(SagaStep(p) for p in instance.passos_concluidos),
            compensation_pendente=(
                SagaStep(instance.compensation_pendente)
                if instance.compensation_pendente
                else None
            ),
            iniciado_em=instance.iniciado_em,
            atualizado_em=instance.atualizado_em,
        )
