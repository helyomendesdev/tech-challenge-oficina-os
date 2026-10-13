"""Port de agregação do timeout de orçamento (banco do -os)."""
from typing import Protocol


class OrdemServicoTimeoutPort(Protocol):
    """Consulta e atualização de OS para o timeout de orçamento."""

    def listar_aguardando_antes_de(self, limite) -> list[tuple[int, float]]:
        """OS com status AGUARDANDO e data_ultima_transicao/data_abertura < limite.

        Retorna pares (os_id, valor_total).
        """
        ...

    def marcar_orcamento_expirado(self, os_id: int) -> None:
        """Cai a OS de AGUARDANDO para DIAGNOSTICO (orcamento expirado)."""
        ...
