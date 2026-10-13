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
from os_service.application.saga.orquestrador import SagaOrquestrador

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
    """Estado da saga no banco do -os (models SagaInstance/ProcessedEvent)."""

    def _dto(self, instância):  # noqa: N805 — assinatura do Protocol
        return SagaInstanceStateDTO(
            os_id=instância.os_id,
            status=SagaStatus(instância.status),
            passo_atual=(
                SagaStep(instância.passo_atual) if instância.passo_atual else None
            ),
            passos_concluidos=tuple(SagaStep(p) for p in instância.passos_concluidos),
            compensation_pendente=(
                SagaStep(instância.compensation_pendente)
                if instância.compensation_pendente
                else None
            ),
            iniciado_em=instância.iniciado_em,
            atualizado_em=instância.atualizado_em,
        )

    def criar(self, os_id, passo_atual):
        from os_service.models import SagaInstance

        instancia, _ = SagaInstance.objects.get_or_create(
            os_id=os_id,
            defaults={"status": SagaStatus.EM_ANDAMENTO.value, "passo_atual": passo_atual.value},
        )
        return self._dto(instancia)

    def get_by_os(self, os_id):
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.filter(os_id=os_id).first()
        return self._dto(instancia) if instancia else None

    def avancar(self, os_id, novo_passo):
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(os_id=os_id)
        instancia.avancar(novo_passo.value)
        instancia.save()
        return self._dto(instancia)

    def iniciar_compensacao(self, os_id):
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(os_id=os_id)
        instancia.status = SagaStatus.COMPENSANDO.value
        instancia.save()
        return self._dto(instancia)

    def marcar_compensada(self, os_id):
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(os_id=os_id)
        instancia.status = SagaStatus.COMPENSADA.value
        instancia.passo_atual = None
        instancia.save()
        return self._dto(instancia)

    def marcar_concluida(self, os_id):
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(os_id=os_id)
        instancia.status = SagaStatus.CONCLUIDA.value
        instancia.passo_atual = None
        instancia.save()
        return self._dto(instancia)

    def marcar_falha(self, os_id, motivo):
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(os_id=os_id)
        instancia.status = SagaStatus.FALHA.value
        instancia.passo_atual = None
        instancia.falha_motivo = motivo
        instancia.save()
        return self._dto(instancia)


def build_saga_orquestrador() -> SagaOrquestrador:
    """Monta orquestrador da saga com estado Django e publisher local/SQS."""
    from django.conf import settings as dj_settings

    if getattr(dj_settings, "SAGA_QUEUE_URL", None):
        import boto3

        publisher: SagaPublisherPort = SQSSagaPublisher(
            queue_url=dj_settings.SAGA_QUEUE_URL,
            client=boto3.client("sqs"),
        )
    else:
        publisher = InMemorySagaPublisher()

    return SagaOrquestrador(
        state_repository=DjangoSagaStateRepository(),
        publisher=publisher,
    )

