# Historico de mensagens da Saga (rota de consulta). Manuscrita: makemigrations
# nao roda com settings_test (padrao ja usado em 0002/0004/0005).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('os_service', '0005_saga_v1_sem_legado'),
    ]

    operations = [
        migrations.CreateModel(
            name='SagaMensagem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('saga_id', models.UUIDField(db_index=True)),
                ('type', models.CharField(max_length=40)),
                ('message_id', models.UUIDField(unique=True)),
                ('causation_id', models.UUIDField(blank=True, null=True)),
                ('occurred_at', models.DateTimeField()),
                ('direcao', models.CharField(choices=[('ENVIADA', 'ENVIADA'), ('RECEBIDA', 'RECEBIDA')], max_length=8)),
                ('request_id', models.CharField(max_length=128)),
                ('trace_id', models.CharField(max_length=32)),
            ],
            options={
                'db_table': 'saga_mensagem',
                'ordering': ['occurred_at', 'id'],
            },
        ),
    ]
