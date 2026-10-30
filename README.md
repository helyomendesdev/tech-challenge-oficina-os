# tech-challenge-oficina-os — OS Service (Fase 4)

Microsserviço de Ordem de Serviço da oficina mecânica (FIAP Postech SOAT,
Tech Challenge Fase 4). Extraído do monólito `tech-challenge-oficina`
(Fase 3), que permanece como referência arquitetural.

[![CI](https://github.com/helyomendesdev/tech-challenge-oficina-os/actions/workflows/ci.yml/badge.svg)](https://github.com/helyomendesdev/tech-challenge-oficina-os/actions/workflows/ci.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=helyomendesdev_tech-challenge-oficina-os)](https://sonarcloud.io/summary/new_code?id=helyomendesdev_tech-challenge-oficina-os)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=helyomendesdev_tech-challenge-oficina-os&metric=coverage)](https://sonarcloud.io/summary/new_code?id=helyomendesdev_tech-challenge-oficina-os)

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
- `GET|POST /api/v1/ordens-servico/` — listagem/criação (extração concluída: PR #2)
- `/api/v1/ordens-servico/abrir/`, `/fila/`, `/<id>/status/`, `/orcamentos/notificacoes/` — use cases extraídos
- `/api/v1/ordens-servico/<os_pk>/servicos/<pk>/iniciar|finalizar/` — itens de serviço

Swagger/OpenAPI via drf-spectacular:

- `/api/schema/` — OpenAPI 3.0 (JSON)
- `/api/schema/swagger-ui/` — Swagger UI
- `/api/schema/redoc/` — ReDoc

## Arquitetura — Saga orquestrada (contrato v1)

O -os é o **orquestrador** da saga de faturamento/execução da OS. A máquina
de estados vem do contrato v1 do `-saga` (tag `v1.0.0`, vendida em
`contrato_v1.py`); serviço algum toca banco de serviço alheio — toda
comunicação é via envelope do contrato na fila.

```
OS aberta (POST /abrir/) ──▶ SagaOrquestradorV1.iniciar()
                                     │ INICIADA → ORCAMENTO_SOLICITADO
                                     ▼
                          ┌── fila de comandos SQS FIFO ◀──▶ Billing / Execution
                          │   (MessageGroupId = sagaId,            │
                          │    dedupe por messageId)               │ respostas
                          ▼                                        ▼
              SQSConsumidorRespostas ◀─── fila de respostas SQS FIFO
                          │ dedupe por messageId (ProcessedEvent)
                          │ orquestrador.processar() → máquina de estados
                          │ hook: DjangoOSStatusPort (OS acompanha a saga)
                          ▼
                   próxima transição/comando (ou fim)
```

- **Comandos** (`GerarOrcamento`, `SolicitarPagamento`, `IniciarExecucao`)
  saem pelo `SQSSagaPublisherV1` (`os_service/infrastructure/saga_adapters.py`).
- **Respostas** (`OrcamentoGerado/Aprovado/Recusado`, `PagamentoConfirmado`,
  `ExecucaoIniciada/Finalizada`, …) entram pelo
  `ConsumidorRespostasSaga` (`os_service/application/saga/consumidor.py`).
- **Idempotência**: toda resposta é dedupada por `messageId` na
  `ProcessedEvent` (at-least-once da fila nunca executa duas vezes).
- **Estado fora da máquina**: resposta incompatível com o estado atual é
  registrada e descartada (contrato, seção 4).
- **Ancoragem na OS** (decisão Q2 do contrato): `AGUARDANDO_APROVACAO → OS
  AGUARDANDO`, `EXECUCAO_SOLICITADA/EM_EXECUCAO → OS EXECUCAO` (com
  `data_inicio_execucao` na 1ª entrada, idempotente), `CONCLUIDA →
  FINALIZADA`, `CANCELADA → CANCELADA` — port `OSStatusPort`
  (`infrastructure/hook_os_status.py`) — falha no hook nunca reverte a saga;
- **Timeout de aprovação** (ADR-003): `checar_timeouts_orcamento` varre
  sagas em `AGUARDANDO_APROVACAO` há mais de `ORCAMENTO_TIMEOUT_MINUTOS`
  (30, configurável; fora do contrato) e manda `CancelarOrcamento
  motivo=TIMEOUT`. Em produção é CronJob `*/5` no `k8s/`.
- **Tracing distribuído**: `traceparent`/`tracestate` W3C via
  MessageAttributes do SQS (contrato do -saga) — publisher injeta,
  consumidor aceita antes de processar (`oficina_saga.newrelic_adapter`).

## Manifests K8s

Manifests por serviço em `k8s/` (a infraestrutura compartilhada continua
no `tech-challenge-oficina-k8s`): deployment (2 réplicas, HPA 2–6),
NodePort 30081, migration job, CronJob do timeout e worker de consumo das
respostas. Ordem de apply e pendências (`<ECR_REPO>:<IMAGE_TAG>` etc.)
em `k8s/README.md`.

## CI/CD

- `ci.yml` — build, Django check, pytest com cobertura (**--cov-fail-under=80**),
  build da imagem
- `cd.yml` — ECR + deploy no EKS via OIDC (mesmo padrão da Fase 3),
  substituindo `<ECR_REPO>:<IMAGE_TAG>` nos manifestos

## Qualidade e evidências

| Evidência | Onde verificar |
|---|---|
| Cobertura **90,26%** em **209 testes**, gate mínimo de 80% | badges Sonar no topo; o comando da suíte abaixo reproduz a medição |
| Quality Gate SonarCloud | badge "Quality Gate Status" — pipeline CI exige `build-and-test` verde |
| Proteção da main | `main` protegida: 1 review obrigatório, `enforce_admins`, `dismiss_stale_reviews`, `strict` (branch atualizada antes do merge), squash-only |
| History verificado | 5 squash merges de PRs revisados (#2–#14) — cada PR com diff+racional antes do merge |

## Rodando localmente

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
export DJANGO_SECRET_KEY=... POSTGRES_PASSWORD=...
python manage.py migrate && python manage.py runserver 8001
```

Suíte de teste (usa SQLite em arquivo — settings_test):

```bash
python -m pytest os_service/tests/ --cov=os_service --cov-fail-under=80 -q
```

Comandos de operação (sem fila real, para uso/testes):

```bash
python manage.py checar_timeouts_orcamento --dry-run   # varredura manual do timeout
python manage.py consumir_respostas_saga --once        # uma onda de consumo
python manage.py consumir_respostas_saga              # laço contínuo (worker)
```

## Equipe — Grupo 47

| Papel na Fase 4 | Responsável |
|---|---|
| OS Service + orquestrador da Saga + template CI/CD | Hélio Mendes |
| Billing Service + Mercado Pago + auth | Lucas Marques |
| Execution Service (NoSQL) + infra/mensageria | Sophia Sussa |
| Contrato da Saga + tracing distribuído + BDD E2E | Luís Fernando Montes |
