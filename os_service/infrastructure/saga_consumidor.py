"""Transporte do consumidor de respostas da Saga v1.

- SQSConsumidorRespostas: uma onda receive_message pedindo primeiro os
  MessageAttributeNames de trace (tracing/propagacao-sqs.md do -saga);
  processa e apaga a mensagem; erro de infra NAO apaga (redelivery);
- DjangoEventosProcessados: dedupe por messageId na ProcessedEvent do -os
  (get-or-create com IntegrityError = segunda vez);
- build_consumidor_respostas: fio com o orquestrador v1; completa com a
  fila real de respostas (SAGA_RESPONSE_QUEUE_URL).
"""
import logging

from django.db import IntegrityError

from os_service.application.saga import dto
from os_service.application.saga.consumidor import ConsumidorRespostasSaga

logger = logging.getLogger("os_service.saga")

# Contrato de tracing: os unicos atributos que viajam na fila levam W3C.
ATRIBUTOS_DE_TRACE = ["traceparent", "tracestate"]


class DjangoEventosProcessados:
    """Dedupe por messageId: devolve True se for a primeira vez."""

    def marcar(self, msg: dto.MensagemSaga) -> bool:
        from django.db import IntegrityError, transaction

        from os_service.models import ProcessedEvent

        try:
            # savepoint: UNIQUE falha em repetida não suja a transação
            # externa (TestCase/worker vive dentro de um atomic maior)
            with transaction.atomic():
                ProcessedEvent.objects.create(
                    event_id=msg.message_id,
                    event_type=msg.type,
                    os_id=msg.os_id,
                    body={"sagaId": msg.saga_id, "payload": msg.payload},
                )
            return True
        except IntegrityError:
            return False


class SQSConsumidorRespostas:
    """Recebe uma onda SQS, processa cada mensagem e apaga as encerradas."""

    def __init__(self, queue_url, client, consumidor,
                 max_messages=10, wait_seconds=10):
        self.queue_url = queue_url
        self.client = client
        self.consumidor = consumidor
        self.max_messages = min(int(max_messages), 10)
        self.wait_seconds = int(wait_seconds)

    def recebe_onda(self) -> dict:
        """Um receive_message: parse/processa/apaga. Resume por tipo."""
        if not self.queue_url:
            raise RuntimeError("SAGA_RESPONSE_QUEUE_URL não configurada")
        resposta = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=self.max_messages,
            WaitTimeSeconds=self.wait_seconds,
            MessageAttributeNames=ATRIBUTOS_DE_TRACE,
        )
        mensagens = resposta.get("Messages", [])
        resumo = {"recebidos": len(mensagens), "processados": 0,
                  "ignoradas": 0, "duplicados": 0, "invalidas": 0, "erros": 0}
        for mensagem in mensagens:
            tipo_saido = self._uma(mensagem)
            chave = {"processado": "processados", "ignorada": "ignoradas",
                     "duplicado": "duplicados", "invalida": "invalidas",
                     "erro": "erros"}[tipo_saido]
            resumo[chave] += 1
        return resumo

    def _uma(self, mensagem) -> str:
        receipt = mensagem.get("ReceiptHandle", "")
        attributes = mensagem.get("MessageAttributes") or {}
        try:
            resultado = self.consumidor.processar(
                mensagem.get("Body", ""), attributes
            )
        except ValueError:
            logger.warning(
                "saga_v1_mensagem_invalida",
                extra={"receipt": receipt[:8] if receipt else None},
            )
            self._apagar(receipt)
            return "invalida"
        except Exception:
            logger.exception(
                "saga_v1_erro_no_processamento"
            )  # sem apagar: visibilidade devolve a mensagem (redelivery)
            return "erro"
        self._apagar(receipt)
        return resultado["resultado"]

    def _apagar(self, receipt) -> None:
        if receipt:
            self.client.delete_message(
                QueueUrl=self.queue_url, ReceiptHandle=receipt
            )


def build_consumidor_respostas() -> SQSConsumidorRespostas:
    """Fio completo: fila de respostas SQS → consumidor → orquestrador v1."""
    from django.conf import settings as dj_settings

    from os_service.infrastructure.saga_adapters import (
        build_saga_orquestrador_v1,
    )

    queue_url = getattr(dj_settings, "SAGA_RESPONSE_QUEUE_URL", "") or ""
    if not queue_url:
        raise RuntimeError("SAGA_RESPONSE_QUEUE_URL não configurada")
    import boto3

    return SQSConsumidorRespostas(
        queue_url=queue_url,
        client=boto3.client("sqs"),
        consumidor=ConsumidorRespostasSaga(
            orquestrador=build_saga_orquestrador_v1(),
            eventos_processados=DjangoEventosProcessados(),
        ),
    )
