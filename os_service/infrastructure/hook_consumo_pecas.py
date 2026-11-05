"""Hook de consumo de peças: ExecucaoFinalizada -> estoque do -os (ADR-004).

Contrato v1.1 (tag do -saga): ExecucaoFinalizada leva pecasConsumidas[]
opcional, por item reservado (itemPecaOSId). ADR-004:
- quantidade informada = consumida por aquele item; o resto da reserva
  volta ao estoque (dono: -os);
- item reservado ausente da lista = não consumido = devolve a reserva;
- ExecucaoFinalizada SEM pecasConsumidas (formato v1.0) mantém o
  comportamento da v1.0: a quantidade reservada é tratada como consumida;
- idempotência: flag consumo_pecas_aplicado nas referências da SagaInstance
  (at-least-once da fila não aplica duas vezes).
"""
import logging

from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger("os_service.saga")


class ConsumoPecasPort:
    """Protocolo do hook de consumo de peças no consumidor da saga."""

    def aplicar_consumo(
        self, saga_id: str, os_id: int, pecas_consumidas: list[dict] | None
    ) -> None:
        """Baixa o consumo por item reservado e devolve o restante."""
        ...


class DjangoConsumoPecasPort(ConsumoPecasPort):
    """Aplica pecasConsumidas no estoque do -os, idempotente por saga."""

    def aplicar_consumo(
        self, saga_id: str, os_id: int, pecas_consumidas: list[dict] | None
    ) -> None:
        from os_service.models import ItemPecaOS, SagaInstance

        with transaction.atomic():
            saga = SagaInstance.objects.filter(saga_id=saga_id).first()
            if saga is None:
                logger.warning(
                    "saga_v1_consumo_sem_saga",
                    extra={"saga_id": saga_id, "os_id": os_id},
                )
                return
            referencias = dict(saga.referencias or {})
            if referencias.get("consumo_pecas_aplicado"):
                logger.info(
                    "saga_v1_consumo_ja_aplicado",
                    extra={"saga_id": saga_id, "os_id": os_id},
                )
                return

            if pecas_consumidas is None:
                # v1.0: a quantidade reservada é tratada como consumida
                # (sem devolução; o estoque já foi baixado na reserva).
                ItemPecaOS.objects.filter(os_id=os_id).update(
                    quantidade_utilizada=F("quantidade")
                )
            else:
                self._aplicar_lista(saga_id, os_id, pecas_consumidas)

            # mesmo com a lista vazia, a conclusão da OS consome o ciclo:
            # aplicar de novo não pode repetir baixa nem devolução.
            referencias["consumo_pecas_aplicado"] = timezone.now().isoformat()
            saga.referencias = referencias
            saga.save(update_fields=["referencias"])
            logger.info(
                "saga_v1_consumo_pecas_aplicado",
                extra={"saga_id": saga_id, "os_id": os_id,
                       "pecas_consumidas": bool(pecas_consumidas)},
            )

    def _aplicar_lista(self, saga_id: str, os_id: int, pecas_consumidas: list[dict]) -> None:
        """Consome os itens da lista e devolve os reservados ausentes."""
        from os_service.models import ItemPecaOS

        # soma entradas repetidas do mesmo item antes de aplicar: a
        # devolução é única, calculada sobre o consumo do item inteiro.
        consumo_por_item: dict[int, int] = {}
        for dados in pecas_consumidas:
            item_id = dados.get("itemPecaOSId")
            consumida = dados.get("quantidade", 0)
            if consumida < 1:
                raise ValueError(
                    "quantidade consumida mínima é 1 "
                    f"(itemPecaOSId={item_id}, quantidade={consumida})"
                )
            consumo_por_item[item_id] = consumo_por_item.get(item_id, 0) + consumida

        itens = (
            ItemPecaOS.objects.select_for_update().filter(os_id=os_id)
        )
        for item in itens:
            consumida = consumo_por_item.pop(item.pk, 0)
            if item.quantidade_utilizada + consumida > item.quantidade:
                raise ValueError(
                    f"consumo excede a reserva do item {item.pk} "
                    f"(reservado {item.quantidade}, "
                    f"utilizado {item.quantidade_utilizada}, "
                    f"consumido {consumida})"
                )
            if consumida:
                item.quantidade_utilizada += consumida
                item.save(update_fields=["quantidade_utilizada"])

            devolve = item.quantidade - item.quantidade_utilizada
            if devolve > 0:
                item.peca.estoque_atual = F("estoque_atual") + devolve
                item.peca.save(update_fields=["estoque_atual"])
                logger.info(
                    "saga_v1_reserva_devolvida",
                    extra={"saga_id": saga_id, "os_id": os_id,
                           "item_peca_os_id": item.pk,
                           "devolvido": devolve},
                )

        # a lista só pode referenciar itens da OS em que a saga correu
        if consumo_por_item:
            estranhos = ", ".join(str(k) for k in consumo_por_item)
            raise ValueError(
                f"itemPecaOSId não pertence à OS {os_id}: {estranhos}"
            )
