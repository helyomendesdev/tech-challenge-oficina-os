"""Management command: checar timeouts de aprovação e compensar (saga v1).

Gatilho externo `timeout` do contrato (ADR-003 do -saga): saga em
AGUARDANDO_APROVACAO há mais de ORCAMENTO_TIMEOUT_MINUTOS vai para
COMPENSANDO e manda CancelarOrcamento com motivo TIMEOUT.

Agendar (quando infra AWS subir): cron no EKS / EventBridge a cada 5 min.
Run local: python manage.py checar_timeouts_orcamento
"""
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    build_saga_orquestrador_v1,
)


from django.conf import settings


def timeout_padrao() -> "timedelta":
    """Prazo do ADR-003 (configurável no -os; fora do contrato)."""
    from datetime import timedelta

    minutos = getattr(settings, "ORCAMENTO_TIMEOUT_MINUTOS", 30)
    return timedelta(minutes=minutos)


class Command(BaseCommand):
    help = "Expira sagas em AGUARDANDO_APROVACAO além do prazo e compensa."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Lista o que seria expirado sem alterar dados",
        )

    def handle(self, *args, **options):
        limite = datetime.now(timezone.utc) - timeout_padrao()

        if options["dry_run"]:
            vencidas = DjangoSagaStateRepositoryV1().listar_aguardando_aprovacao_antes_de(limite)
            self.stdout.write(f"[dry-run] {len(vencidas)} sagas vencidas: {vencidas}")
            return

        expiradas = build_saga_orquestrador_v1().expirar_vencidas(limite)
        self.stdout.write(f"Timeouts de aprovação: {len(expiradas)} sagas expiradas")
