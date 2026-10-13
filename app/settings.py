"""
Configuração Django do OS Service (Fase 4 — microsserviço de Ordem de Serviço).

Seguidores do padrão do monólito: decouple para env vars, sem fallback de
segredos, JWT na frente (o auth-vindo do tech-challenge-oficina-auth valida).
"""

from datetime import timedelta
from pathlib import Path

from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Segurança — segredos sem fallback: falha cedo se a env var sumir
# ---------------------------------------------------------------------------
SECRET_KEY = config('DJANGO_SECRET_KEY')

DEBUG = config('DJANGO_DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('DJANGO_ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

# ---------------------------------------------------------------------------
# Aplicações
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',
    'django_filters',
    'os_service',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'app.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'app.wsgi.application'

# ---------------------------------------------------------------------------
# Banco — Postgres dedicado deste serviço (nenhum outro serviço o acessa)
# ---------------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('POSTGRES_DB', default='os_service_db'),
        'USER': config('POSTGRES_USER', default='os_user'),
        'PASSWORD': config('POSTGRES_PASSWORD'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

# ---------------------------------------------------------------------------
# JWT — validado contra o tech-challenge-oficina-auth (Lucas)
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'os_service.domain.api_exceptions.custom_exception_handler',
    # Paginação global — evita retornar listas ilimitadas
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # Filtros — habilita ?status=RECEBIDA, ?data_abertura_after=2026-01-01, etc.
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    # Throttling global
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/hour',          # Usuários não autenticados
        'user': '600/hour',         # Usuários autenticados
        'consulta_cliente': '30/hour',  # Endpoint público de consulta
    },
}

# ---------------------------------------------------------------------------
# Swagger / OpenAPI (drf-spectacular)
# ---------------------------------------------------------------------------

SPECTACULAR_SETTINGS = {
    'TITLE': 'OS Service — Oficina Mecânica (Grupo 47)',
    'DESCRIPTION': 'Microsserviço de Ordens de Serviço e orquestrador da '
                   'Saga orquestrada. Parte da Fase 4 — Tech Challenge.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'ENUM_NAME_OVERRIDES': {
        'StatusOrdemServicoEnum': 'os_service.models.OrdemServico.STATUS_CHOICES',
        'StatusItemServicoEnum': 'os_service.models.ItemServicoOS.STATUS_CHOICES',
    },
}

# ---------------------------------------------------------------------------
# Internacionalização
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ---------------------------------------------------------------------------
# Comunicação entre serviços
# ---------------------------------------------------------------------------
# Endpoints dos serviços vizinhos (Billing, Execution). Preenchidos quando a
# Saga conectar os serviços; defaults localhost para a demo docker-compose.
BILLING_SERVICE_URL = config('BILLING_SERVICE_URL', default='http://localhost:8002')
EXECUTION_SERVICE_URL = config('EXECUTION_SERVICE_URL', default='http://localhost:8003')

# Fila (SQS na Fase 4 — configuração entra quando a Sophia subir a infra)
SAGA_QUEUE_URL = config('SAGA_QUEUE_URL', default='')
