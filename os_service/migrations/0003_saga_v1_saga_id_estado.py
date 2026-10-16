"""Saga v1: saga_id (chave), estado da máquina e motivo.

Contrato v1 (-saga PR #2): saga_id UUID é a chave da instância (uma OS pode
ter várias sagas); os_id deixa de ser unique; `estado` guarda o estado da
máquina saga-estados.yaml. Campos do rascunho (passo_atual,
passos_concluidos, compensation_pendente) permanecem para o orquestrador
legado durante a transição e caem na migração seguinte.
"""
import uuid

from django.db import migrations, models


def _uuid_default():
    return uuid.uuid4()


class Migration(migrations.Migration):
    dependencies = [
        ("os_service", "0002_processedevent_sagainstance"),
    ]

    operations = [
        migrations.AddField(
            model_name="sagainstance",
            name="saga_id",
            field=models.UUIDField(
                db_index=True,
                default=_uuid_default,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="sagainstance",
            name="estado",
            field=models.CharField(default="INICIADA", max_length=32),
        ),
        migrations.AddField(
            model_name="sagainstance",
            name="motivo",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="sagainstance",
            name="os_id",
            field=models.PositiveIntegerField(db_index=True),
        ),
        migrations.AddIndex(
            model_name="sagainstance",
            index=models.Index(
                fields=["os_id", "status"],
                name="saga_instan_os_id_s_d6a1b4_idx",
            ),
        ),
    ]
