"""Timeout de aprovação ponta a ponta no banco (ADR-003 do -saga).

checar_timeouts_orcamento expira só a saga em AGUARDANDO_APROVACAO além do
prazo, e o estado gravado guarda o orcamentoId que a compensação exige.
"""
import uuid
from datetime import datetime, timedelta, timezone
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from os_service.application.saga import dto
from os_service.infrastructure.saga_adapters import (
    DjangoSagaStateRepositoryV1,
    build_saga_orquestrador_v1,
)
from os_service.models import SagaInstance


class ChecarTimeoutsV1TestCase(TestCase):
    def _saga_aguardando(self, orcamento_id):
        orquestrador = build_saga_orquestrador_v1()
        saga_id = orquestrador.iniciar(os_id=301, request_id="req-t", payload={})
        orquestrador.processar(dto.MensagemSaga(
            message_id=str(uuid.uuid4()),
            type="OrcamentoGerado",
            saga_id=saga_id,
            os_id=301,
            request_id="req-t",
            occurred_at=datetime.now(timezone.utc),
            payload={"orcamentoId": orcamento_id, "valorTotalCentavos": 10000,
                     "validoAte": "2026-10-20T12:00:00Z"},
        ))
        return saga_id

    def test_expira_so_a_saga_vencida(self):
        orcamento_id = str(uuid.uuid4())
        vencida = self._saga_aguardando(orcamento_id)
        recente = self._saga_aguardando(str(uuid.uuid4()))
        SagaInstance.objects.filter(saga_id=vencida).update(
            estado_desde=datetime.now(timezone.utc) - timedelta(hours=1)
        )

        saida = StringIO()
        call_command("checar_timeouts_orcamento", stdout=saida)

        repo = DjangoSagaStateRepositoryV1()
        self.assertEqual(repo.get(vencida).estado, dto.EstadoSaga.COMPENSANDO)
        self.assertEqual(repo.get(vencida).motivo, "TIMEOUT")
        self.assertEqual(repo.get(vencida).referencias,
                         {"requestId": "req-t", "orcamentoId": orcamento_id})
        self.assertEqual(repo.get(recente).estado, dto.EstadoSaga.AGUARDANDO_APROVACAO)
        self.assertIn("1 sagas expiradas", saida.getvalue())

    def test_dry_run_nao_altera(self):
        vencida = self._saga_aguardando(str(uuid.uuid4()))
        SagaInstance.objects.filter(saga_id=vencida).update(
            estado_desde=datetime.now(timezone.utc) - timedelta(hours=1)
        )

        saida = StringIO()
        call_command("checar_timeouts_orcamento", "--dry-run", stdout=saida)

        estado = DjangoSagaStateRepositoryV1().get(vencida).estado
        self.assertEqual(estado, dto.EstadoSaga.AGUARDANDO_APROVACAO)
        self.assertIn(vencida, saida.getvalue())
