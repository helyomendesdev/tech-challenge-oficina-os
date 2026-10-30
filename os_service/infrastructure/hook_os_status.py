"""Hook OS <-> saga v1: transições da saga ancoram no aggregate da OS.

Mapeamento (grupo, Q2): saga CANCELADA -> OS CANCELADA; nova saga só em
nova abertura. Substitui o legado (timeout recolocava a OS em
DIAGNOSTICO — decisão v1.1 se o grupo quiser reabertura sem nova OS).
"""
import logging

from django.utils import timezone

from os_service.application.saga import dto
from os_service.domain.enums import StatusOrdemServico

logger = logging.getLogger("os_service.saga")

# Estado novo da saga -> (status alvo da OS, campo de data a marcar)
_MAPA = {
    dto.EstadoSaga.AGUARDANDO_APROVACAO: StatusOrdemServico.AGUARDANDO,
    dto.EstadoSaga.EXECUCAO_SOLICITADA: StatusOrdemServico.EXECUCAO,
    dto.EstadoSaga.EM_EXECUCAO: StatusOrdemServico.EXECUCAO,
    dto.EstadoSaga.CONCLUIDA: StatusOrdemServico.FINALIZADA,
    dto.EstadoSaga.CANCELADA: (
        StatusOrdemServico.CANCELADA
    ),
}
_CAMPO_POR_ESTADO = {
    dto.EstadoSaga.EXECUCAO_SOLICITADA: "data_inicio_execucao",
    dto.EstadoSaga.EM_EXECUCAO: "data_inicio_execucao",
    dto.EstadoSaga.CONCLUIDA: "data_finalizacao",
}


class OSStatusPort:
    """Protocolo do hook pós-transição no consumidor da saga."""

    def apos_transicao(self, saga_id: str, os_id: int, estado) -> None:
        """Chamado com o estado NOVO da saga após processar a mensagem."""
        ...


class DjangoOSStatusPort(OSStatusPort):
    """Atualiza OrdemServico a partir do estado novo da saga (idempotente)."""

    def apos_transicao(self, saga_id: str, os_id: int, estado) -> None:
        from os_service.models import OrdemServico

        if _MAPA.get(estado) is None:
            return  # INICIADA/PAG.SOLICITADO/COMPENSANDO/ORC.SOLICITADO
        status_alvo = _MAPA[estado]

        instancia = OrdemServico.objects.filter(pk=os_id).first()
        if instancia is None:
            logger.warning(
                "saga_v1_hook_os_ausente",
                extra={"saga_id": saga_id, "os_id": os_id},
            )
            return

        agora = timezone.now()
        campos: list[str] = []
        campo_data = _CAMPO_POR_ESTADO.get(estado)
        if campo_data is not None and getattr(instancia, campo_data) is None:
            setattr(instancia, campo_data, agora)
            campos.append(campo_data)
        if instancia.status != status_alvo.value:
            instancia.status = status_alvo.value
            instancia.data_ultima_transicao = agora
            campos.extend(["status", "data_ultima_transicao"])

        if not campos:
            return  # idempotente: repetição não regrava datas nem status
        instancia.save(update_fields=campos)
        logger.info(
            "saga_v1_os_status_ancorado",
            extra={"saga_id": saga_id, "os_id": os_id,
                   "estado": estado.value, "status": status_alvo.value},
        )
