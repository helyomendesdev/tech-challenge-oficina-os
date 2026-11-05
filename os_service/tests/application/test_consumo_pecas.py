"""Baixa/devolução de peças na ExecucaoFinalizada (ADR-004, v1.1).

Consumidor recebe pecasConsumidas e o estoque do -os fecha o ciclo:
- por item reservado (itemPecaOSId): marca o consumo e devolve o resto;
- item reservado ausente da lista = não consumido = devolve a reserva;
- ExecucaoFinalizada SEM pecasConsumidas (v1.0) = reserva consumida por
  inteiro, sem devolução;
- idempotente por saga (flag nas referências), mesmo com at-least-once;
- consumo > reserva ou item de outra OS levanta (hook loga e não
  bloqueia a transição da saga).
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.db.models import F
from django.test import TestCase, override_settings

from os_service.application.saga import dto
from os_service.application.saga.consumidor import ConsumidorRespostasSaga
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
from os_service.infrastructure.hook_consumo_pecas import DjangoConsumoPecasPort
from os_service.infrastructure.hook_os_status import DjangoOSStatusPort
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    InMemorySagaPublisherV1,
)
from os_service.models import ItemPecaOS
from os_service.tests.helpers import (
    create_cliente,
    create_ordem_servico,
    create_peca,
    create_item_peca_os,
    create_veiculo,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _msg(tipo: str, saga_id: str, os_id: int, payload=None) -> dto.MensagemSaga:
    return dto.MensagemSaga(
        message_id=_uid(),
        type=tipo,
        saga_id=saga_id,
        os_id=os_id,
        request_id="req-1",
        occurred_at=datetime.now(timezone.utc),
        payload=payload or {},
    )


def _corpo(tipo: str, saga_id: str, os_id: int, payload=None) -> str:
    return json.dumps(
        dto.envelope(_msg(tipo, saga_id, os_id, payload)),
        ensure_ascii=False,
    )


class ConsumoPecasBase(TestCase):
    def setUp(self):
        self.cliente = create_cliente()
        self.veiculo = create_veiculo(self.cliente)
        self.os = create_ordem_servico(self.cliente, self.veiculo)
        self.peca = create_peca()  # estoque 10
        # a reserva baixa a disponibilidade na criação do item
        self.item = create_item_peca_os(self.os, self.peca, quantidade=5)
        self.peca.refresh_from_db()
        self.repository = DjangoSagaStateRepositoryV1()
        self.orquestrador = SagaOrquestradorV1(
            state_repository=self.repository,
            publisher=InMemorySagaPublisherV1(),
        )
        self.port = DjangoConsumoPecasPort()

    def _saga(self, estado=dto.EstadoSaga.EM_EXECUCAO):
        saga_id = _uid()
        self.repository.criar(saga_id, self.os.pk, estado)
        return saga_id


class ConsumoPecasPortTestCase(ConsumoPecasBase):
    def test_marca_o_consumo_e_devolve_o_restante_da_reserva(self):
        saga_id = self._saga()
        estoque_antes = self.peca.estoque_atual  # 5 (10 - reserva 5)

        self.port.aplicar_consumo(
            saga_id, self.os.pk,
            [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
              "quantidade": 2}],
        )

        self.item.refresh_from_db()
        self.peca.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 2)
        # 3 peças da reserva sobram: voltam ao estoque
        self.assertEqual(self.peca.estoque_atual, estoque_antes + 3)

    def test_item_reservado_ausente_da_lista_devolve_a_reserva_inteira(self):
        saga_id = self._saga()

        self.port.aplicar_consumo(saga_id, self.os.pk, [])

        self.item.refresh_from_db()
        self.peca.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 0)
        self.assertEqual(self.peca.estoque_atual, 10)  # devolveu a reserva

    def test_sem_pecas_consumidas_v1_0_trata_reserva_como_consumida(self):
        saga_id = self._saga()

        self.port.aplicar_consumo(saga_id, self.os.pk, None)

        self.item.refresh_from_db()
        self.peca.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, self.item.quantidade)
        self.assertEqual(self.peca.estoque_atual, 5)  # sem devolução

    def test_entradas_repetidas_do_mesmo_item_somam(self):
        saga_id = self._saga()

        self.port.aplicar_consumo(
            saga_id, self.os.pk,
            [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
              "quantidade": 1},
             {"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
              "quantidade": 1}],
        )

        self.item.refresh_from_db()
        self.peca.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 2)
        self.assertEqual(self.peca.estoque_atual, 8)

    def test_repeticao_da_saga_e_idempotente(self):
        saga_id = self._saga()
        payload = [{"itemPecaOSId": self.item.pk,
                    "pecaId": self.peca.pk, "quantidade": 2}]

        self.port.aplicar_consumo(saga_id, self.os.pk, payload)
        self.peca.refresh_from_db()
        estoque_apos_primeira = self.peca.estoque_atual

        self.port.aplicar_consumo(saga_id, self.os.pk, payload)

        self.item.refresh_from_db()
        self.peca.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 2)
        self.assertEqual(self.peca.estoque_atual, estoque_apos_primeira)

    def test_consumo_maior_que_a_reserva_levanta_e_nada_aplica(self):
        saga_id = self._saga()

        with self.assertRaises(ValueError):
            self.port.aplicar_consumo(
                saga_id, self.os.pk,
                [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
                  "quantidade": 6}],
            )

        self.item.refresh_from_db()
        self.peca.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 0)
        self.assertEqual(self.peca.estoque_atual, 5)
        # falhou e nada gravou: uma mensagem corrigida ainda se aplica
        self.port.aplicar_consumo(
            saga_id, self.os.pk,
            [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
              "quantidade": 5}],
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 5)

    def test_item_de_outra_os_nao_aplica(self):
        saga_id = self._saga()
        outra_os = create_ordem_servico(self.cliente, self.veiculo)

        with self.assertRaises(ValueError):
            self.port.aplicar_consumo(
                saga_id, outra_os.pk,
                [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
                  "quantidade": 1}],
            )

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 0)

    def test_quantidade_menor_que_um_levanta(self):
        saga_id = self._saga()
        with self.assertRaises(ValueError):
            self.port.aplicar_consumo(
                saga_id, self.os.pk,
                [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
                  "quantidade": 0}],
            )

    def test_saga_inexistente_nao_levanta_nem_aplica(self):
        self.port.aplicar_consumo(
            _uid(), self.os.pk,
            [{"itemPecaOSId": self.item.pk, "pecaId": self.peca.pk,
              "quantidade": 1}],
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantidade_utilizada, 0)


class _PortFake:
    def __init__(self, levanta=False):
        self.chamadas = []
        self.levanta = levanta

    def aplicar_consumo(self, saga_id, os_id, pecas_consumidas):
        self.chamadas.append((saga_id, os_id, pecas_consumidas))
        if self.levanta:
            raise RuntimeError("estoque explodiu")


class ConsumidorDelegacaoConsumoTestCase(ConsumoPecasBase):
    def _consumidor(self, port):
        return ConsumidorRespostasSaga(
            orquestrador=self.orquestrador,
            consumo_pecas_port=port,
        )

    def test_finalizada_chama_o_port_com_pecas_consumidas(self):
        port = _PortFake()
        consumidor = self._consumidor(port)
        saga_id = self._saga(dto.EstadoSaga.EM_EXECUCAO)
        payload = {"execucaoId": _uid(),
                   "finalizadaEm": "2026-11-04T10:00:00Z",
                   "pecasConsumidas": [{"itemPecaOSId": self.item.pk,
                                        "pecaId": self.peca.pk,
                                        "quantidade": 1}],
                   }

        saida = consumidor.processar(
            _corpo("ExecucaoFinalizada", saga_id, self.os.pk, payload)
        )

        self.assertEqual(saida["resultado"], "processado")
        self.assertEqual(saida["estado"], "CONCLUIDA")
        self.assertEqual(len(port.chamadas), 1)
        chamada_saga, chamada_os, chamada_pecas = port.chamadas[0]
        self.assertEqual(chamada_saga, saga_id)
        self.assertEqual(chamada_os, self.os.pk)
        self.assertEqual(chamada_pecas, payload["pecasConsumidas"])

    def test_finalizada_sem_pecas_v1_0_passa_none_ao_port(self):
        port = _PortFake()
        consumidor = self._consumidor(port)
        saga_id = self._saga()
        payload = {"execucaoId": _uid(),
                   "finalizadaEm": "2026-11-04T10:00:00Z"}

        consumidor.processar(
            _corpo("ExecucaoFinalizada", saga_id, self.os.pk, payload)
        )

        self.assertEqual(port.chamadas[0][2], None)

    def test_port_falho_nao_bloqueia_a_transicao(self):
        consumidor = self._consumidor(_PortFake(levanta=True))
        saga_id = self._saga()
        payload = {"execucaoId": _uid(),
                   "finalizadaEm": "2026-11-04T10:00:00Z"}

        saida = consumidor.processar(
            _corpo("ExecucaoFinalizada", saga_id, self.os.pk, payload)
        )

        self.assertEqual(saida["resultado"], "processado")
        self.assertEqual(saida["estado"], "CONCLUIDA")

    def test_outra_mensagem_nao_chama_o_port(self):
        port = _PortFake()
        consumidor = self._consumidor(port)
        saga_id = self._saga(dto.EstadoSaga.ORCAMENTO_SOLICITADO)

        consumidor.processar(
            _corpo("OrcamentoGerado", saga_id, self.os.pk,
                   {"orcamentoId": 7})
        )

        self.assertEqual(port.chamadas, [])


class FiacaoDoConsumidorRealTestCase(TestCase):
    """build_consumidor_respostas: OS status e consumo de peças plugados."""

    @override_settings(SAGA_RESPONSE_QUEUE_URL="http://sqs/respostas.fifo")
    def test_build_injeta_os_dois_hooks(self):
        from unittest.mock import patch

        from os_service.infrastructure.saga_consumidor import (
            build_consumidor_respostas,
        )

        with patch("boto3.client") as boto_fake:
            fila = build_consumidor_respostas()

        boto_fake.assert_called_with("sqs")
        self.assertIsInstance(fila.consumidor.os_status_port, DjangoOSStatusPort)
        self.assertIsInstance(
            fila.consumidor.consumo_pecas_port, DjangoConsumoPecasPort
        )
