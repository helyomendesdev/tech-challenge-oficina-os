"""Testes de OpenAPI/Swagger (rotas /api/schema/*)."""
from django.test import TestCase


class OpenAPISchemaTestCase(TestCase):
    def test_schema_endpoint_200(self):
        response = self.client.get('/api/schema/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('openapi', response.content.decode()[:60])

    def test_swagger_ui_responde(self):
        response = self.client.get('/api/schema/swagger-ui/')
        self.assertEqual(response.status_code, 200)

    def test_redoc_responde(self):
        response = self.client.get('/api/schema/redoc/')
        self.assertEqual(response.status_code, 200)

    def test_schema_declara_endpoints_principais(self):
        import json

        response = self.client.get(
            '/api/schema/', HTTP_ACCEPT='application/json'
        ).content.decode()
        schema = json.loads(response)
        paths = set(schema['paths'].keys())

        esperados = {
            '/api/v1/ordens-servico/abrir/',
            '/api/v1/ordens-servico/fila/',
            '/api/v1/ordens-servico/{ordem_servico_id}/status/',
            '/api/v1/ordens-servico/{os_pk}/servicos/{id}/iniciar/',
            '/api/v1/ordens-servico/{os_pk}/servicos/{id}/finalizar/',
            '/api/v1/ordens-servico/',
        }
        faltando = esperados - paths
        self.assertFalse(
            faltando,
            f"Endpoints de OS ausentes no schema: {faltando}",
        )
