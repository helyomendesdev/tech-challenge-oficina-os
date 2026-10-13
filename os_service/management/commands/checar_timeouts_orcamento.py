"""Management command: checar timeouts de orçamento e compensar.

Agendar (quando infra AWS subir): cron no EKS / EventBridge a cada 5 min.
Run local: python manage.py checar_timeouts_orcamento
"""
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from os_service.application.saga.timeout_orcamento import TimeoutOrcamentoService
from os_service.infrastructure.saga_adapters import build_saga_orquestrador
from os_service.infrastructure.saga_timeout_adapter import (
    DjangoOrdemServicoTimeoutAdapter,
)


class Command(BaseCommand):
    help = "Cancela orçamentos AGUARDANDO expirados e compensa a saga."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Lista o que seria compensado sem alterar dados",
        )

    def handle(self, *args, **options):
        adapter = DjangoOrdemServicoTimeoutAdapter()
        orquestrador = build_saga_orquestrador()
        service = TimeoutOrcamentoService(orquestrador, adapter)

        if options["dry_run"]:
            from datetime import timedelta

            limite = datetime.now(timezone.utc) - timedelta(minutes=30)
            expiradas = adapter.listar_aguardando_antes_de(limite)
            self.stdout.write(f"[dry-run] {len(expiradas)} OS expiradas: {expiradas}")
            return

        resumo = service.executar()
        self.stdout.write(f"Timeouts de orçamento: {resumo}")
