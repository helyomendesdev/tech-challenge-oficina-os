# Imagem base oficial do Python
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

RUN chown -R appuser:appuser /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready/ || exit 1

USER appuser

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "app.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
