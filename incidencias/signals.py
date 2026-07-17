from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EventoIncidencia, Incidencia


@receiver(post_save, sender=Incidencia)
def registrar_creacion_en_timeline(sender, instance, created, **kwargs):
    """Deja la primera entrada del timeline (INC-06) al crear la incidencia,
    para que el DoD "estado inicial y timeline visible" se cumpla desde el
    primer momento sin que cada vista tenga que acordarse de hacerlo.
    """
    if created:
        EventoIncidencia.objects.create(
            incidencia=instance,
            tipo_evento=EventoIncidencia.CREADA,
            estado_nuevo=instance.estado,
            usuario=instance.residente.usuario,
        )
