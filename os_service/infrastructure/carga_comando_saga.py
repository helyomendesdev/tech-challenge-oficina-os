"""Adapter Django do port CargaComandoSagaPort (payloads completos da Saga).

Consulta o aggregate da OS no banco no momento da transição:
- SolicitarPagamento: valorTotalCentavos = valor_total da OS em centavos;
- IniciarExecucao: servicos[] (itens_servico, servicoId+quantidade=1) e
  pecas[] (itens_pecas: pecaId/quantidade; itemPecaOSId atrás do gate
  `com_item_peca_os_id` — ADR-004: só depois do consumidor re-vendar v1.1).
"""
from typing import Any

from os_service.application.saga.carga_comando import CargaComandoSagaPort


class DjangoCargaComandoSagaPort(CargaComandoSagaPort):
    def __init__(self, com_item_peca_os_id: bool = False):
        self.com_item_peca_os_id = com_item_peca_os_id

    def carga(self, comando: str, os_id: int) -> dict[str, Any]:
        from os_service.models import OrdemServico

        os_obj = OrdemServico.objects.filter(pk=os_id).first()
        if os_obj is None:
            raise ValueError(f"OS {os_id} não encontrada")

        valor_total = os_obj.valor_total or 0
        valor_total_centavos = int((valor_total * 100).to_integral_value())
        servicos = [
            {"servicoId": item.servico_id, "quantidade": 1}
            for item in os_obj.itens_servico.all()
        ]
        pecas = []
        for item in os_obj.itens_pecas.all():
            peca = {"pecaId": item.peca_id, "quantidade": item.quantidade}
            if self.com_item_peca_os_id:
                peca["itemPecaOSId"] = item.pk
            pecas.append(peca)
        return {
            "valorTotalCentavos": valor_total_centavos,
            "servicos": servicos,
            "pecas": pecas,
        }
