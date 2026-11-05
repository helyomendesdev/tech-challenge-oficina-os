"""Port de carga do aggregate da OS para os comandos da Saga.

Achado no alinhamento do grupo (05/11): os comandos iam só com os ids e o
Billing/Execution recusavam pelo schema (additionalProperties: false).
- SolicitarPagamento exige orcamentoId + valorTotalCentavos;
- IniciarExecucao exige orcamentoId + servicos[] + pecas[].

A camada de aplicação não conhece ORM: o adapter Django consulta o
aggregate no momento da transição (dados frescas, não snapshot).
"""
from typing import Any


class CargaComandoSagaPort:
    """Protocolo de carga do aggregate da OS para o payload do comando."""

    def carga(self, comando: str, os_id: int) -> dict[str, Any]:
        """Devolve os dados que o comando precisa (chaves do contrato)."""
        ...
