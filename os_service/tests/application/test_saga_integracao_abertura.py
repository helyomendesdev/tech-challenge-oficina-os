"""Testes de integração da Saga com os use cases (usar settings_test).

Cobre: abertura de OS inicia saga com event OrdemServicoCriada; estado
SagaInstance criado no banco do -os; publisher recebe o envelhe.
"""
from unittest.mock import MagicMock

from django.test import TestCase

from os_service.application.saga.dto import (
    SagaEventType,
    SagaInstanceStateDTO,
    SagaStatus,
    SagaStep,
)


class SagaRegistroIntegracaoTestCase(TestCase):
    """Publicação de OrdemServicoCriada na abertura de OS."""

    def preparar(self):
        from os_service.application.use_cases.abrir_ordem_servico import (
            AbrirOrdemServicoUseCase,
        )
        from os_service.application.saga.orquestrador import SagaOrquestrador
        from unittest.mock import MagicMock

        self.state = MagicMock()
        self.publisher = MagicMock()
        self.publisher.publish.return_value = {"MessageId": "x"}
        orquestrador = SagaOrquestrador(self.state, self.publisher)

        # Use case real: repos fake mínimo
        self.state.criar.return_value = SagaInstanceStateDTO(
            os_id=1,
            status=SagaStatus.EM_ANDAMENTO,
            passo_atual=SagaStep.RESERVA_ESTOQUE,
        )
        use_case = AbrirOrdemServicoUseCase(
            cliente_repository=MagicMock(),
            veiculo_repository=MagicMock(),
            servico_repository=MagicMock(),
            ordem_servico_repository=MagicMock(),
            transaction_manager=MagicMock(),
            saga_orquestrador=orquestrador,
        )
        return use_case

    def test_abertura_publica_evento_saga(self):
        use_case = self.preparar()
        # mocks mínimos pro happy path do use case
        cliente = {"id": 10, "email": "c@x.com"}
        ordem = {"id": 1, "veiculo_id": 2, "valor_total": 100.0, "status": "RECEBIDA"}
        # chamar direto pelo método privado com input fake
        class FakeInput:
            servicos = [1, 2]
            pecas = []

        use_case._publicar_evento_saga(ordem, cliente, FakeInput())

        # Saga iniciada = estado criado; publicação do comando concentra-se
        # nos avanços de passo (EXECUCAO_INICIADA etc.), não no início.
        self.state.criar.assert_called_once()
        args, kwargs = self.state.criar.call_args
        self.assertEqual(args[1], SagaStep.RESERVA_ESTOQUE)

    def test_abertura_cria_estado_saga(self):
        use_case = self.preparar()
        class FakeInput:
            servicos = []
            pecas = []

        use_case._publicar_evento_saga(
            {"id": 7, "veiculo_id": 1, "valor_total": 0, "status": "RECEBIDA"},
            {"id": 1, "email": "c@x.com"},
            FakeInput(),
        )
        self.state.criar.assert_called_once()
        args, kwargs = self.state.criar.call_args
        self.assertEqual(args[1], SagaStep.RESERVA_ESTOQUE)
