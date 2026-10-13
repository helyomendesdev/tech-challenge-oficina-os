"""Adapter Django do port de timeout de orçamento."""
from os_service.domain.enums import StatusOrdemServico


class DjangoOrdemServicoTimeoutAdapter:
    """Implementação ORM do port do timeout de orçamento."""

    def listar_aguardando_antes_de(self, limite) -> list[tuple[int, float]]:
        from os_service.models import OrdemServico

        qs = OrdemServico.objects.filter(
            status=StatusOrdemServico.AGUARDANDO.value
        )
        # preferir data_ultima_transicao; cair para data_abertura quando NULL
        ids_com_valor = []
        for os_ in qs.select_related(None).only(
            "id", "valor_total", "data_ultima_transicao", "data_abertura"
        ):
            referencia = os_.data_ultima_transicao or os_.data_abertura
            if referencia and referencia < limite:
                ids_com_valor.append((os_.id, float(os_.valor_total)))
        return ids_com_valor

    def marcar_orcamento_expirado(self, os_id: int) -> None:
        from django.utils import timezone

        from os_service.models import OrdemServico

        os_ = OrdemServico.objects.get(pk=os_id)
        os_.status = StatusOrdemServico.DIAGNOSTICO.value
        os_.data_ultima_transicao = timezone.now()
        os_.save()
