"""URLs da API v1 do OS Service."""
from django.urls import path

from . import views

urlpatterns = [
    path('ordens-servico/', views.ordens_servico, name='ordens-servico'),
]
