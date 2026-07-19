from django.db import migrations


def backfill_revisions(apps, schema_editor):
    Incidencia = apps.get_model('incidencias', 'Incidencia')
    Revision = apps.get_model('incidencias', 'RevisionIncidencia')
    Aprobacion = apps.get_model('incidencias', 'AprobacionIncidencia')
    hours = {'CRITICA': 2, 'ALTA': 24, 'MEDIA': 48, 'BAJA': 120}
    costs = {
        'PLOMERIA': (100, 350),
        'ELECTRICIDAD': (120, 400),
        'ASCENSOR': (300, 1200),
        'SEGURIDAD': (150, 500),
        'LIMPIEZA': (50, 180),
        'OTRO': (None, None),
    }
    for incident in Incidencia.objects.select_related('residente__usuario').iterator():
        if Revision.objects.filter(incidencia_id=incident.pk).exists():
            continue
        estimate = incident.estimacion_preliminar or {}
        cost_min, cost_max = costs.get(incident.categoria, (None, None))
        revision = Revision.objects.create(
            incidencia_id=incident.pk,
            version=1,
            categoria=incident.categoria,
            prioridad=incident.urgencia,
            costo_estimado_min=estimate.get('estimated_cost_min', cost_min),
            costo_estimado_max=estimate.get('estimated_cost_max', cost_max),
            moneda=estimate.get('currency', 'BOB'),
            tiempo_estimado_horas=estimate.get(
                'estimated_hours', hours.get(incident.urgencia, 48),
            ),
            comentario='Evaluación migrada desde el reporte existente.',
            origen='AGENTE',
            creada_por_id=incident.residente.usuario_id,
            vigente=True,
        )
        Aprobacion.objects.create(
            revision_id=revision.pk,
            rol='RESIDENTE',
            decision='APROBADA',
            usuario_id=incident.residente.usuario_id,
            comentario='Reporte existente confirmado por el residente.',
        )


class Migration(migrations.Migration):
    dependencies = [
        ('incidencias', '0003_incidencia_empleado_asignado_revisionincidencia_and_more'),
    ]

    operations = [migrations.RunPython(backfill_revisions, migrations.RunPython.noop)]
