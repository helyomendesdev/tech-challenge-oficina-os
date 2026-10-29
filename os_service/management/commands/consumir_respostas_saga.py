"""Management command: consumir as respostas da Saga v1 (Billing/Execução).

Uma onda receive_message (com MessageAttributeNames de trace), processa
cada envelope do contrato v1 e apaga a mensagem encerrada. Roda como
serviço/worker (cron no EKS ou EventBridge/Scheduler) quando a infra
da Sophia subir; com SAGA_RESPONSE_QUEUE_URL vazia, o comando explica
e sai sem alterar nada.

Run local: python manage.py consumir_respostas_saga --once
Loop curto (worker real):
  SAGA_RESPONSE_QUEUE_URL=... python manage.py consumir_respostas_saga
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Consome respostas da fila da Saga v1 e as aplica ao orquestrador "
        "(Billing/Execução -> -os)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Processa uma única onda (teste/manual); default: laço contínuo.",
        )
        parser.add_argument("--max-messages", type=int, default=10)
        parser.add_argument("--wait-seconds", type=int, default=10)
        parser.add_argument(
            "--intervalo",
            type=float,
            default=10.0,
            help="Segundos entre ondas no laço contínuo.",
        )

    def handle(self, *args, **options):
        from os_service.infrastructure.saga_consumidor import (
            build_consumidor_respostas,
        )

        try:
            consumidor = build_consumidor_respostas()
        except RuntimeError as exc:
            raise CommandError(str(exc) + " — configure a fila de respostas.")

        consumidor.max_messages = options["max_messages"]
        consumidor.wait_seconds = options["wait_seconds"]

        if options["once"]:
            self._onda(consumidor)
            return

        import time

        while True:
            self._onda(consumidor)
            time.sleep(options["intervalo"])

    def _onda(self, consumidor):
        resumo = consumidor.recebe_onda()
        self.stdout.write(
            f"Respostas da Saga: {resumo['recebidos']} recebidas "
            f"(processadas {resumo['processados']}, ignoradas "
            f"{resumo['ignoradas']}, duplicadas {resumo['duplicados']}, "
            f"invalidas {resumo['invalidas']}, erros {resumo['erros']})"
        )
