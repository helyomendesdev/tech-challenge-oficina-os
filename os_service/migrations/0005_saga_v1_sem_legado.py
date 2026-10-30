# Fatia C (realinhamento): campos do rascunho v0 removidos da SagaInstance.
# Makemigrations não roda com settings_test (trava esperando Postgres) —
# migration manuscrita (padrão já usado em 0002/0004).
from django.db import migrations, models

from os_service.application.saga.dto import StatusSaga


def noop(apps, schema_editor):
    pass  # nada a preencher: os campos saem com seus dados


class Migration(migrations.Migration):

    dependencies = [
        ('os_service', '0004_saga_v1_referencias_estado_desde'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sagainstance',
            name='status',
            field=models.CharField(
                choices=[(s.value, s.value) for s in StatusSaga],
                default='EM_ANDAMENTO',
                max_length=20,
            ),
        ),
        migrations.RemoveField(
            model_name='sagainstance',
            name='passo_atual',
        ),
        migrations.RemoveField(
            model_name='sagainstance',
            name='passos_concluidos',
        ),
        migrations.RemoveField(
            model_name='sagainstance',
            name='compensation_pendente',
        ),
        migrations.RemoveField(
            model_name='sagainstance',
            name='event_ids_processados',
        ),
        migrations.RemoveField(
            model_name='sagainstance',
            name='falha_motivo',
        ),
        migrations.RunPython(noop, migrations.RunPython.noop),
    ]
