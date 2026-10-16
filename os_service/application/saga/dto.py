"""DTOs, enums e máquina de estados da Saga — contrato v1 (-saga, PR #2).

Envelope do contrato (messageId/type/version/sagaId/osId/causationId/
requestId/occurredAt/payload), estados da máquina saga-estados.yaml
(vendida em contrato_v1.py) e comandos+respostas em TipoMensagem.

A seção LEGADO (SagaStep/SagaEventType/COMPENSACOES etc.) é do rascunho
anterior e é removida quando orquestrador/timeout/adapters terminarem a
migração (fatias B e C do realinhamento).
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


# ------------------------------------------------------------------
# LEGADO — rascunho anterior (CONTRATO_SAGA_V1.md do PR #1). Usado por
# orquestrador.py, timeout_orcamento.py e saga_adapters.py até o fim da
# migração; remoção programada nas fatias B/C do realinhamento.
# ------------------------------------------------------------------
from dataclasses import dataclass, field as _field  # noqa: E402



class SagaStep(str, Enum):
    """Passos da saga orquestrada."""

    RESERVA_ESTOQUE = "RESERVA_ESTOQUE"
    ORCAMENTO = "ORCAMENTO"
    APROVACAO = "APROVACAO"
    EXECUCAO = "EXECUCAO"
    PAGAMENTO = "PAGAMENTO"
    ENTREGA = "ENTREGA"


class SagaStatus(str, Enum):
    """Status do fluxo da saga como um todo."""

    EM_ANDAMENTO = "EM_ANDAMENTO"
    CONCLUIDA = "CONCLUIDA"
    COMPENSANDO = "COMPENSANDO"
    COMPENSADA = "COMPENSADA"
    FALHA = "FALHA"


class SagaEventType(str, Enum):
    """event_type definidos no CONTRATO_SAGA_V1.md (seção 4 e 5)."""

    ORDEM_SERVICO_CRIADA = "OrdemServicoCriada"
    ORCAMENTO_GERADO = "OrcamentoGerado"
    ORCAMENTO_APROVADO = "OrcamentoAprovado"
    ORCAMENTO_RECUSADO = "OrcamentoRecusado"
    EXECUCAO_INICIADA = "ExecucaoIniciada"
    EXECUCAO_CONCLUIDA = "ExecucaoConcluida"
    PAGAMENTO_SOLICITADO = "PagamentoSolicitado"
    PAGAMENTO_APROVADO = "PagamentoAprovado"
    PAGAMENTO_RECUSADO = "PagamentoRecusado"
    ORDEM_SERVICO_ENTREGUE = "OrdemServicoEntregue"
    # Compensações
    ESTOQUE_LIBERADO = "EstoqueLiberado"
    ORCAMENTO_CANCELADO = "OrcamentoCancelado"
    EXECUCAO_ABORTADA = "ExecucaoAbortada"
    PAGAMENTO_ESTORNADO = "PagamentoEstornado"
    ORDEM_SERVICO_CANCELADA = "OrdemServicoCancelada"


COMPENSACOES = {
    SagaStep.RESERVA_ESTOQUE: SagaEventType.ESTOQUE_LIBERADO,
    SagaStep.ORCAMENTO: SagaEventType.ORCAMENTO_CANCELADO,
    SagaStep.EXECUCAO: SagaEventType.EXECUCAO_ABORTADA,
    SagaStep.PAGAMENTO: SagaEventType.PAGAMENTO_ESTORNADO,
    SagaStep.ENTREGA: SagaEventType.ORDEM_SERVICO_CANCELADA,
}


@dataclass(frozen=True)
class SagaEventDTO:
    """Envelope padrão de mensagem do contrato v1."""

    event_id: str
    event_type: SagaEventType
    occurred_at: datetime
    correlation_id: str
    os_id: int
    payload: dict = field(default_factory=dict)
    event_version: int = 1
    producer: str = "os-service"


@dataclass(frozen=True)
class SagaInstanceStateDTO:
    """Estado atual de uma instância da saga para uma OS."""

    os_id: int
    status: SagaStatus
    passo_atual: SagaStep | None
    passos_concluidos: tuple[SagaStep, ...] = ()
    compensation_pendente: SagaStep | None = None
    iniciado_em: datetime | None = None
    atualizado_em: datetime | None = None
