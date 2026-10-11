"""Teste de smoke do esqueleto do OS Service."""

import pytest
from django.test import Client


pytestmark = pytest.mark.django_db


def test_health_live():
    resp = Client().get('/health/live/')
    assert resp.status_code == 200
    assert resp.json() == {'status': 'ok'}


def test_health_ready_sqlite():
    resp = Client().get('/health/ready/')
    assert resp.status_code == 200
    assert resp.json() == {'status': 'ready'}


def test_endpoint_stub_exige_jwt():
    resp = Client().get('/api/v1/ordens-servico/')
    assert resp.status_code == 401
