"""URLs da API v1 do OS Service — extração dos endpoints de OS do monólito.

Rotas extraídas:
- Use cases (abrir, fila, status, orçamento, notificações)
- Rotas de itens de serviço (iniciar/finalizar) e CRUD legado de OS
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import legacy_views

router = DefaultRouter()
router.register(r'clientes', legacy_views.ClienteViewSet, basename='clientes')
router.register(r'veiculos', legacy_views.VeiculoViewSet, basename='veiculos')
router.register(r'ordens-servico', legacy_views.OrdemServicoViewSet, basename='ordens-servico')
router.register(r'itens-pecas', legacy_views.ItemPecaOSViewSet, basename='itens-pecas')
router.register(r'servicos', legacy_views.ServicoViewSet, basename='servicos')
router.register(r'pecas', legacy_views.PecaViewSet, basename='pecas')

urlpatterns = [
    # Use cases (Fase 2 extraídos)
    path('', include('os_service.interfaces.api.urls')),
    # Rotas de itens de serviço da OS (iniciar/finalizar)
    path(
        'ordens-servico/<int:os_pk>/servicos/',
        legacy_views.ItemServicoOSViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='os-servicos-list',
    ),
    path(
        'ordens-servico/<int:os_pk>/servicos/<int:pk>/',
        legacy_views.ItemServicoOSViewSet.as_view({'get': 'retrieve', 'delete': 'destroy'}),
        name='os-servicos-detail',
    ),
    path(
        'ordens-servico/<int:os_pk>/servicos/<int:pk>/iniciar/',
        legacy_views.ItemServicoOSViewSet.as_view({'post': 'iniciar'}),
        name='os-servicos-iniciar',
    ),
    path(
        'ordens-servico/<int:os_pk>/servicos/<int:pk>/finalizar/',
        legacy_views.ItemServicoOSViewSet.as_view({'post': 'finalizar'}),
        name='os-servicos-finalizar',
    ),
    path('', include(router.urls)),
]
