"""Drenagem da outbox da Saga para as filas por destinatario (ADR-001).

O orquestrador grava o comando na tabela saga_comando na mesma transição
da transição (OutboxSagaPublisherV1). Este comando é o drain: pega os
comandos não publicados, publica via SQSSagaPublisherV1 (fila por
destinatário, MessageGroupId=sagaId, dedupe por messageId, trace W3C) e
marca publicado_em. Falha de fila: para a passada, sem marcar — a
passada seguinte repete (idempotente no SQS, MessageDeduplicationId).
"""
import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger("os_service.saga")

TAMANHO_DO_LOTE = 50


class Command(BaseCommand):
    help = (
        "Drena a outbox da Saga (saga_comando) para as filas por "
        "destinatário; --loop mantém o processo vivo entre passadas."
    )

    def handle(self, *args, **options):
        intervalo = int(options["intervalo"])
        while True:
            publicados = self._passada()
            if not options["loop"]:
                self.stdout.write(f"{publicados} comando(s) publicad(s)")
                return
            if publicados == 0:
                time.sleep(intervalo)

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Roda em laço contínuo (intervalo fixo com a fila vazia).",
        )
        parser.add_argument(
            "--intervalo",
            type=int,
            default=10,
            help="Segundos de espera em --loop com a outbox vazia.",
        )

    def _passada(self) -> int:
        from django.conf import settings as dj_settings

        from os_service.infrastructure.saga_adapters import SQSSagaPublisherV1
        from os_service.models import SagaComando

        billing_url = getattr(dj_settings, "SAGA_BILLING_QUEUE_URL", "")
        execution_url = getattr(dj_settings, "SAGA_EXECUTION_QUEUE_URL", "")
        legado = getattr(dj_settings, "SAGA_QUEUE_URL", "")
        if not billing_url or not execution_url:
            if legado:
                billing_url = billing_url or legado
                execution_url = execution_url or legado
                logger.warning(
                    "saga_fila_unica_legada",
                    extra={"aviso": "drain com fila única; configurar "
                            "SAGA_BILLING_QUEUE_URL e SAGA_EXECUTION_QUEUE_URL"},
                )
        if not billing_url or not execution_url:
            raise RuntimeError(
                "SAGA_BILLING_QUEUE_URL/SAGA_EXECUTION_QUEUE_URL não "
                "configuradas: drain não tem filas para publicar"
            )
        import boto3

        publisher = SQSSagaPublisherV1(
            billing_queue_url=billing_url,
            execution_queue_url=execution_url,
            client=boto3.client("sqs"),
        )

        from django.utils import timezone

        publicados = 0
        while True:
            lote = list(
                SagaComando.objects.filter(publicado_em__isnull=True)
                .order_by("id")[:TAMANHO_DO_LOTE]
            )
            for comando in lote:
                # dedupe da fila aceita re-envio; só marca o que saiu
                publisher.publish(comando.para_mensagem())
                comando.publicado_em = timezone.now()
                comando.save(update_fields=["publicado_em"])
                publicados += 1
            if len(lote) < TAMANHO_DO_LOTE:
                return publicados
