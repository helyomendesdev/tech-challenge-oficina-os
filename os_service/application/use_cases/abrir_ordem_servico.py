"""Use case de abertura de ordem de servico.

Camada de aplicacao: orquestra o fluxo usando ports e transacao, sem acessar
Django ORM, serializers, request ou response diretamente.
"""

from typing import Any

from os_service.application.dtos import (
    AbrirOrdemServicoInputDTO,
    AbrirOrdemServicoOutputDTO,
)
from os_service.application.ports.cliente_repository import ClienteRepositoryPort
from os_service.application.ports.notification_port import NotificationPort
from os_service.application.ports.observabilidade_port import ObservabilidadePort
from os_service.application.ports.ordem_servico_repository import (
    OrdemServicoRepositoryPort,
)
from os_service.application.ports.servico_repository import ServicoRepositoryPort
from os_service.application.ports.transaction_manager import TransactionManagerPort
from os_service.application.ports.veiculo_repository import VeiculoRepositoryPort
from os_service.application.saga.dto import SagaEventDTO, SagaEventType, SagaStep
from os_service.application.saga.orquestrador import SagaOrquestrador
from os_service.domain.enums import StatusOrdemServico


def _campo(objeto: Any, nome: str) -> Any:
    """Busca um campo em objetos simples ou dicionarios."""
    if isinstance(objeto, dict):
        return objeto.get(nome)
    return getattr(objeto, nome)


class AbrirOrdemServicoUseCase:
    """Orquestra o fluxo de abertura de uma ordem de servico."""

    def __init__(
        self,
        cliente_repository: ClienteRepositoryPort,
        veiculo_repository: VeiculoRepositoryPort,
        servico_repository: ServicoRepositoryPort,
        ordem_servico_repository: OrdemServicoRepositoryPort,
        transaction_manager: TransactionManagerPort,
        notification_port: NotificationPort | None = None,
        observabilidade_port: ObservabilidadePort | None = None,
        saga_orquestrador: SagaOrquestrador | None = None,
    ):
        self.cliente_repository = cliente_repository
        self.veiculo_repository = veiculo_repository
        self.servico_repository = servico_repository
        self.ordem_servico_repository = ordem_servico_repository
        self.transaction_manager = transaction_manager
        self.notification_port = notification_port
        self.observabilidade_port = observabilidade_port
        # Saga opcional: quando presente, a abertura de OS inicia a saga
        self.saga_orquestrador = saga_orquestrador

    def execute(
        self, input_dto: AbrirOrdemServicoInputDTO
    ) -> AbrirOrdemServicoOutputDTO:
        """Executa a abertura da ordem de servico.

        Servicos continuam opcionais por compatibilidade com os fluxos legados.
        Quando informados, os ids sao validados pelo repository de servicos.

        A reserva de pecas passa pelo repository de OS para manter uma unica
        fonte de verdade para a baixa de estoque enquanto ItemPecaOS.save()
        preserva esse comportamento legado.
        """
        servico_ids = list(input_dto.servicos or [])
        pecas_input = list(input_dto.pecas or [])

        with self.transaction_manager.atomic():
            cliente = self.cliente_repository.get_or_create(
                input_dto.cliente,
                usuario_id=input_dto.usuario_id,
                usuario_is_staff=input_dto.usuario_is_staff,
            )
            veiculo = self.veiculo_repository.get_or_create(
                cliente,
                input_dto.veiculo,
                usuario_id=input_dto.usuario_id,
                usuario_is_staff=input_dto.usuario_is_staff,
            )
            servicos = self.servico_repository.get_by_ids(
                servico_ids,
                usuario_id=input_dto.usuario_id,
                usuario_is_staff=input_dto.usuario_is_staff,
            )
            ordem_servico = self.ordem_servico_repository.create(
                cliente,
                veiculo,
                usuario_id=input_dto.usuario_id,
            )

            self.ordem_servico_repository.adicionar_servicos(
                ordem_servico,
                servicos,
                usuario_id=input_dto.usuario_id,
            )
            self.ordem_servico_repository.adicionar_pecas(
                ordem_servico,
                pecas_input,
                usuario_id=input_dto.usuario_id,
                usuario_is_staff=input_dto.usuario_is_staff,
            )
            ordem_servico = self.ordem_servico_repository.recalcular_total(
                ordem_servico
            )

        self._notificar_orcamento(ordem_servico, cliente)
        self._registrar_evento_abertura(ordem_servico)
        self._publicar_evento_saga(ordem_servico, cliente, input_dto)

        return AbrirOrdemServicoOutputDTO(
            ordem_servico_id=_campo(ordem_servico, "id"),
            status=_campo(ordem_servico, "status"),
            valor_total=_campo(ordem_servico, "valor_total"),
            mensagem="Ordem de servico aberta com sucesso.",
        )

    def _notificar_orcamento(self, ordem_servico: Any, cliente: Any) -> None:
        """Solicita notificacao de orcamento quando houver adapter configurado."""
        if not self.notification_port:
            return

        self.notification_port.notificar_orcamento(
            ordem_servico_id=_campo(ordem_servico, "id"),
            email=_campo(cliente, "email"),
            valor_total=_campo(ordem_servico, "valor_total"),
        )

    def _registrar_evento_abertura(self, ordem_servico: Any) -> None:
        """Registra evento ABERTURA quando houver adapter de observabilidade configurado.

        Evento emitido após criação bem-sucedida:
        - evento: ABERTURA (criação, não transição)
        - statusAnterior: None (é criação, não transição de status anterior)
        - statusNovo: RECEBIDA (status inicial)
        - duracaoStatusSegundos: 0 (acaba de ser criada)
        """
        if not self.observabilidade_port:
            return

        os_id = _campo(ordem_servico, "id")
        status_novo = _campo(ordem_servico, "status")

        self.observabilidade_port.registrar_evento_ordem_servico({
            'evento': 'ABERTURA',
            'osId': os_id,
            'statusAnterior': None,
            'statusNovo': status_novo,
            'duracaoStatusSegundos': 0.0,
            'erroTipo': None,
        })

    def _publicar_evento_saga(self, ordem_servico, cliente, input_dto) -> None:
        """Dispara a saga (contrato v1): gatilho externo `inicio`.

        O orquestrador v1 gera o sagaId, cria o estado INICIADA →
        ORCAMENTO_SOLICITADO e publica o comando GerarOrcamento.
        Falha na saga não reverte a abertura: SagaInstance guarda o registro
        para reprocesso (contrato, DLQ + idempotência).
        """
        if not self.saga_orquestrador:
            return

        import logging
        from uuid import uuid4

        os_id = _campo(ordem_servico, "id")
        payload = {
            "clienteId": _campo(cliente, "id"),
            "veiculoId": _campo(ordem_servico, "veiculo_id"),
            "servicos": [
                {"servicoId": s, "quantidade": 1}
                for s in (input_dto.servicos or [])
            ],
            "pecas": [
                p
                if isinstance(p, dict)
                else {
                    "pecaId": getattr(p, "peca_id", None) or p[0],
                    "quantidade": getattr(p, "quantidade", None) or p[1],
                }
                for p in (input_dto.pecas or [])
            ],
        }
        try:
            self.saga_orquestrador.iniciar(
                os_id=os_id,
                request_id=f"abertura-os-{os_id}-{uuid4().hex[:8]}",
                payload=payload,
            )
        except Exception:  # pragma: no cover - falha de saga não bloqueia abertura
            logging.getLogger("os_service.saga").exception(
                "saga_inicio_falhou", extra={"os_id": os_id}
            )
