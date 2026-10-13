"""Enums e DTOs da Saga orquestrada (orquestrador no OS Service).

Espelham `CONTRATO_SAGA_V1.md` do repo `-saga` (Grupo 47). Qualquer mudança
de quebra aqui exige nova versão do contrato no `-saga`.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


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
