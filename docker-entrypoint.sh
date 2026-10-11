#!/bin/sh
# Entrypoint do OS Service — mesmo padrão do monólito (Fase 3).
#.gunicorn sobe sob New Relic quando a credencial existe; migrations via Job/flag.

set -e

if [ "$RUN_MIGRATIONS" = "true" ]; then
    echo "INFO: aplicando migrations"
    python manage.py migrate --noinput
fi

if [ "$#" -eq 0 ]; then
    set -- gunicorn app.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60
fi

if [ "$1" = "gunicorn" ] && [ -n "$NEW_RELIC_LICENSE_KEY" ]; then
    echo "INFO: subindo sob o agente New Relic"
    exec newrelic-admin run-program "$@"
fi

exec "$@"
