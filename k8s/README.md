# Manifests K8s do OS Service (Fase 4)

Manifests por serviço no repo (exigência do enunciado da fase). O
`tech-challenge-oficina-k8s` (Sophia) continua a fonte de verdade da
infraestrutura compartilhada: cluster, SQS, S3 remote state, namespace,
secrets reais do auth/newrelic/ecr. Aqui ficam apenas os recursos do
serviço os-service.

## Conteúdo

| Arquivo | Recurso | Papel |
|---|---|---|
| `deployment.yaml` | Deployment `os-service` | app Django (2 réplicas, probes live/ready) |
| `service.yaml` | Service `os-service` | NodePort 30081 |
| `hpa.yaml` | HPA 2–6 réplicas | escala por CPU/memória |
| `migration-job.yaml` | Job `os-service-migrate` | `manage.py migrate` antes do deploy |
| `cronjob-timeout-orcamento.yaml` | CronJob `*/5 * * * *` | gatilho do timeout de aprovação (ADR-003): `checar_timeouts_orcamento` |
| `worker-consumer.yaml` | Deployment `os-service-consumer-respostas` | worker `consumir_respostas_saga` em laço contínuo |
| `configmap.yaml` | ConfigMap `os-service-config` | env de config (com placeholders) |
| `secret.example.yaml` | Secret `os-service-secret` | exemplo — Secret real via `kubectl create secret`, nunca versionado |

## Ordem de apply (1ª vez)

```bash
kubectl apply -f k8s/configmap.yaml
# Secret real (não o exemplo):
kubectl create secret generic os-service-secret -n oficina --from-literal=DJANGO_SECRET_KEY='<chave>' --from-literal=POSTGRES_DB=os_service_db --from-literal=POSTGRES_USER=os_user --from-literal='POSTGRES_PASSWORD=<senha>'
# namespace, ecr-registry-secret, newrelic-license e oficina-auth: já cuidadas pelo tech-challenge-oficina-k8s
kubectl apply -f k8s/migration-job.yaml
kubectl apply -f k8s/deployment.yaml -f k8s/service.yaml -f k8s/hpa.yaml
kubectl apply -f k8s/cronjob-timeout-orcamento.yaml -f k8s/worker-consumer.yaml
```

## Pendências para o deploy real

- `<ECR_REPO>:<IMAGE_TAG>`: substituídos pelo CD do repo (`cd.yml`) quando os secrets AWS subirem.
- `SAGA_QUEUE_URL` / `SAGA_RESPONSE_QUEUE_URL`: filas FIFO do SQS (responsabilidade da Sophia no -k8s); acesso dos pods via IRSA (policy por role, nunca credenciais de usuário no Secret).
- `<RDS_ENDPOINT_DO_OS>`: instância RDS própria do -os. Banco por serviço é exigência da fase — não reaproveitar o RDS do monólito.

## Referências

- Contrato da saga v1: `tech-challenge-oficina-saga` (tag v1.0.0) — envelope, ordem da fila (`MessageGroupId = sagaId`), trace W3C via MessageAttributes.
- Observabilidade: New Relic APM (padrão F3) + logs JSON no stdout.
