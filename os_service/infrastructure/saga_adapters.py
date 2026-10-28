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

from os_service.application.saga import dto
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
            # Fila FIFO: sem isso o send é recusado (ou dedupado por body
            # se ContentBasedDeduplication estiver ligado). O id de dedupe
            # é o messageId do envelope.
            MessageDeduplicationId=event.event_id,
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


# ----------------------------------------------------------------------
# Contrato v1 (-saga PR #2)
# ----------------------------------------------------------------------
class DjangoSagaStateRepositoryV1:
    """Estado da saga v1 no banco do -os (SagaInstance.saga_id)."""

    def criar(self, saga_id: str, os_id: int, estado_inicial) -> dto.SagaV1StateDTO:
        from os_service.models import SagaInstance

        instancia, _ = SagaInstance.objects.get_or_create(
            saga_id=saga_id,
            defaults={
                "os_id": os_id,
                "estado": estado_inicial.value,
                "status": dto.StatusSaga.EM_ANDAMENTO.value,
            },
        )
        return self._dto(instancia)

    def get(self, saga_id: str) -> dto.SagaV1StateDTO | None:
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.filter(saga_id=saga_id).first()
        return self._dto(instancia) if instancia else None

    def salvar_estado(self, saga_id: str, novo_estado) -> dto.SagaV1StateDTO:
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(saga_id=saga_id)
        instancia.estado = novo_estado.value
        if novo_estado is dto.EstadoSaga.CONCLUIDA:
            instancia.status = dto.StatusSaga.CONCLUIDA.value
        elif novo_estado is dto.EstadoSaga.CANCELADA:
            instancia.status = dto.StatusSaga.FALHA.value
        instancia.save()
        return self._dto(instancia)

    def salvar_motivo(self, saga_id: str, motivo: str) -> None:
        from os_service.models import SagaInstance

        SagaInstance.objects.filter(saga_id=saga_id).update(motivo=motivo)

    def _dto(self, instancia) -> dto.SagaV1StateDTO:
        return dto.SagaV1StateDTO(
            saga_id=str(instancia.saga_id),
            os_id=instancia.os_id,
            status=dto.StatusSaga(instancia.status),
            estado=dto.EstadoSaga(instancia.estado),
            motivo=instancia.motivo,
        )


class SQSSagaPublisherV1:
    """Publisher SQS do envelope do contrato v1 (fila FIFO)."""

    def __init__(self, queue_url: str, client):
        self.queue_url = queue_url
        self.client = client

    def publish(self, msg: dto.MensagemSaga) -> dict:
        from django.conf import settings as dj_settings

        from os_service.infrastructure.saga_adapters import os_id_group

        from oficina_saga import newrelic_adapter

        if not self.queue_url:
            raise RuntimeError("SAGA_QUEUE_URL não configurada")
        envelope = dto.envelope(msg)
        # traceparent/tracestate vão em MessageAttributes (tracing/propagacao-sqs.md
        # do -saga); com o agente ativo, o trace da requisição continua na fila.
        newrelic_adapter.registrar_mensagem(envelope)
        response = self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(envelope, ensure_ascii=False),
            MessageAttributes=newrelic_adapter.atributos_para_envio(),
            MessageGroupId=os_id_group(msg.os_id),
            MessageDeduplicationId=msg.message_id,
        )
        logger.info(
            "saga_v1_comando_publicado",
            extra={
                "message_id": msg.message_id,
                "type": msg.type,
                "saga_id": msg.saga_id,
                "os_id": msg.os_id,
            },
        )
        return response


class InMemorySagaPublisherV1:
    """Publisher de teste/local do contrato v1."""

    def __init__(self):
        self.publicados: list[dto.MensagemSaga] = []

    def publish(self, msg: dto.MensagemSaga) -> dict:
        self.publicados.append(msg)
        return {"MessageId": msg.message_id}


def build_saga_orquestrador_v1() -> "SagaOrquestradorV1":
    """Monta o orquestrador v1 (máquina do contrato) com estado Django."""
    from django.conf import settings as dj_settings

    from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1

    if getattr(dj_settings, "SAGA_QUEUE_URL", None):
        import boto3

        publisher = SQSSagaPublisherV1(
            queue_url=dj_settings.SAGA_QUEUE_URL,
            client=boto3.client("sqs"),
        )
    else:
        publisher = InMemorySagaPublisherV1()

    return SagaOrquestradorV1(
        state_repository=DjangoSagaStateRepositoryV1(),
        publisher=publisher,
    )

