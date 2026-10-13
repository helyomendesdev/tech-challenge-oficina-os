"""Timeout de orçamento — compensação automática (contrato, seção 8).

Regra: OS que ficou AGUARDANDO por mais de ORCAMENTO_TIMEOUT_MINUTOS sem
decisão do cliente sai da fila com compensação (OrcamentoCancelado + OS de
volta pra DIAGNOSTICO como "expirado"), para não travar o fluxo da saga.

Execução: management command `checar_timeouts_orcamento` (cron/celery beat/
ECS task — quando a Sophia subir a infra, agendamos no EKS).
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from django.conf import settings

from os_service.application.saga.dto import (
    COMPENSACOES,
    SagaEventDTO,
    SagaEventType,
    SagaStep,
)
from os_service.application.saga.orquestrador import SagaOrquestrador
from os_service.domain.enums import StatusOrdemServico


def timeout_padrao() -> timedelta:
    minutos = getattr(settings, "ORCAMENTO_TIMEOUT_MINUTOS", 30)
    return timedelta(minutes=minutos)


class TimeoutOrcamentoService:
    """Marca OS AGUARDANDO expiradas e dispara compensação na saga."""

    def __init__(self, orquestrador: SagaOrquestrador, resta_port: Any):
        self.orquestrador = orquestrador
        self.resta_port = resta_port  # consulta/agregação no banco do -os

    def executar(self, agora: datetime | None = None) -> dict[str, int]:
        """Processa todos os aguardando expirados. Retorna resumo."""
        if agora is None:
            agora = datetime.now(timezone.utc)
        limite = agora - timeout_padrao()

        expiradas = self.resta_port.listar_aguardando_antes_de(limite)

        resumo = {"expiradas": 0, "compensadas": 0, "erro": 0}
        for os_id, valor_total in expiradas:
            try:
                self.orquestrador.compensar(
                    os_id, motivo="timeout de orçamento (30 min)"
                )
                self.resta_port.marcar_orcamento_expirado(os_id)
                resumo["compensadas"] += 1
            except Exception:
                import logging

                logging.getLogger("os_service.saga").exception(
                    "saga_timeout_compensacao_falhou", extra={"os_id": os_id}
                )
                resumo["erro"] += 1
            resumo["expiradas"] += 1
        return resumo
