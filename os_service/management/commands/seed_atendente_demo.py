"""Seed idempotente do atendente de demo/BDD (acordo do grupo, 05/11).

O -os autentica atendentes por usuário Django (SimpleJWT,
api/v1/auth/token/). O BDD de stack real (Luís) e a demo precisam de um
usuário staff criado de forma determinística:

- ATENDENTE_USERNAME (default 'atendente.demo');
- ATENDENTE_PASSWORD: obrigatório — sem ele o comando não faz nada
  (aviso, exit 0) e nunca há senha fixa no código.

Sempre reseta a senha para o valor do ambiente (idempotente e mantém o
usuário de demo sincronizado com o .env/secret).
"""
import logging
import os

from django.core.management.base import BaseCommand

logger = logging.getLogger("os_service.seed")


class Command(BaseCommand):
    help = "Cria/atualiza o usuário atendente de demo/BDD (idempotente)."

    def handle(self, *args, **options):
        from os_service.models import User

        username = os.environ.get("ATENDENTE_USERNAME", "atendente.demo")
        password = os.environ.get("ATENDENTE_PASSWORD", "")
        if not password:
            self.stdout.write(
                "ATENDENTE_PASSWORD ausente: nada a fazer "
                f"(username default '{username}' não criado/atualizado)"
            )
            return

        user, criado = User.objects.get_or_create(
            username=username,
            defaults={"is_staff": True},
        )
        if criado:
            logger.info(
                "atendente_demo_criado", extra={"username": username}
            )
        user.is_staff = True
        user.set_password(password)
        user.save(update_fields=["password", "is_staff"])
        self.stdout.write(
            f"atendente '{username}' "
            f"{'criado' if criado else 'atualizado'} (senha do ambiente)"
        )
