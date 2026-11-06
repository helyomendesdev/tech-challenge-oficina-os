"""Outbox da Saga (ADR-001 §3.5): comandos gravados com a transição.

- OutboxSagaPublisherV1 grava na tabela saga_comando (idempotente por
  message_id); o fio real usa ele em vez de publicar na fila na hora;
- `publicar_comandos_saga` drena para as filas por destinatário e marca
  publicado_em; falha não marca (a passada seguinte repete — SQS dedup
  aceita reenvio pelo messageId);
- build real: com filas configuradas, o orquestrador escreve na outbox.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from os_service.infrastructure.saga_adapters import (
    InMemorySagaPublisherV1,
    OutboxSagaPublisherV1,
    build_saga_orquestrador_v1,
)
from os_service.models import SagaComando
from os_service.tests.helpers import (
    create_cliente,
    create_ordem_servico,
    create_veiculo,
)

URL_BILLING = "http://sqs/oficina-billing-comandos.fifo"
URL_EXECUTION = "http://sqs/oficina-execucao-comandos.fifo"
TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _msg(tipo: str, payload=None):
    from os_service.application.saga import dto

    return dto.MensagemSaga(
        message_id=str(uuid.uuid4()),
        type=tipo,
        saga_id=str(uuid.uuid4()),
        os_id=5,
        request_id="req-1",
        occurred_at=datetime.now(timezone.utc),
        payload=payload or {"orcamentoId": 88},
        traceparent=TRACE,
    )


class OutboxPublisherTestCase(TestCase):
    def setUp(self):
        self.publisher = OutboxSagaPublisherV1()

    def test_publish_grava_na_outbox_com_os_dados_da_mensagem(self):
        msg = _msg("SolicitarPagamento")

        resposta = self.publisher.publish(msg)

        registro = SagaComando.objects.get()
        self.assertEqual(resposta["MessageId"], msg.message_id)
        self.assertEqual(str(registro.message_id), msg.message_id)
        self.assertEqual(registro.type, "SolicitarPagamento")
        self.assertEqual(registro.os_id, msg.os_id)
        self.assertEqual(registro.payload, {"orcamentoId": 88})
        self.assertEqual(registro.traceparent, msg.traceparent)
        self.assertIsNone(registro.publicado_em)

    def test_repetida_por_message_id_e_idempotente(self):
        msg = _msg("GerarOrcamento")

        self.publisher.publish(msg)
        self.publisher.publish(msg)

        self.assertEqual(SagaComando.objects.count(), 1)

    def test_para_mensagem_reconstroi_o_dto(self):
        msg = _msg("IniciarExecucao", {"servicos": [], "pecas": []})
        self.publisher.publish(msg)

        reconstruida = SagaComando.objects.get().para_mensagem()

        self.assertEqual(reconstruida.message_id, msg.message_id)
        self.assertEqual(reconstruida.type, "IniciarExecucao")
        self.assertEqual(reconstruida.payload, {"servicos": [], "pecas": []})
        self.assertEqual(reconstruida.traceparent, msg.traceparent)

    def test_com_filas_configuradas_o_fio_usa_a_outbox(self):
        with override_settings(
            SAGA_BILLING_QUEUE_URL=URL_BILLING,
            SAGA_EXECUTION_QUEUE_URL=URL_EXECUTION,
        ):
            orquestrador = build_saga_orquestrador_v1()

        self.assertIsInstance(orquestrador.publisher, OutboxSagaPublisherV1)

    def test_sem_filas_o_fio_fica_no_inmemory(self):
        with override_settings(
            SAGA_BILLING_QUEUE_URL="", SAGA_EXECUTION_QUEUE_URL=""
        ):
            orquestrador = build_saga_orquestrador_v1()

        self.assertIsInstance(orquestrador.publisher, InMemorySagaPublisherV1)


class PublicarComandosSagaTestCase(TestCase):
    def setUp(self):
        agora = datetime.now(timezone.utc)
        self.cmd_billing = SagaComando.objects.create(
            message_id=uuid.uuid4(),
            saga_id=uuid.uuid4(),
            os_id=5,
            type="SolicitarPagamento",
            payload={"orcamentoId": 88, "valorTotalCentavos": 149990},
            request_id="req-1",
            causation_id=None,
            occurred_at=agora,
            traceparent=TRACE,
        )
        self.cmd_execution = SagaComando.objects.create(
            message_id=uuid.uuid4(),
            saga_id=uuid.uuid4(),
            os_id=5,
            type="IniciarExecucao",
            payload={"orcamentoId": 88},
            request_id="req-1",
            causation_id=None,
            occurred_at=agora,
            traceparent="",
        )

    def _drena(self, **kwargs):
        with (
            override_settings(
                SAGA_BILLING_QUEUE_URL=URL_BILLING,
                SAGA_EXECUTION_QUEUE_URL=URL_EXECUTION,
            ),
            patch("boto3.client") as boto_cli,
        ):
            boto_cli.return_value.send_message.return_value = {
                "MessageId": "m-1"
            }
            call_command("publicar_comandos_saga", **kwargs)
        return boto_cli

    def test_drena_todas_publica_e_marca_publicado_em(self):
        boto_cli = self._drena()

        send_kwargs = [
            c.kwargs
            for c in boto_cli.return_value.send_message.call_args_list
        ]
        self.assertEqual(
            sorted(k["QueueUrl"] for k in send_kwargs),
            sorted([URL_BILLING, URL_EXECUTION]),
        )
        # ordem por Saga e dedupe por messageId do contrato
        self.assertEqual(
            {k["MessageGroupId"] for k in send_kwargs},
            {str(self.cmd_billing.saga_id), str(self.cmd_execution.saga_id)},
        )
        self.assertEqual(
            {k["MessageDeduplicationId"] for k in send_kwargs},
            {str(self.cmd_billing.message_id),
             str(self.cmd_execution.message_id)},
        )
        self.cmd_billing.refresh_from_db()
        self.cmd_execution.refresh_from_db()
        self.assertIsNotNone(self.cmd_billing.publicado_em)
        self.assertIsNotNone(self.cmd_execution.publicado_em)

    def test_falha_no_envio_nao_marca_e_passada_segunda_publica(self):
        boto_cli = MagicMock()
        enviados = {"n": 0}

        def envia(**kwargs):
            enviados["n"] += 1
            if enviados["n"] == 2:  # 2º envio fora: só o 1º fica marcado
                raise RuntimeError("fila fora")
            return {"MessageId": "m-2"}

        boto_cli.return_value.send_message.side_effect = envia
        with (
            override_settings(
                SAGA_BILLING_QUEUE_URL=URL_BILLING,
                SAGA_EXECUTION_QUEUE_URL=URL_EXECUTION,
            ),
            patch("boto3.client", boto_cli),
        ):
            # a primeira passada grava o 1º e estoura no 2º
            with self.assertRaises(RuntimeError):
                call_command("publicar_comandos_saga")
            self.cmd_billing.refresh_from_db()
            self.cmd_execution.refresh_from_db()
            self.assertIsNotNone(self.cmd_billing.publicado_em)
            self.assertIsNone(self.cmd_execution.publicado_em)

            enviados["n"] = 0
            call_command("publicar_comandos_saga")

        self.cmd_billing.refresh_from_db()
        self.cmd_execution.refresh_from_db()
        self.assertIsNotNone(self.cmd_billing.publicado_em)
        self.assertIsNotNone(self.cmd_execution.publicado_em)
        self.assertEqual(boto_cli.return_value.send_message.call_count, 3)

    def test_sem_filas_configuradas_levanta_runtime_e_nada_marca(self):
        client = MagicMock()
        with (
            override_settings(
                SAGA_BILLING_QUEUE_URL="", SAGA_EXECUTION_QUEUE_URL=""
            ),
            patch("boto3.client", client),
        ):
            with self.assertRaises(RuntimeError):
                call_command("publicar_comandos_saga")

        self.cmd_billing.refresh_from_db()
        self.assertIsNone(self.cmd_billing.publicado_em)
        self.assertEqual(client.send_message.call_count, 0)

    def test_drena_em_lote_quando_mais_de_um_tamanho(self):
        client = MagicMock()
        client.return_value.send_message.return_value = {"MessageId": "m-3"}
        for _ in range(55):  # TAMANHO_DO_LOTE = 50 no command
            SagaComando.objects.create(
                message_id=uuid.uuid4(),
                saga_id=uuid.uuid4(),
                os_id=5,
                type="GerarOrcamento",
                payload={},
                request_id="req-1",
                causation_id=None,
                occurred_at=datetime.now(timezone.utc),
                traceparent=TRACE,
            )
        with (
            override_settings(
                SAGA_BILLING_QUEUE_URL=URL_BILLING,
                SAGA_EXECUTION_QUEUE_URL=URL_EXECUTION,
            ),
            patch("boto3.client", client),
        ):
            call_command("publicar_comandos_saga")

        self.assertEqual(
            client.return_value.send_message.call_count, 55 + 2
        )
        self.assertEqual(
            SagaComando.objects.filter(publicado_em__isnull=True).count(), 0
        )
