"""DTOs, enums e máquina de estados da Saga — contrato v1 (-saga, PR #2).

Envelope do contrato (messageId/type/version/sagaId/osId/causationId/
requestId/occurredAt/payload), estados da máquina saga-estados.yaml
(vendida em contrato_v1.py) e comandos+respostas em TipoMensagem.
O rascunho v0 (SagaStep/SagaEventType/COMPENSACOES) foi removido na
fatia C do realinhamento (PR da fatia C).
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TipoMensagem(str, Enum):
    """Campo `type` do envelope (comandos e respostas)."""

    # Comandos (orquestrador → serviços)
    GERAR_ORCAMENTO = "GerarOrcamento"
    CANCELAR_ORCAMENTO = "CancelarOrcamento"
    SOLICITAR_PAGAMENTO = "SolicitarPagamento"
    ESTORNAR_PAGAMENTO = "EstornarPagamento"
    INICIAR_EXECUCAO = "IniciarExecucao"
    CANCELAR_EXECUCAO = "CancelarExecucao"
    # Respostas (serviços → orquestrador)
    ORCAMENTO_GERADO = "OrcamentoGerado"
    ORCAMENTO_FALHOU = "OrcamentoFalhou"
    ORCAMENTO_APROVADO = "OrcamentoAprovado"
    ORCAMENTO_RECUSADO = "OrcamentoRecusado"
    ORCAMENTO_CANCELADO = "OrcamentoCancelado"
    PAGAMENTO_CONFIRMADO = "PagamentoConfirmado"
    PAGAMENTO_FALHOU = "PagamentoFalhou"
    PAGAMENTO_ESTORNADO = "PagamentoEstornado"
    EXECUCAO_INICIADA = "ExecucaoIniciada"
    EXECUCAO_FALHOU = "ExecucaoFalhou"
    EXECUCAO_FINALIZADA = "ExecucaoFinalizada"
    EXECUCAO_CANCELADA = "ExecucaoCancelada"


class EstadoSaga(str, Enum):
    """Estados da máquina de estados do orquestrador."""

    INICIADA = "INICIADA"
    ORCAMENTO_SOLICITADO = "ORCAMENTO_SOLICITADO"
    AGUARDANDO_APROVACAO = "AGUARDANDO_APROVACAO"
    PAGAMENTO_SOLICITADO = "PAGAMENTO_SOLICITADO"
    EXECUCAO_SOLICITADA = "EXECUCAO_SOLICITADA"
    EM_EXECUCAO = "EM_EXECUCAO"
    COMPENSANDO = "COMPENSANDO"
    CONCLUIDA = "CONCLUIDA"
    CANCELADA = "CANCELADA"


class MotivoReversao(str, Enum):
    """Motivo de CancelarExecucao e EstornarPagamento (comum.schema.json)."""

    EXECUCAO_FALHOU = "EXECUCAO_FALHOU"
    CANCELADO_PELO_CLIENTE = "CANCELADO_PELO_CLIENTE"
    TIMEOUT = "TIMEOUT"


class MotivoCancelamento(str, Enum):
    """Motivo de CancelarOrcamento = motivoReversao + PAGAMENTO_FALHOU."""

    EXECUCAO_FALHOU = "EXECUCAO_FALHOU"
    CANCELADO_PELO_CLIENTE = "CANCELADO_PELO_CLIENTE"
    TIMEOUT = "TIMEOUT"
    PAGAMENTO_FALHOU = "PAGAMENTO_FALHOU"


class TransicaoInvalidaError(Exception):
    """Mensagem recebida não casa com transição nenhuma do estado atual.

    Conforme saga-estados.yaml: resposta fora de estado não muda o estado —
    o orquestrador registra e descarta.
    """


class SagaNaoEncontradaError(Exception):
    """sagaId inexistente no repositório de estado."""


class StatusSaga(str, Enum):
    """Status de alto nível da instância da saga (espelha SagaInstance)."""

    EM_ANDAMENTO = "EM_ANDAMENTO"
    CONCLUIDA = "CONCLUIDA"
    FALHA = "FALHA"


@dataclass(frozen=True)
class SagaV1StateDTO:
    """Estado corrente de uma instância da saga v1."""

    saga_id: str
    os_id: int
    status: StatusSaga
    estado: EstadoSaga
    motivo: str | None = None
    # requestId da abertura e ids dos passos (orcamentoId, pagamentoId,
    # execucaoId): a compensação precisa deles e o gatilho externo não os traz.
    referencias: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MensagemSaga:
    """Envelope padrão de mensagem do contrato v1."""

    message_id: str
    type: str
    saga_id: str
    os_id: int
    request_id: str
    occurred_at: datetime
    payload: dict = field(default_factory=dict)
    version: int = 1
    causation_id: str | None = None

    @property
    def tipo(self) -> TipoMensagem:
        return TipoMensagem(self.type)


def envelope(msg: MensagemSaga) -> dict:
    """Serializa no formato do envelope.schema.json (camelCase)."""
    return {
        "messageId": msg.message_id,
        "type": msg.type,
        "version": msg.version,
        "sagaId": msg.saga_id,
        "osId": msg.os_id,
        "causationId": msg.causation_id,
        "requestId": msg.request_id,
        "occurredAt": msg.occurred_at.isoformat(),
        "payload": msg.payload,
    }


def deserializar(dados: dict) -> MensagemSaga:
    """Envelope camelCase do contrato → MensagemSaga (inverso de envelope)."""
    if not isinstance(dados, dict):
        raise ValueError("envelope deve ser objeto JSON")
    obrig = ("messageId", "type", "sagaId", "osId", "requestId", "occurredAt")
    faltantes = [campo for campo in obrig if campo not in dados]
    if faltantes:
        raise ValueError(f"campos ausentes no envelope: {', '.join(faltantes)}")
    try:
        occurred = datetime.fromisoformat(str(dados["occurredAt"]))
        os_id = int(dados["osId"])
        version = int(dados.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"campo com tipo inválido: {exc}") from exc
    return MensagemSaga(
        message_id=str(dados["messageId"]),
        type=str(dados["type"]),
        version=version,
        saga_id=str(dados["sagaId"]),
        os_id=os_id,
        causation_id=(
            None if dados.get("causationId") is None
            else str(dados["causationId"])
        ),
        request_id=str(dados["requestId"]),
        occurred_at=occurred,
        payload=dict(dados.get("payload") or {}),
    )


@dataclass(frozen=True)
class Transicao:
    """Uma linha de `transicoes:` da máquina."""

    de: EstadoSaga
    recebe: str
    para: EstadoSaga
    envia: str | None = None
    motivo: str | None = None


class MaquinaEstados:
    """Consulta das transições de saga-estados.yaml (vendida no -os)."""

    def __init__(self, definicao: dict):
        self.inicial = EstadoSaga(definicao["inicial"])
        self.finais = {EstadoSaga(e) for e in definicao["finais"]}
        self._transicoes: dict[tuple[EstadoSaga, str], Transicao] = {}
        for t in definicao["transicoes"]:
            chave = (EstadoSaga(t["de"]), t["recebe"])
            if chave in self._transicoes:
                raise ValueError(f"Transição duplicada: {chave}")
            self._transicoes[chave] = Transicao(
                de=chave[0],
                recebe=t["recebe"],
                para=EstadoSaga(t["para"]),
                envia=t.get("envia"),
                motivo=t.get("motivo"),
            )

    def transicao(
        self, estado: EstadoSaga, mensagem: str
    ) -> tuple[EstadoSaga, str | None, str | None]:
        """Resolve (estado, mensagem) → (para, envia, motivo).

        Levanta TransicaoInvalidaError se não houver transição — o chamador
        decide registrar-e-descartar ou falhar.
        """
        t = self._transicoes.get((estado, mensagem))
        if t is None:
            raise TransicaoInvalidaError(
                f"Estado {estado.value} não aceita '{mensagem}'"
            )
        return t.para, t.envia, t.motivo
