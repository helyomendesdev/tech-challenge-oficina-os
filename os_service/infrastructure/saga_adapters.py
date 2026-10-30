"""Adapters de infraestrutura da Saga v1.

- DjangoSagaStateRepositoryV1: estado da saga no banco do próprio -os.
- SQSSagaPublisherV1: publica comandos no barramento (boto3), envelope do
  contrato v1, MessageGroupId=sagaId, dedupe por messageId e trace W3C.
- InMemorySagaPublisherV1: publisher de teste/local.

SQS entra de fato quando Sophia subir a infra (SAGA_QUEUE_URL); endpoints
locais usam o publisher fake dos testes.
"""
import json
import logging

from os_service.application.saga import dto

logger = logging.getLogger("os_service.saga")


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
        from django.utils import timezone

        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(saga_id=saga_id)
        instancia.estado = novo_estado.value
        instancia.estado_desde = timezone.now()
        if novo_estado is dto.EstadoSaga.CONCLUIDA:
            instancia.status = dto.StatusSaga.CONCLUIDA.value
        elif novo_estado is dto.EstadoSaga.CANCELADA:
            instancia.status = dto.StatusSaga.FALHA.value
        instancia.save()
        return self._dto(instancia)

    def salvar_motivo(self, saga_id: str, motivo: str) -> None:
        from os_service.models import SagaInstance

        SagaInstance.objects.filter(saga_id=saga_id).update(motivo=motivo)

    def salvar_referencias(self, saga_id: str, referencias: dict) -> None:
        from os_service.models import SagaInstance

        instancia = SagaInstance.objects.get(saga_id=saga_id)
        instancia.referencias = {**(instancia.referencias or {}), **referencias}
        instancia.save(update_fields=["referencias"])

    def listar_aguardando_aprovacao_antes_de(self, limite) -> list[str]:
        from os_service.models import SagaInstance

        return [
            str(saga_id)
            for saga_id in SagaInstance.objects.filter(
                estado=dto.EstadoSaga.AGUARDANDO_APROVACAO.value,
                estado_desde__lt=limite,
            ).values_list("saga_id", flat=True)
        ]

    def _dto(self, instancia) -> dto.SagaV1StateDTO:
        return dto.SagaV1StateDTO(
            saga_id=str(instancia.saga_id),
            os_id=instancia.os_id,
            status=dto.StatusSaga(instancia.status),
            estado=dto.EstadoSaga(instancia.estado),
            motivo=instancia.motivo,
            referencias=dict(instancia.referencias or {}),
        )


class SQSSagaPublisherV1:
    """Publisher SQS do envelope do contrato v1 (fila FIFO)."""

    def __init__(self, queue_url: str, client):
        self.queue_url = queue_url
        self.client = client

    def publish(self, msg: dto.MensagemSaga) -> dict:
        from django.conf import settings as dj_settings

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
            # Contrato v1 (asyncapi.yaml, ADR-001): ordem por Saga, não por OS.
            MessageGroupId=msg.saga_id,
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

