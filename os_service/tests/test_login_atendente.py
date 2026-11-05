"""Login do atendente no -os (acordo do grupo, 05/11).

O -os já tinha TokenObtainPairView/TokenRefreshView (SimpleJWT + auditoria
de security) sem rota -- agora expõe api/v1/auth/token/ e
api/v1/auth/token/refresh/. O token do cliente continua no -auth (Lambda
por CPF). Seed idempotente do atendente de demo/BDD via env
ATENDENTE_USERNAME/ATENDENTE_PASSWORD.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase, override_settings

from os_service.models import User


class LoginAtendenteUrlTestCase(TestCase):
    LOGIN = "/api/v1/auth/token/"

    def _cria_atendente(self, username="atendente", password="senha-forte-1"):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(
            username=username, password=password, is_staff=True
        )
        return username, password

    def test_login_valido_devolve_access_e_refresh(self):
        username, password = self._cria_atendente()
        resposta = self.client.post(
            self.LOGIN, {"username": username, "password": password}
        )
        self.assertEqual(resposta.status_code, 200)
        corpo = resposta.json()
        self.assertIn("access", corpo)
        self.assertIn("refresh", corpo)

    def test_login_invalido_e_401(self):
        username, _ = self._cria_atendente()
        resposta = self.client.post(
            self.LOGIN, {"username": username, "password": "errada"}
        )
        self.assertEqual(resposta.status_code, 401)

    def test_refresh_devolve_access_novo(self):
        username, password = self._cria_atendente()
        login = self.client.post(
            self.LOGIN, {"username": username, "password": password}
        ).json()
        resposta = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": login["refresh"]}
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("access", resposta.json())

    def test_atendente_consome_token_no_endpoint_da_os(self):
        from django.contrib.auth import get_user_model

        username, password = self._cria_atendente()
        token = self.client.post(
            self.LOGIN, {"username": username, "password": password}
        ).json()["access"]
        resposta = self.client.get(
            "/api/v1/ordens-servico/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(resposta.status_code, 200)


class SeedAtendenteDemoTestCase(TestCase):
    def test_cria_atendente_staff_com_env(self):
        with override_settings(
            **{}  # management command lê os.environ, não settings
        ):
            import os

            username = f"atendente-{uuid.uuid4().hex[:8]}"
            password = "senha-do-bdd-1"
            os.environ["ATENDENTE_USERNAME"] = username
            os.environ["ATENDENTE_PASSWORD"] = password
            try:
                call_command("seed_atendente_demo", stdout=None)
                user = User.objects.get(username=username)
                self.assertTrue(user.is_staff)
                self.assertTrue(user.check_password(password))
            finally:
                os.environ.pop("ATENDENTE_USERNAME", None)
                os.environ.pop("ATENDENTE_PASSWORD", None)

    def test_idempotente_nao_duplica(self):
        import os

        username = f"atendente-{uuid.uuid4().hex[:8]}"
        os.environ["ATENDENTE_USERNAME"] = username
        os.environ["ATENDENTE_PASSWORD"] = "senha-do-bdd-1"
        try:
            call_command("seed_atendente_demo", stdout=None)
            call_command("seed_atendente_demo", stdout=None)
            user = User.objects.get(username=username)
            self.assertTrue(user.check_password("senha-do-bdd-1"))
            self.assertEqual(
                User.objects.filter(username=username).count(), 1
            )
        finally:
            os.environ.pop("ATENDENTE_USERNAME", None)
            os.environ.pop("ATENDENTE_PASSWORD", None)

    def test_sem_env_de_password_nao_levanta(self):
        import os

        os.environ.pop("ATENDENTE_PASSWORD", None)
        os.environ.pop("ATENDENTE_USERNAME", None)
        call_command("seed_atendente_demo", stdout=None)  # no-op avisando
