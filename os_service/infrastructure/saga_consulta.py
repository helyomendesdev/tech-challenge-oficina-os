"""Leitura das Sagas de uma OS no formato ConsultaSaga do contrato.

Contrato: contrato/os-consulta-saga.openapi.yaml do -saga (componentes
ConsultaSaga, Saga e MensagemDaSaga). Somente leitura: o historico vem de
SagaMensagem, preenchida pelo orquestrador (ENVIADA) e pelo consumidor
(RECEBIDA).
"""
from datetime import timezone

from os_service.application.saga.contrato_v1 import CONTRATO_SAGA_ESTADOS_V1
from os_service.models import SagaInstance, SagaMensagem


def _iso(instante) -> str:
    """UTC com sufixo Z, como no contrato."""
    return instante.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _mensagem(registro: SagaMensagem) -> dict:
    return {
        "type": registro.type,
        "messageId": str(registro.message_id),
        "causationId": str(registro.causation_id) if registro.causation_id else None,
        "occurredAt": _iso(registro.occurred_at),
        "direcao": registro.direcao,
        "requestId": registro.request_id,
        "traceId": registro.trace_id,
    }


def _saga(instancia: SagaInstance, mensagens: list[SagaMensagem]) -> dict:
    historico = [_mensagem(m) for m in mensagens]
    saga = {
        "sagaId": str(instancia.saga_id),
        "estado": instancia.estado,
        "statusOs": CONTRATO_SAGA_ESTADOS_V1["status_os"][instancia.estado],
        "iniciadaEm": _iso(instancia.iniciado_em),
        # instante da ultima mensagem registrada; sem mensagem, a abertura
        "atualizadaEm": (
            historico[-1]["occurredAt"] if historico else _iso(instancia.iniciado_em)
        ),
        "historico": historico,
    }
    if orcamento_id := (instancia.referencias or {}).get("orcamentoId"):
        saga["orcamentoId"] = str(orcamento_id)
    return saga


def consultar_sagas_da_os(os_id: int) -> dict | None:
    """ConsultaSaga da OS; None se a OS nao tem Saga.

    `sagas` da mais antiga para a mais recente; `sagaAtual` e a mais recente.
    """
    instancias = list(
        SagaInstance.objects.filter(os_id=os_id).order_by("iniciado_em", "id")
    )
    if not instancias:
        return None
    por_saga: dict[str, list[SagaMensagem]] = {}
    for mensagem in SagaMensagem.objects.filter(
        saga_id__in=[i.saga_id for i in instancias]
    ).order_by("occurred_at", "id"):
        por_saga.setdefault(str(mensagem.saga_id), []).append(mensagem)
    sagas = [_saga(i, por_saga.get(str(i.saga_id), [])) for i in instancias]
    return {"osId": os_id, "sagaAtual": sagas[-1]["sagaId"], "sagas": sagas}
