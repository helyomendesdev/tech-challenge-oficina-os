"""
Settings de teste: SQLite em memória (mesmo padrão do monólito).
"""

from .settings import *  # noqa: F401,F403
from .settings import BASE_DIR

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db_test.sqlite3',
    }
}

ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Observabilidade (extraído do monólito) — exigido pelo adapter real
SERVICE_ENVIRONMENT = 'dev'
