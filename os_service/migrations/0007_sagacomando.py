# Outbox da Saga (ADR-001 §3.5). Manuscrita: makemigrations nao roda com
# settings_test (padrao ja usado em 0002/0004/0005/0006).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('os_service', '0006_sagamensagem'),
    ]

    operations = [
        migrations.CreateModel(
            name='SagaComando',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message_id', models.UUIDField(unique=True)),
                ('saga_id', models.UUIDField(db_index=True)),
                ('os_id', models.IntegerField()),
                ('type', models.CharField(max_length=40)),
                ('payload', models.JSONField(default=dict)),
                ('request_id', models.CharField(max_length=128)),
                ('causation_id', models.UUIDField(blank=True, null=True)),
                ('occurred_at', models.DateTimeField()),
                ('traceparent', models.CharField(blank=True, default='', max_length=80)),
                ('criado_em', models.DateTimeField(auto_now_add=True)),
                ('publicado_em', models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={
                'db_table': 'saga_comando',
                'ordering': ['id'],
            },
        ),
    ]
