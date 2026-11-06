"""Adapters de infraestrutura da Saga v1.

- DjangoSagaStateRepositoryV1: estado da saga no banco do próprio -os.
- SQSSagaPublisherV1: publica comandos nas filas por destinatário (boto3),
  envelope do contrato v1, MessageGroupId=sagaId, dedupe por messageId e
  trace W3C (issue #17: uma fila de comandos por serviço, como no AsyncAPI).
- InMemorySagaPublisherV1: publisher de teste/local.

SQS entra de fato quando as filas estão configuradas
(SAGA_BILLING_QUEUE_URL/SAGA_EXECUTION_QUEUE_URL; SAGA_QUEUE_URL é legado
de fila única, replicado para os dois destinos com aviso de depreciação).
endpoints locais usam o publisher fake dos testes.
"""
import json
import logging

import boto3

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
    """Publisher SQS do envelope do contrato v1 (fila FIFO por destinatário)."""

    def __init__(self, billing_queue_url: str, execution_queue_url: str, client):
        self.queue_urls = {
            "billing": billing_queue_url,
            "execution": execution_queue_url,
        }
        self.client = client

    def fila_de(self, tipo: str) -> str:
        """Fila do destinatário do comando (asyncapi.yaml, ADR-001; issue #17).

        Cada comando tem um único destinatário no contrato; tipo sem rota
        levanta ValueError (fail fast) em vez de ir para uma fila errada.
        """
        if tipo in ("GerarOrcamento", "SolicitarPagamento", "CancelarOrcamento", "EstornarPagamento"):
            destino, var = "billing", "SAGA_BILLING_QUEUE_URL"
        elif tipo in ("IniciarExecucao", "CancelarExecucao"):
            destino, var = "execution", "SAGA_EXECUTION_QUEUE_URL"
        else:
            raise ValueError(f"comando sem destinatário no contrato v1: {tipo}")
        url = self.queue_urls[destino]
        if not url:
            raise RuntimeError(f"{var} não configurada")
        return url

    def publish(self, msg: dto.MensagemSaga) -> dict:
        from oficina_saga import newrelic_adapter

        queue_url = self.fila_de(msg.type)
        envelope = dto.envelope(msg)
        # traceparent/tracestate vão em MessageAttributes (tracing/propagacao-sqs.md
        # do -saga); com o agente ativo, o trace da requisição continua na fila.
        newrelic_adapter.registrar_mensagem(envelope)
        # Um trace por Saga: o traceparent enviado e filho do da Saga.
        atributos = newrelic_adapter.atributos_para_envio(
            traceparent_atual=msg.traceparent
        )
        response = self.client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(envelope, ensure_ascii=False),
            MessageAttributes=atributos,
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
                "queue_url": queue_url,
            },
        )
        # `traceparent`: o que foi de fato para a fila (o orquestrador grava
        # o trace-id dele no historico).
        enviado = atributos.get("traceparent", {}).get("StringValue")
        base = response if isinstance(response, dict) else {}
        return {**base, "traceparent": enviado}


class InMemorySagaPublisherV1:
    """Publisher de teste/local do contrato v1."""

    def __init__(self):
        self.publicados: list[dto.MensagemSaga] = []

    def publish(self, msg: dto.MensagemSaga) -> dict:
        from oficina_saga import newrelic_adapter

        self.publicados.append(msg)
        atributos = newrelic_adapter.atributos_para_envio(
            traceparent_atual=msg.traceparent
        )
        return {
            "MessageId": msg.message_id,
            "traceparent": atributos["traceparent"]["StringValue"],
        }


class DjangoRegistroMensagensSaga:
    """Historico da consulta: grava SagaMensagem (idempotente por messageId)."""

    def registrar(self, msg: dto.MensagemSaga, direcao: str, trace_id=None) -> None:
        from uuid import uuid4

        from os_service.application.saga.orquestrador_v1 import trace_id_de
        from os_service.models import SagaInstance, SagaMensagem

        if not trace_id:
            # Sem trace na mensagem: o da Saga; sem ele, um novo (32 hex).
            instancia = SagaInstance.objects.filter(saga_id=msg.saga_id).first()
            salvo = (instancia.referencias or {}).get("traceparent") if instancia else None
            trace_id = trace_id_de(salvo) or uuid4().hex
        SagaMensagem.objects.get_or_create(
            message_id=msg.message_id,
            defaults={
                "saga_id": msg.saga_id,
                "type": msg.type,
                "causation_id": msg.causation_id,
                "occurred_at": msg.occurred_at,
                "direcao": direcao,
                "request_id": msg.request_id,
                "trace_id": trace_id,
            },
        )


def build_saga_orquestrador_v1() -> "SagaOrquestradorV1":
    """Monta o orquestrador v1 (máquina do contrato) com estado Django."""
    from django.conf import settings as dj_settings

    from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
    from os_service.infrastructure.carga_comando_saga import (
        DjangoCargaComandoSagaPort,
    )

    billing_url = getattr(dj_settings, "SAGA_BILLING_QUEUE_URL", "")
    execution_url = getattr(dj_settings, "SAGA_EXECUTION_QUEUE_URL", "")
    # Issue #17: transição da fila única legada. Sem as duas novas variáveis,
    # a SAGA_QUEUE_URL antiga replica para os dois destinos (comportamento
    # anterior mantido, com aviso) — rotação para as duas filas fica pendente
    # na configuração do ambiente.
    if not billing_url or not execution_url:
        legado = getattr(dj_settings, "SAGA_QUEUE_URL", "")
        if legado:
            billing_url = billing_url or legado
            execution_url = execution_url or legado
            logger.warning(
                "saga_fila_unica_legada",
                extra={
                    "aviso": "SAGA_QUEUE_URL única usada para billing e execution; "
                    "configurar SAGA_BILLING_QUEUE_URL e SAGA_EXECUTION_QUEUE_URL",
                },
            )

    if billing_url and execution_url:
        publisher = SQSSagaPublisherV1(
            billing_queue_url=billing_url,
            execution_queue_url=execution_url,
            client=boto3.client("sqs"),
        )
    else:
        publisher = InMemorySagaPublisherV1()

    return SagaOrquestradorV1(
        state_repository=DjangoSagaStateRepositoryV1(),
        publisher=publisher,
        registro_mensagens=DjangoRegistroMensagensSaga(),
        # Payloads completos do contrato (valorTotalCentavos/servicos/pecas):
        # a carga consulta o aggregate no momento da transição. O gate do
        # itemPecaOSId segue o ADR-004 (só depois da -execution re-vendar).
        carga_port=DjangoCargaComandoSagaPort(
            com_item_peca_os_id=bool(
                getattr(dj_settings, "SAGA_ENVIA_ITEM_PECA_OS_ID", False)
            )
        ),
    )

