"""Payloads completos dos comandos SolicitarPagamento e IniciarExecucao.

Achado do Luís (zap 05/11): os comandos iam só com os ids e Billing/
Execution recusavam pelo schema do contrato (additionalProperties:false):
- SolicitarPagamento exige orcamentoId + valorTotalCentavos (int);
- IniciarExecucao exige orcamentoId + servicos[] + pecas[] (v1.1 aceita
  itemPecaOSId por item, gatable até a -execution re-vendar).

Desenho: port de aplicação CargaComandoSagaPort (aggregate da OS) +
adapter Django; o orquestrador hidrata o payload na hora de enviar; o
orcamentoId fica nas referências da saga desde o OrcamentoGerado.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.test import TestCase, override_settings

from os_service.application.saga import dto
from os_service.application.saga.carga_comando import CargaComandoSagaPort
from os_service.application.saga.orquestrador_v1 import SagaOrquestradorV1
from os_service.infrastructure.hook_consumo_pecas import DjangoConsumoPecasPort
from os_service.infrastructure.hook_os_status import DjangoOSStatusPort
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    InMemorySagaPublisherV1,
)
from os_service.infrastructure.carga_comando_saga import (
    DjangoCargaComandoSagaPort,
)
from os_service.tests.helpers import (
    create_cliente,
    create_ordem_servico,
    create_peca,
    create_item_peca_os,
    create_item_servico_os,
    create_servico,
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


def _corpo(tipo, saga_id, os_id, payload=None) -> str:
    return json.dumps(
        dto.envelope(_msg(tipo, saga_id, os_id, payload)),
        ensure_ascii=False,
    )


class _CargaFake(CargaComandoSagaPort):
    def __init__(self, carga):
        self.carga_data = carga
        self.chamadas = []

    def carga(self, comando: str, os_id: int) -> dict:
        self.chamadas.append((comando, os_id))
        return dict(self.carga_data[comando])


class PayloadsSagaBase(TestCase):
    def setUp(self):
        self.cliente = create_cliente()
        self.veiculo = create_veiculo(self.cliente)
        self.os = create_ordem_servico(self.cliente, self.veiculo)
        self.repository = DjangoSagaStateRepositoryV1()
        self.carga = _CargaFake(
            {
                "SolicitarPagamento": {"valorTotalCentavos": 1499900},
                "IniciarExecucao": {
                    "servicos": [{"servicoId": 3, "quantidade": 1}],
                    "pecas": [{"pecaId": 40, "quantidade": 2,
                               "itemPecaOSId": 501}],
                },
            }
        )
        self.orquestrador = SagaOrquestradorV1(
            state_repository=self.repository,
            publisher=InMemorySagaPublisherV1(),
            carga_port=self.carga,
        )
        # saga na máquina: INICIADA -> ORCAMENTO_SOLICITADO e assim por diante
        self.saga_id = self.orquestrador.iniciar(self.os.pk, "req-1", {"clienteId": 1})

    def _publica(self, tipo, payload):
        """Publica uma resposta do serviço e devolve o último comando enviado."""
        self.orquestrador.processar(
            _msg(tipo, self.saga_id, self.os.pk, payload)
        )
        return self.orquestrador.publisher.publicados[-1]


class SolicitarPagamentoPayloadTestCase(PayloadsSagaBase):
    def test_leva_orcamentoId_e_valorTotalCentavos(self):
        orcamento_id = 88

        comando = self._publica(
            "OrcamentoGerado",
            {"orcamentoId": orcamento_id,
             "valorTotalCentavos": 1499900,
             "validoAte": "2026-11-06T10:00:00Z"},
        )

        self.assertEqual(comando.type, "GerarOrcamento")  # abertura já publicou

        comando = self._publica(
            "OrcamentoAprovado",
            {"orcamentoId": orcamento_id,
             "aprovadoEm": "2026-11-05T18:00:00Z"},
        )

        self.assertEqual(comando.type, "SolicitarPagamento")
        self.assertEqual(
            dict(comando.payload),
            {"orcamentoId": orcamento_id, "valorTotalCentavos": 1499900},
        )
        self.assertIn(("SolicitarPagamento", self.os.pk), self.carga.chamadas)
        # orcamentoId ficou constante nas referências da saga
        estado = self.repository.get(self.saga_id)
        self.assertEqual(estado.referencias.get("orcamentoId"), orcamento_id)


class IniciarExecucaoPayloadTestCase(PayloadsSagaBase):
    def _ate_pagamento(self):
        self._publica("OrcamentoGerado",
                      {"orcamentoId": 88, "valorTotalCentavos": 1499900,
                       "validoAte": "2026-11-06T10:00:00Z"})
        self._publica("OrcamentoAprovado",
                      {"orcamentoId": 88, "aprovadoEm": "2026-11-05T18:00:00Z"})
        self._publica("PagamentoConfirmado", {"pagamentoId": 7})

    def test_leva_orcamentoId_servicos_e_pecas(self):
        self._ate_pagamento()

        # a máquina: PagamentoConfirmado -> EXECUCAO_SOLICITADA, envia
        # IniciarExecucao (SolicitarPagamento já saiu no passo anterior)
        comandos = [m for m in self.orquestrador.publisher.publicados]
        self.assertIn("IniciarExecucao", [m.type for m in comandos])
        comando = [m for m in comandos if m.type == "IniciarExecucao"][-1]
        self.assertEqual(
            comando.payload["servicos"],
            [{"servicoId": 3, "quantidade": 1}],
        )
        self.assertEqual(
            comando.payload["pecas"],
            [{"pecaId": 40, "quantidade": 2, "itemPecaOSId": 501}],
        )
        self.assertEqual(comando.payload["orcamentoId"], 88)
        self.assertIn(("IniciarExecucao", self.os.pk), self.carga.chamadas)

    def test_orcamentoId_vem_das_referencias_se_a_base_nao_leva(self):
        self._ate_pagamento()
        self.orquestrador.processar(
            _msg("PagamentoConfirmado", self.saga_id, self.os.pk,
                 {"pagamentoId": 7})
        )
        comando = [m for m in self.orquestrador.publisher.publicados
                   if m.type == "IniciarExecucao"][-1]
        self.assertEqual(comando.payload["orcamentoId"], 88)


class SemPortMantemPayloadMinimoTestCase(PayloadsSagaBase):
    def test_sem_carga_port_o_comando_va_somente_com_o_id(self):
        self.orquestrador.carga_port = None
        self._publica("OrcamentoGerado",
                      {"orcamentoId": 88, "valorTotalCentavos": 1499900,
                       "validoAte": "2026-11-06T10:00:00Z"})
        comando = self._publica("OrcamentoAprovado",
                                {"orcamentoId": 88,
                                 "aprovadoEm": "2026-11-05T18:00:00Z"})

        self.assertEqual(comando.type, "SolicitarPagamento")
        self.assertEqual(dict(comando.payload), {"orcamentoId": 88})


class ComandosCompensacaoContinuamIdemTestCase(PayloadsSagaBase):
    """Compensação passa a levar só o id do passo (sem carga do port)."""

    def test_pagamento_falhou_manda_cancelar_orcamento_como_vinha(self):
        self._publica("OrcamentoGerado",
                      {"orcamentoId": 88, "valorTotalCentavos": 1499900,
                       "validoAte": "2026-11-06T10:00:00Z"})
        self._publica("OrcamentoAprovado",
                      {"orcamentoId": 88, "aprovadoEm": "2026-11-05T18:00:00Z"})

        comando = self._publica("PagamentoFalhou", {"motivo": "cartao recusado"})

        self.assertEqual(comando.type, "CancelarOrcamento")
        self.assertEqual(
            dict(comando.payload),
            {"orcamentoId": 88, "motivo": "PAGAMENTO_FALHOU"},
        )
