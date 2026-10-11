# tech-challenge-oficina-os — OS Service (Fase 4)

Microsserviço de Ordem de Serviço da oficina mecânica (FIAP Postech SOAT,
Tech Challenge Fase 4). Extraído do monólito `tech-challenge-oficina`
(Fase 3), que permanece como referência arquitetural.

Escopo do serviço (enunciado Fase 4):

- Abertura da OS
- Atualização de status
- Consulta de status e histórico
- Orquestrador da Saga Pattern (orquestrada)

## Stack

- Python 3.11 / Django 5.1 / Django REST Framework (mesmo padrão do monólito)
- PostgreSQL dedicado deste serviço (nenhum outro serviço o acessa)
- Clean Architecture: `domain/` (regras puras), `application/` (use cases,
  DTOs, ports), `infrastructure/` (repositórios, adapters),
  `interfaces/api/` (views, serializers)
- JWT validado via `tech-challenge-oficina-auth` (Fase 3)

## Endpoints

- `GET /health/live/` — liveness
- `GET /health/ready/` — readiness (valida banco)
- `GET|POST /api/v1/ordens-servico/` — stub inicial (extração em curso)

Swagger/OpenAPI via drf-spectacular ( entra com os endpoints reais ).

## CI/CD

- `ci.yml` — build, Django check, pytest com cobertura (**--cov-fail-under=80**),
  build da imagem
- `cd.yml` — ECR + deploy no EKS (mesmo padrão OIDC da Fase 3)

## Rodando localmente

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
export DJANGO_SECRET_KEY=... POSTGRES_PASSWORD=...
python manage.py migrate && python manage.py runserver 8001
```

## Equipe — Grupo 80

| Papel na Fase 4 | Responsável |
|---|---|
| OS Service + orquestrador da Saga + template CI/CD | Hélio Mendes |
| Billing Service + Mercado Pago + auth | Lucas Marques |
| Execution Service (NoSQL) + infra/mensageria | Sophia Sussa |
| Contrato da Saga + tracing distribuído + BDD E2E | Luís Fernando Montes |
