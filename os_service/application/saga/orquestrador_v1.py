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
from datetime import datetime
from uuid import uuid4

from os_service.application.saga import dto
from os_service.application.saga.contrato_v1 import CONTRATO_SAGA_ESTADOS_V1

# Gatilhos externos da máquina (não são mensagens de serviço)
GATILHOS = ("inicio", "cancelamento")


class SagaOrquestradorV1:
    """Decide transições da saga v1 a partir da máquina de estados."""

    def __init__(
        self,
        state_repository,
        publisher,
        clock=None,
    ):
        self.state_repository = state_repository
        self.publisher = publisher
        self.clock = clock or (lambda: datetime.now(datetime.now().astimezone().tzinfo))
        self.maquina = dto.MaquinaEstados(CONTRATO_SAGA_ESTADOS_V1)

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
        msg = dto.MensagemSaga(
            message_id=str(uuid4()),
            type=comando,
            saga_id=saga_id or base.saga_id,
            os_id=os_id if os_id is not None else base.os_id,
            request_id=request_id or base.request_id,
            occurred_at=self.clock(),
            causation_id=causa.message_id if causa else None,
            payload=self._payload_comando(comando, base, motivo, payload),
        )
        self.publisher.publish(msg)
        return msg

    def _payload_comando(
        self, comando: str, base, motivo: str | None, extra: dict | None
    ) -> dict:
        """Monta o payload mínimo do comando.

        - `GerarOrcamento` leva o payload do gatilho (diagnóstico).
        - Comandos de compensação levam ids + motivo (contrato exige motivo
          em CancelarOrcamento/CancelarExecucao/EstornarPagamento).
        - Demais comandos levam os ids que vieram na mensagem causadora
          (ex.: OrcamentoGerado.orcamentoId → SolicitarPagamento.orcamentoId).
        """
        payload = dict(extra or {})
        if comando == "GerarOrcamento":
            return payload
        if motivo is not None:
            payload["motivo"] = motivo if motivo != "herdado" else payload.get("motivo")
        for chave in ("orcamentoId", "pagamentoId", "execucaoId"):
            if chave not in payload:
                valor = (base.payload or {}).get(chave) if hasattr(base, "payload") else None
                if valor:
                    payload[chave] = valor
        return payload
