"""Hooks OS <-> saga: transicoes da saga v1 ancoram no aggregate da OS.

Contrato v1 (grupo, Q2): saga CANCELADA -> OS CANCELADA (nova saga so em
nova abertura). Isto substitui o legado (timeout recolocava a OS em
DIAGNOSTICO). O port mora no consumidor; o orquestrador v1 fica puro.

Cada teste posiciona a saga no estado anterior REAL da maquina e envia
a mensagem correspondente; o hook roda no estado pos-transicao.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.test import TestCase

from os_service.application.saga import dto
from os_service.application.saga.consumidor import ConsumidorRespostasSaga
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
from os_service.domain.enums import StatusOrdemServico
from os_service.infrastructure.hook_os_status import DjangoOSStatusPort
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    InMemorySagaPublisherV1,
)
from os_service.models import OrdemServico
from os_service.tests.helpers import (
    create_cliente,
    create_ordem_servico,
    create_veiculo,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _mensagem(tipo: str, saga_id: str, os_id: int) -> dto.MensagemSaga:
    return dto.MensagemSaga(
        message_id=_uid(),
        type=tipo,
        saga_id=saga_id,
        os_id=os_id,
        request_id="req-1",
        occurred_at=datetime.now(timezone.utc),
        payload={},
    )


class HookOSStatusBase(TestCase):
    def setUp(self):
        self.cliente = create_cliente()
        self.veiculo = create_veiculo(self.cliente)
        self.os_pk = create_ordem_servico(self.cliente, self.veiculo).pk
        self.repository = DjangoSagaStateRepositoryV1()
        self.orquestrador = SagaOrquestradorV1(
            state_repository=self.repository,
            publisher=InMemorySagaPublisherV1(),
        )
        self.port = DjangoOSStatusPort()
        self.consumer = ConsumidorRespostasSaga(
            orquestrador=self.orquestrador, os_status_port=self.port
        )

    def _saga_no_estado(self, estado: dto.EstadoSaga) -> str:
        saga_id = _uid()
        self.repository.criar(saga_id, self.os_pk, estado)
        return saga_id

    def _envia(self, tipo: str, saga_id: str) -> None:
        import json

        self.consumer.processar(self._corpo_json(tipo, saga_id))

    def _corpo_json(self, tipo: str, saga_id: str) -> str:
        import json

        return json.dumps(
            dto.envelope(_mensagem(tipo, saga_id, self.os_pk)),
            ensure_ascii=False,
        )

    def _os_atual(self) -> OrdemServico:
        return OrdemServico.objects.get(pk=self.os_pk)


class MapeamentoEstadoParaOS(HookOSStatusBase):
    def test_aguardando_aprovacao_muda_os_para_aguardando(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.ORCAMENTO_SOLICITADO)
        self._envia("OrcamentoGerado", saga_id)
        self.assertEqual(
            self._os_atual().status, StatusOrdemServico.AGUARDANDO.value
        )

    def test_execucao_solicitada_muda_os_para_execucao_com_data(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.PAGAMENTO_SOLICITADO)
        self._envia("PagamentoConfirmado", saga_id)
        atual = self._os_atual()
        self.assertEqual(atual.status, StatusOrdemServico.EXECUCAO.value)
        self.assertIsNotNone(atual.data_inicio_execucao)

    def test_em_execucao_nao_sobrescreve_data_de_inicio(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.PAGAMENTO_SOLICITADO)
        self._envia("PagamentoConfirmado", saga_id)
        data_1 = self._os_atual().data_inicio_execucao
        self._envia("ExecucaoIniciada", saga_id)  # EXEC._SOLIC.-> EM_EXECUCAO
        atual = self._os_atual()
        self.assertEqual(atual.status, StatusOrdemServico.EXECUCAO.value)
        self.assertEqual(atual.data_inicio_execucao, data_1)

    def test_concluida_finaliza_a_os_com_data(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.EM_EXECUCAO)
        self._envia("ExecucaoFinalizada", saga_id)
        atual = self._os_atual()
        self.assertEqual(atual.status, StatusOrdemServico.FINALIZADA.value)
        self.assertIsNotNone(atual.data_finalizacao)

    def test_cancelada_cancela_a_os(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.AGUARDANDO_APROVACAO)
        self._envia("OrcamentoRecusado", saga_id)
        self.assertEqual(
            self._os_atual().status, StatusOrdemServico.CANCELADA.value
        )

    def test_estados_intermediarios_nao_mudam_a_os(self):
        # PAGAMENTO_SOLICITADO e COMPENSANDO nao ancoram na OS
        antes = self._os_atual().status
        saga_1 = self._saga_no_estado(dto.EstadoSaga.AGUARDANDO_APROVACAO)
        self._envia("OrcamentoAprovado", saga_1)  # -> PAGAMENTO_SOLICITADO
        saga_2 = self._saga_no_estado(dto.EstadoSaga.PAGAMENTO_SOLICITADO)
        self._envia("PagamentoFalhou", saga_2)    # -> COMPENSANDO
        self.assertEqual(self._os_atual().status, antes)


class HookNuncaBloqueiaOSaga(HookOSStatusBase):
    def test_port_quebrado_nao_reverte_a_transicao_e_loga(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.ORCAMENTO_SOLICITADO)
        quebrado = MagicMock(side_effect=RuntimeError("OS sumiu"))
        self.consumer.os_status_port = quebrado

        self.consumer.processar(self._corpo_json("OrcamentoGerado",
                                                 saga_id))

        estado = self.repository.get(saga_id)
        self.assertEqual(
            estado.estado, dto.EstadoSaga.AGUARDANDO_APROVACAO
        )


class HookEChamadoComOEstadoNovo(HookOSStatusBase):
    def test_consumer_passa_estado_os_id_e_saga_id(self):
        saga_id = self._saga_no_estado(dto.EstadoSaga.ORCAMENTO_SOLICITADO)
        mockado = MagicMock()
        self.consumer.os_status_port = mockado

        self.consumer.processar(self._corpo_json("OrcamentoGerado",
                                                 saga_id))

        mockado.apos_transicao.assert_called_once()
        chamado = mockado.apos_transicao.call_args
        self.assertEqual(
            chamado.kwargs.get("os_id", chamado.args[0] if chamado.args
                               else None),
            self.os_pk,
        )
