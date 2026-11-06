"""Orquestrador da Saga v1 — aplica a máquina de estados do contrato (-saga PR #2).

Diferenças do rascunho (orquestrador.py, legado):
- Estado vem da máquina (saga-estados.yaml vendida em contrato_v1.py), não
  de SEQUENCIA/EVENTO_CONCLUSAO hardcoded.
- Comandos, não só reação: iniciar() manda GerarOrcamento; as transições
  mandam SolicitarPagamento/IniciarExecucao; falhas disparam compensação.
- Chave é sagaId (UUID, gerado aqui) — uma OS pode ter várias sagas.
- causationId = messageId da mensagem que causou o comando; nulo só em
  gatilho externo (inicio, cancelamento, timeout).
- Compensação herda o motivo do passo que abriu a trilha (contrato,
  `motivo: herdado`).
- Resposta fora do estado atual: registra e descarta (não levanta).

Este módulo contém só a lógica de decisão (camada de aplicação). Transporte
e persistência entram por ports — adapters em infrastructure/.
"""
import logging
from datetime import datetime
from uuid import uuid4

from os_service.application.saga import dto
from os_service.application.saga.contrato_v1 import CONTRATO_SAGA_ESTADOS_V1

logger = logging.getLogger("os_service.saga")


def trace_id_de(traceparent) -> str | None:
    """trace-id (32 hex) de um traceparent W3C; None se invalido ou ausente."""
    from oficina_saga.rastreio import parse_traceparent

    parsed = parse_traceparent(traceparent)
    return parsed.trace_id if parsed else None


def _traceparent_da_abertura() -> str:
    """Traceparent da Saga: o da requisicao se o agente estiver ativo, senao novo."""
    from oficina_saga import newrelic_adapter

    atributos = newrelic_adapter.atributos_para_envio()
    return atributos["traceparent"]["StringValue"]

# Gatilhos externos da máquina (não são mensagens de serviço)
GATILHOS = ("inicio", "cancelamento", "timeout")

# Id que cada comando de compensação exige (schemas de comandos do contrato);
# guardado na saga quando a resposta do passo chega.
ID_DA_COMPENSACAO = {
    "CancelarOrcamento": "orcamentoId",
    "EstornarPagamento": "pagamentoId",
    "CancelarExecucao": "execucaoId",
}

# Carga extra do aggregate da OS por comando (só o que o schema aceita)
CHAVES_DE_CARGA = {
    "SolicitarPagamento": ("valorTotalCentavos",),
    "IniciarExecucao": ("servicos", "pecas"),
}

# Campos que o schema do comando aceita (payload com additionalProperties: false
# em contrato/schemas/comandos/). O laco que herda ids da mensagem causadora
# trazia o pagamentoId do PagamentoConfirmado para o IniciarExecucao, e o
# Execution recusa o comando inteiro.
CAMPOS_DO_PAYLOAD = {
    "SolicitarPagamento": ("orcamentoId", "valorTotalCentavos"),
    "IniciarExecucao": ("orcamentoId", "servicos", "pecas"),
}


