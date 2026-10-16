"""Testes de integração da Saga com o use case de abertura (contrato v1).

Abertura de OS = gatilho externo `inicio`: cria SagaInstance (saga_id novo)
e publica o comando GerarOrcamento no publisher.
"""
from unittest.mock import MagicMock

from django.test import TestCase

from os_service.application.saga import dto


class SagaAberturaV1TestCase(TestCase):
    """Abertura de OS dispara a saga v1 (GerarOrcamento)."""

    def preparar(self):
        from os_service.application.use_cases.abrir_ordem_servico import (
            AbrirOrdemServicoUseCase,
        )

        orquestrador = MagicMock()
        orquestrador.iniciar.return_value = "saga-uuid-1"

        use_case = AbrirOrdemServicoUseCase(
            cliente_repository=MagicMock(),
            veiculo_repository=MagicMock(),
            servico_repository=MagicMock(),
            ordem_servico_repository=MagicMock(),
            transaction_manager=MagicMock(),
            saga_orquestrador=orquestrador,
        )
        return use_case, orquestrador

    def test_abertura_inicia_saga_v1_com_payload_do_diagnostico(self):
        use_case, orquestrador = self.preparar()

        class FakeInput:
            servicos = [1, 2]
            pecas = [(40, 2)]

        use_case._publicar_evento_saga(
            {"id": 1, "veiculo_id": 15, "valor_total": 250.0, "status": "RECEBIDA"},
            {"id": 7, "email": "c@x.com"},
            FakeInput(),
        )

        orquestrador.iniciar.assert_called_once()
        _, kwargs = orquestrador.iniciar.call_args
        self.assertEqual(kwargs["os_id"], 1)
        payload = kwargs["payload"]
        self.assertEqual(payload["clienteId"], 7)
        self.assertEqual(payload["veiculoId"], 15)
        self.assertEqual(
            payload["servicos"],
            [{"servicoId": 1, "quantidade": 1}, {"servicoId": 2, "quantidade": 1}],
        )
        self.assertEqual(payload["pecas"], [{"pecaId": 40, "quantidade": 2}])
        self.assertTrue(kwargs["request_id"])

    def test_falha_da_saga_nao_bloqueia_abertura(self):
        use_case, orquestrador = self.preparar()
        orquestrador.iniciar.side_effect = RuntimeError("fila fora do ar")

        class FakeInput:
            servicos = []
            pecas = []

        # não levanta
        use_case._publicar_evento_saga(
            {"id": 7, "veiculo_id": 1, "valor_total": 0, "status": "RECEBIDA"},
            {"id": 1, "email": "c@x.com"},
            FakeInput(),
        )

    def test_build_factory_usa_orquestrador_v1(self):
        from os_service.application.saga.orquestrador_v1 import (
            SagaOrquestradorV1,
        )
        from os_service.infrastructure.saga_adapters import (
            build_saga_orquestrador_v1,
        )

        orquestrador = build_saga_orquestrador_v1()
        self.assertIsInstance(orquestrador, SagaOrquestradorV1)
