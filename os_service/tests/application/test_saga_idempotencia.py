"""Testes de idempotência da Saga (review do contrato v1 — PR #2 do -saga).

Pontos apontados pelo Luís no PR #1 do -saga e confirmados no código:
1. _publicar_avanco publica ExecucaoIniciada com o mesmo event_id do evento
   recebido — o consumidor dedupa e a execução nunca inicia.
2. _evento_compensacao gera event_id "comp-{os}-{passo}", que não é UUID —
   ProcessedEvent.event_id é UUIDField e o insert de dedupe quebra.
3. SagaInstance.os_id unique=True — uma saga por OS para sempre; recusa de
   orçamento que devolve a OS ao diagnóstico não consegue abrir nova saga.
4. SQSSagaPublisher.publish não envia MessageDeduplicationId — fila FIFO
   recusa o envio (ou dedupa por body se ContentBasedDeduplication estiver
   ligado).
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from os_service.application.saga.dto import (
    SagaEventDTO,
    SagaEventType,
    SagaStatus,
    SagaStep,
)
from os_service.application.saga.orquestrador import SagaOrquestrador
from os_service.infrastructure.saga_adapters import SQSSagaPublisher
from os_service.tests.application.test_saga_orquestrador import (
    FakePublisher,
    FakeStateRepository,
    evento,
)


class PublicacaoComEventIdProprioTestCase(unittest.TestCase):
    """Ponto 1: cada mensagem publicada precisa de event_id próprio."""

    def setUp(self):
        self.state = FakeStateRepository()
        self.publisher = FakePublisher()
        self.saga = SagaOrquestrador(self.state, self.publisher)

    def test_publicar_avanco_publica_event_id_novo(self):
        avanco = evento(SagaEventType.ORCAMENTO_APROVADO)
        self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORCAMENTO_GERADO))

        self.saga.processar(avanco)

        tipos = [e.event_type for e in self.publisher.publicados]
        self.assertIn(SagaEventType.EXECUCAO_INICIADA, tipos)
        publicado = self.publisher.publicados[0]
        self.assertNotEqual(publicado.event_id, avanco.event_id)

    def test_compensacao_publica_event_ids_em_uuid(self):
        """Ponto 2: event_id de compensação precisa ser UUID válido."""
        self.saga.iniciar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORDEM_SERVICO_CRIADA))
        self.saga.processar(evento(SagaEventType.ORCAMENTO_GERADO))
        self.saga.processar(evento(SagaEventType.ORCAMENTO_APROVADO))

        self.saga.compensar(1, motivo="falha técnica na execução")

        self.assertTrue(self.publisher.publicados)
        for publicado in self.publisher.publicados:
            UUID(publicado.event_id)  # levanta ValueError se não for UUID


class SQSSagaPublisherTestCase(unittest.TestCase):
    """Ponto 4: send_message em fila FIFO exige MessageDeduplicationId."""

    def test_publish_envia_message_deduplication_id(self):
        client = MagicMock()
        client.send_message.return_value = {"MessageId": "m-1"}
        publisher = SQSSagaPublisher(
            queue_url="https://sqs.sa-east-1.amazonaws.com/123/saga.fifo",
            client=client,
        )
        event = SagaEventDTO(
            event_id=str(uuid4()),
            event_type=SagaEventType.EXECUCAO_INICIADA,
            occurred_at=datetime.now(timezone.utc),
            correlation_id="os-1",
            os_id=1,
            payload={},
        )

        publisher.publish(event)

        _, kwargs = client.send_message.call_args
        self.assertEqual(kwargs["MessageGroupId"], "os-1")
        self.assertEqual(kwargs["MessageDeduplicationId"], event.event_id)
        self.assertIn(event.event_id, kwargs["MessageBody"])


if __name__ == "__main__":
    unittest.main()
