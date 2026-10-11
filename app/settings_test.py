"""
Settings de teste: SQLite em memória (mesmo padrão do monólito).
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]
