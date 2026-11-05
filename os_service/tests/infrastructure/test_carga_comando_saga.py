"""CargaComandoSagaPort — adapter Django (payloads completos da Saga).

- valorTotalCentavos: valor_total da OS em centavos;
- servicos[]: itens_servico (servicoId, quantidade=1 — cada linha é 1
  item, o modelo não tem campo quantidade);
- pecas[]: itens_pecas (pecaId, quantidade; itemPecaOSId atrás do gate
  com_item_peca_os_id — ADR-004, liga quando a -execution re-vendar).
"""
from django.test import TestCase

from os_service.infrastructure.carga_comando_saga import (
    DjangoCargaComandoSagaPort,
)
from os_service.infrastructure.saga_adapters import (
    build_saga_orquestrador_v1,
)
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
from os_service.tests.helpers import (
    create_cliente,
    create_ordem_servico,
    create_peca,
    create_item_peca_os,
    create_item_servico_os,
    create_servico,
    create_veiculo,
)


class DjangoCargaComandoSagaPortTestCase(TestCase):
    def setUp(self):
        self.cliente = create_cliente()
        self.veiculo = create_veiculo(self.cliente)
        self.os = create_ordem_servico(self.cliente, self.veiculo)
        self.servico = create_servico()
        self.item_servico = create_item_servico_os(self.os, self.servico)
        self.peca = create_peca()  # valor_unitario 10
        self.item_peca = create_item_peca_os(self.os, self.peca, quantidade=5)
        self.port = DjangoCargaComandoSagaPort(com_item_peca_os_id=True)
        # update() direto: Model.save() recalcula o total dos itens
        from os_service.models import OrdemServico

        OrdemServico.objects.filter(pk=self.os.pk).update(valor_total=1499.90)

    def test_valorTotalCentavos_da_os(self):
        carga = self.port.carga("SolicitarPagamento", self.os.pk)
        self.assertEqual(carga["valorTotalCentavos"], 149990)

    def test_servicos_sao_itens_da_os(self):
        carga = self.port.carga("IniciarExecucao", self.os.pk)
        self.assertEqual(
            carga["servicos"],
            [{"servicoId": self.servico.pk, "quantidade": 1}],
        )

    def test_pecas_com_itemPecaOSId_quando_gate_ligado(self):
        carga = self.port.carga("IniciarExecucao", self.os.pk)
        self.assertEqual(
            carga["pecas"],
            [{"pecaId": self.peca.pk, "quantidade": 5,
              "itemPecaOSId": self.item_peca.pk}],
        )

    def test_pecas_sem_itemPecaOSId_quando_gate_desligado(self):
        port = DjangoCargaComandoSagaPort(com_item_peca_os_id=False)
        carga = port.carga("IniciarExecucao", self.os.pk)
        self.assertEqual(
            carga["pecas"],
            [{"pecaId": self.peca.pk, "quantidade": 5}],
        )

    def test_os_inexistente_levanta(self):
        with self.assertRaises(ValueError):
            DjangoCargaComandoSagaPort().carga("SolicitarPagamento", 999999)


class FiacaoCargaPortTestCase(TestCase):
    @staticmethod
    def _build():
        from unittest.mock import patch

        with patch("boto3.client"):
            return build_saga_orquestrador_v1()

    def test_build_conecta_o_adapter_django_como_carga_port(self):
        orquestrador = self._build()
        self.assertIsInstance(orquestrador, SagaOrquestradorV1)
        self.assertIsInstance(
            orquestrador.carga_port, DjangoCargaComandoSagaPort
        )

    def test_gate_da_o_setting(self):
        from django.test import override_settings

        with override_settings(SAGA_ENVIA_ITEM_PECA_OS_ID=True):
            orquestrador = self._build()
            self.assertTrue(orquestrador.carga_port.com_item_peca_os_id)

        with override_settings(SAGA_ENVIA_ITEM_PECA_OS_ID=False):
            orquestrador = self._build()
            self.assertFalse(orquestrador.carga_port.com_item_peca_os_id)
