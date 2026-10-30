"""Consumidor de respostas da Saga v1 (Billing/Execution → orquestrador).

- parse do body SQS (envelope do contrato, camelCase);
- dedupe por messageId (ProcessedEvent — at-least-once da fila);
- delega a transição ao orquestrador v1 (registra-e-descarta já lá);
- contexto W3C dos MessageAttributes aceito antes de processar
  (tracing/propagacao-sqs.md do -saga; aceitar_recebido nunca levanta).
"""
import json
import logging

from os_service.application.saga import dto

logger = logging.getLogger("os_service.saga")


class ConsumidorRespostasSaga:
    """Transforma mensagem SQS de resposta em transição do orquestrador."""

    def __init__(self, orquestrador, eventos_processados=None,
                 os_status_port=None):
        self.orquestrador = orquestrador
        self.eventos_processados = eventos_processados
        self.os_status_port = os_status_port

    def processar(self, corpo, message_attributes=None) -> dict:
        """Processa uma mensagem recebida; não levanta para saga ausente.

        Retorna {"resultado": processado|duplicado|ignorada, "messageId",
        "estado", "trace"}; "trace" só com traceparent válido nos atributos.
        """
        attributes = message_attributes or {}
        trace = self._aceitar_contexto(attributes)
        try:
            dados = corpo if isinstance(corpo, dict) else json.loads(corpo)
            msg = dto.deserializar(dados)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Body não é um envelope v1 válido: {exc}") from exc

        if self.eventos_processados is not None:
            if not self.eventos_processados.marcar(msg):
                logger.info(
                    "saga_v1_resposta_duplicada",
                    extra={"message_id": msg.message_id, "type": msg.type},
                )
                return {"resultado": "duplicado", "messageId": msg.message_id,
                        "estado": None, "trace": trace}

        try:
            estado = self.orquestrador.processar(msg)
        except dto.SagaNaoEncontradaError:
            logger.warning(
                "saga_v1_resposta_sem_saga",
                extra={"message_id": msg.message_id, "saga_id": msg.saga_id,
                       "type": msg.type},
            )
            return {"resultado": "ignorada", "messageId": msg.message_id,
                    "estado": None, "trace": trace}

        self._ancorar_na_os(saga_id=msg.saga_id, os_id=msg.os_id,
                            estado=estado.estado)
        return {"resultado": "processado", "messageId": msg.message_id,
                "estado": estado.estado.value, "trace": trace}

    def _ancorar_na_os(self, saga_id: str, os_id: int, estado) -> None:
        """Hook pós-transição: a saga muda, o aggregate da OS acompanha.

        Quebra de hook NÃO reverte a transição (só loga) — mesmo princípio
        da abertura (PR #5): falha de saga não reverte o negócio.
        """
        if self.os_status_port is None:
            return
        try:
            self.os_status_port.apos_transicao(
                saga_id=saga_id, os_id=os_id, estado=estado
            )
        except Exception:  # noqa: BLE001 — hook nunca bloqueia a saga
            logger.exception(
                "saga_v1_hook_os_falhou",
                extra={"saga_id": saga_id, "os_id": os_id,
                       "estado": estado.value},
            )

    @staticmethod
    def _aceitar_contexto(attributes) -> dict:
        """W3C dos MessageAttributes; sem pai válido abre trace próprio."""
        try:
            from oficina_saga.newrelic_adapter import aceitar_recebido
            from oficina_saga.rastreio import extrair_recebido, parse_traceparent

            traceparent, _tracestate = extrair_recebido(attributes)
            if traceparent is None:
                return {}
            aceitar_recebido(attributes)  # agente NR (ou none) aceita aqui
            parsed = parse_traceparent(traceparent)
            if parsed is None:
                return {}
            return {"trace_id": parsed.trace_id, "span_id": parsed.parent_id}
        except Exception:  # tracing nunca bloqueia o fluxo da saga
            logger.debug("saga_v1_trace_contexto_recusado", exc_info=True)
            return {}