class SagaOrquestradorV1:
    """Decide transições da saga v1 a partir da máquina de estados."""

    def __init__(
        self,
        state_repository,
        publisher,
        clock=None,
        carga_port=None,
        registro_mensagens=None,
    ):
        self.state_repository = state_repository
        self.publisher = publisher
        # Port do historico da consulta (SagaMensagem); None = sem registro.
        self.registro_mensagens = registro_mensagens
        self.clock = clock or (lambda: datetime.now(datetime.now().astimezone().tzinfo))
        self.maquina = dto.MaquinaEstados(CONTRATO_SAGA_ESTADOS_V1)
        self.carga_port = carga_port

    # ------------------------------------------------------------------
    # Gatilho externo: início
    # ------------------------------------------------------------------
    def iniciar(self, os_id: int, request_id: str, payload: dict) -> str:
        """Abre a saga (INICIADA → ORCAMENTO_SOLICITADO) e manda GerarOrcamento.

        Retorna o sagaId da instância criada.
        """
        saga_id = str(uuid4())
        estado = self.state_repository.criar(
            saga_id, os_id, self.maquina.inicial
        )
        # Um trace por Saga: todo comando enviado herda este traceparent.
        self.state_repository.salvar_referencias(
            saga_id,
            {"requestId": request_id, "traceparent": _traceparent_da_abertura()},
        )
        self._aplicar_pos_transicao(
            estado_atual=self.maquina.inicial,
            mensagem="inicio",
            saga_id=saga_id,
            os_id=os_id,
            request_id=request_id,
            payload=payload,
        )
        return saga_id

    # ------------------------------------------------------------------
    # Processamento de resposta
    # ------------------------------------------------------------------
    def processar(self, msg: dto.MensagemSaga) -> dto.SagaV1StateDTO:
        """Aplica uma resposta do barramento à saga.

        Resposta fora do estado atual não muda o estado (contrato:
        registra e descarta) — retorna o estado corrente.
        """
        estado = self.state_repository.get(msg.saga_id)
        if estado is None:
            raise dto.SagaNaoEncontradaError(
                f"Saga {msg.saga_id} não encontrada"
            )

        if estado.estado in self.maquina.finais:
            return estado  # saga encerrada: registra e descarta

        try:
            alvo, comando, motivo = self.maquina.transicao(
                estado.estado, msg.type
            )
        except dto.TransicaoInvalidaError:
            return estado  # registra e descarta

        self.state_repository.salvar_estado(msg.saga_id, alvo)
        if motivo is not None and motivo != "herdado":
            self.state_repository.salvar_motivo(msg.saga_id, motivo)
        ids = {
            chave: valor
            for chave in ID_DA_COMPENSACAO.values()
            if (valor := (msg.payload or {}).get(chave))
        }
        if ids:
            self.state_repository.salvar_referencias(msg.saga_id, ids)
        # orcamentoId fica nas referências: SolicitarPagamento/IniciarExecucao
        # precisam dele depois (o PagamentoConfirmado não o traz).
        if orcamento_id := (msg.payload or {}).get("orcamentoId"):
            self.state_repository.salvar_referencias(
                msg.saga_id, {"orcamentoId": orcamento_id}
            )

        if comando is not None:
            self._enviar_comando(
                comando=comando,
                causa=msg,
                motivo=self._motivo_herdado(motivo, msg.saga_id),
            )
        return self.state_repository.get(msg.saga_id)

    # ------------------------------------------------------------------
    # Gatilho externo: cancelamento
    # ------------------------------------------------------------------
    def cancelar(self, saga_id: str, request_id: str) -> dto.SagaV1StateDTO:
        """Cliente pede cancelamento (gatilho externo `cancelamento`)."""
        estado = self.state_repository.get(saga_id)
        if estado is None:
            raise dto.SagaNaoEncontradaError(f"Saga {saga_id} não encontrada")
        if estado.estado in self.maquina.finais:
            return estado

        try:
            alvo, comando, motivo = self.maquina.transicao(
                estado.estado, "cancelamento"
            )
        except dto.TransicaoInvalidaError:
            return estado  # estado não aceita cancelamento: descarta

        self.state_repository.salvar_estado(saga_id, alvo)
        self.state_repository.salvar_motivo(saga_id, motivo)
        if comando is not None:
            self._enviar_comando(
                comando=comando,
                causa=None,
                motivo=motivo,
                saga_id=saga_id,
                os_id=estado.os_id,
                request_id=request_id,
            )
        return self.state_repository.get(saga_id)

    # ------------------------------------------------------------------
    # Gatilho externo: timeout de aprovação (ADR-003 do -saga)
    # ------------------------------------------------------------------
    def expirar(self, saga_id: str) -> dto.SagaV1StateDTO:
        """Orçamento sem resposta além do prazo (gatilho externo `timeout`).

        O comando leva o requestId da abertura da saga: não há requisição
        HTTP por trás do agendamento.
        """
        estado = self.state_repository.get(saga_id)
        if estado is None:
            raise dto.SagaNaoEncontradaError(f"Saga {saga_id} não encontrada")
        if estado.estado in self.maquina.finais:
            return estado

        try:
            alvo, comando, motivo = self.maquina.transicao(estado.estado, "timeout")
        except dto.TransicaoInvalidaError:
            return estado  # já saiu de AGUARDANDO_APROVACAO: descarta

        self.state_repository.salvar_estado(saga_id, alvo)
        self.state_repository.salvar_motivo(saga_id, motivo)
        self._enviar_comando(
            comando=comando,
            causa=None,
            motivo=motivo,
            saga_id=saga_id,
            os_id=estado.os_id,
            request_id=estado.referencias.get("requestId"),
        )
        return self.state_repository.get(saga_id)

    def expirar_vencidas(self, limite: datetime) -> list[str]:
        """Expira as sagas em AGUARDANDO_APROVACAO desde antes de `limite`."""
        saga_ids = self.state_repository.listar_aguardando_aprovacao_antes_de(limite)
        for saga_id in saga_ids:
            self.expirar(saga_id)
        return saga_ids

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _aplicar_pos_transicao(
        self, estado_atual, mensagem, saga_id, os_id, request_id, payload
    ) -> None:
        """Resolve a transição do gatilho externo `inicio` e envia o comando."""
        alvo, comando, motivo = self.maquina.transicao(estado_atual, mensagem)
        self.state_repository.salvar_estado(saga_id, alvo)
        if comando is not None:
            self._enviar_comando(
                comando=comando,
                causa=None,  # gatilho externo: causa nula
                motivo=motivo,
                saga_id=saga_id,
                os_id=os_id,
                request_id=request_id,
                payload=payload,
            )

    def _motivo_herdado(self, motivo, saga_id):
        """`herdado` → motivo guardado na saga; senão o próprio motivo."""
        if motivo == "herdado":
            estado = self.state_repository.get(saga_id)
            return estado.motivo
        return motivo

    def _enviar_comando(
        self,
        comando: str,
        causa: dto.MensagemSaga | None,
        motivo: str | None,
        saga_id: str | None = None,
        os_id: int | None = None,
        request_id: str | None = None,
        payload: dict | None = None,
    ) -> dto.MensagemSaga:
        """Publica o comando da transição como MensagemSaga do contrato."""
        base = causa or {}
        saga_id = saga_id or base.saga_id
        msg = dto.MensagemSaga(
            message_id=str(uuid4()),
            type=comando,
            saga_id=saga_id,
            os_id=os_id if os_id is not None else base.os_id,
            request_id=request_id or base.request_id,
            occurred_at=self.clock(),
            causation_id=causa.message_id if causa else None,
            payload=self._payload_comando(
                comando, base, motivo, payload, saga_id,
                os_id=os_id if os_id is not None else (base.os_id if hasattr(base, "os_id") else None),
            ),
            traceparent=self._traceparent_da_saga(saga_id),
        )
        resposta = self.publisher.publish(msg)
        self._registrar_enviada(msg, resposta)
        return msg

    def _traceparent_da_saga(self, saga_id: str) -> str | None:
        estado = self.state_repository.get(saga_id)
        if estado is None:
            return None
        return (estado.referencias or {}).get("traceparent")

    def _registrar_enviada(self, msg: dto.MensagemSaga, resposta) -> None:
        """Grava o comando no historico; falha do registro nunca desfaz o envio.

        O trace gravado e o que o publisher devolveu como realmente enviado
        (chave `traceparent` da resposta), nao o que se pretendia enviar.
        """
        if self.registro_mensagens is None:
            return
        enviado = resposta.get("traceparent") if isinstance(resposta, dict) else None
        trace_id = trace_id_de(enviado) or trace_id_de(msg.traceparent)
        try:
            self.registro_mensagens.registrar(msg, "ENVIADA", trace_id)
        except Exception:  # noqa: BLE001 — historico nunca bloqueia a saga
            logger.exception(
                "saga_v1_registro_enviada_falhou",
                extra={"message_id": msg.message_id, "saga_id": msg.saga_id},
            )

    def _payload_comando(
        self, comando: str, base, motivo: str | None, extra: dict | None,
        saga_id: str, os_id: int | None = None,
    ) -> dict:
        """Monta o payload mínimo do comando.

        - `GerarOrcamento` leva o payload do gatilho (diagnóstico).
        - Comandos de compensação levam exatamente o id do passo desfeito e
          motivo (schema com additionalProperties: false); o id vem da saga,
          porque a mensagem causadora nem sempre o traz.
        - SolicitarPagamento/IniciarExecucao levam o orcamentoId (das
          referências, pois a mensagem causadora nem sempre o traz) e a
          carga do aggregate da OS via CargaComandoSagaPort:
          valorTotalCentavos / servicos / pecas (payload completo do
          contrato — achado do grupo, 05/11).
        """
        payload = dict(extra or {})
        if comando == "GerarOrcamento":
            return payload
        if comando in ID_DA_COMPENSACAO:
            chave = ID_DA_COMPENSACAO[comando]
            referencias = self.state_repository.get(saga_id).referencias
            return {chave: referencias.get(chave), "motivo": motivo}
        if motivo is not None:
            payload["motivo"] = motivo if motivo != "herdado" else payload.get("motivo")
        for chave in ("orcamentoId", "pagamentoId", "execucaoId"):
            if chave not in payload:
                valor = (base.payload or {}).get(chave) if hasattr(base, "payload") else None
                if valor:
                    payload[chave] = valor
        if comando in ("SolicitarPagamento", "IniciarExecucao") and os_id is not None:
            if "orcamentoId" not in payload:
                referencias = self.state_repository.get(saga_id).referencias
                if (valor := referencias.get("orcamentoId")):
                    payload["orcamentoId"] = valor
            if self.carga_port is not None:
                carga = self.carga_port.carga(comando, os_id)
                for chave in CHAVES_DE_CARGA[comando]:
                    if carga.get(chave) is not None:
                        payload[chave] = carga[chave]
        if comando in CAMPOS_DO_PAYLOAD:
            payload = {
                chave: valor for chave, valor in payload.items()
                if chave in CAMPOS_DO_PAYLOAD[comando]
            }
        return payload
