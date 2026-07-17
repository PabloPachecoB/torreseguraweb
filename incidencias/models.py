from django.conf import settings
from django.db import models

from viviendas.models import Residente


class Incidencia(models.Model):
    """Reporte de un problema en el condominio (HU-03.1), con evidencia adjunta
    y un timeline de eventos visible (INC-06).

    A diferencia de `agente.AgentAction`, esta SI necesita historial completo
    (una incidencia pasa por varios estados a lo largo de dias), por eso el
    timeline vive en un modelo aparte (`EventoIncidencia`), no en un campo
    `estado_previo`.
    """

    PLOMERIA = 'PLOMERIA'
    ELECTRICIDAD = 'ELECTRICIDAD'
    ASCENSOR = 'ASCENSOR'
    SEGURIDAD = 'SEGURIDAD'
    LIMPIEZA = 'LIMPIEZA'
    OTRO = 'OTRO'
    CATEGORIAS = [
        (PLOMERIA, 'Plomeria'),
        (ELECTRICIDAD, 'Electricidad'),
        (ASCENSOR, 'Ascensor'),
        (SEGURIDAD, 'Seguridad'),
        (LIMPIEZA, 'Limpieza'),
        (OTRO, 'Otro'),
    ]

    REPORTADA = 'REPORTADA'
    EN_REVISION = 'EN_REVISION'
    APROBADA = 'APROBADA'
    EN_PROCESO = 'EN_PROCESO'
    RESUELTA = 'RESUELTA'
    RECHAZADA = 'RECHAZADA'
    CANCELADA = 'CANCELADA'
    ESTADOS = [
        (REPORTADA, 'Reportada'),
        (EN_REVISION, 'En revision'),
        (APROBADA, 'Aprobada'),
        (EN_PROCESO, 'En proceso'),
        (RESUELTA, 'Resuelta'),
        (RECHAZADA, 'Rechazada'),
        (CANCELADA, 'Cancelada'),
    ]

    residente = models.ForeignKey(
        Residente, on_delete=models.CASCADE, related_name='incidencias',
    )
    categoria = models.CharField(max_length=20, choices=CATEGORIAS, default=OTRO)
    titulo = models.CharField(max_length=150)
    descripcion = models.TextField()
    estado = models.CharField(max_length=15, choices=ESTADOS, default=REPORTADA)

    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Incidencia'
        verbose_name_plural = 'Incidencias'
        ordering = ['-fecha_creacion']

    def __str__(self):
        return f'{self.titulo} ({self.estado}) — {self.residente}'

    def cambiar_estado(self, usuario, nuevo_estado, comentario=''):
        """Transiciona el estado y deja un EventoIncidencia con el registro del
        cambio, para que el timeline (INC-06) siempre refleje la historia real.
        """
        estado_anterior = self.estado
        self.estado = nuevo_estado
        self.save(update_fields=['estado', 'fecha_actualizacion'])

        EventoIncidencia.objects.create(
            incidencia=self,
            tipo_evento=EventoIncidencia.CAMBIO_ESTADO,
            estado_anterior=estado_anterior,
            estado_nuevo=nuevo_estado,
            comentario=comentario,
            usuario=usuario,
        )
        return self


class EvidenciaIncidencia(models.Model):
    """Un archivo adjunto (foto/video/documento) de una incidencia.

    "Almacenamiento privado" (DoR): el archivo se guarda en `media/` como
    cualquier otro FileField del proyecto, pero nunca se expone su URL cruda
    en la API — solo se sirve via el endpoint `descargar_evidencia`, que
    valida permisos antes de devolver el archivo (ver `incidencias/api.py`).
    """

    FOTO = 'FOTO'
    VIDEO = 'VIDEO'
    DOCUMENTO = 'DOCUMENTO'
    TIPOS = [
        (FOTO, 'Foto'),
        (VIDEO, 'Video'),
        (DOCUMENTO, 'Documento'),
    ]

    incidencia = models.ForeignKey(
        Incidencia, on_delete=models.CASCADE, related_name='evidencias',
    )
    archivo = models.FileField(upload_to='incidencias/evidencia/%Y/%m/%d/')
    tipo = models.CharField(max_length=10, choices=TIPOS, default=FOTO)
    subido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
    )
    fecha_subida = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Evidencia de incidencia'
        verbose_name_plural = 'Evidencias de incidencia'
        ordering = ['fecha_subida']

    def __str__(self):
        return f'Evidencia #{self.pk} — {self.incidencia}'


class EventoIncidencia(models.Model):
    """Una entrada del timeline de una incidencia (INC-06). Es un log real
    (a diferencia de AgentAction): se agrega una fila nueva por cada evento,
    nunca se pisa una existente.
    """

    CREADA = 'CREADA'
    CAMBIO_ESTADO = 'CAMBIO_ESTADO'
    COMENTARIO = 'COMENTARIO'
    TIPOS_EVENTO = [
        (CREADA, 'Incidencia creada'),
        (CAMBIO_ESTADO, 'Cambio de estado'),
        (COMENTARIO, 'Comentario'),
    ]

    incidencia = models.ForeignKey(
        Incidencia, on_delete=models.CASCADE, related_name='eventos',
    )
    tipo_evento = models.CharField(max_length=15, choices=TIPOS_EVENTO)
    estado_anterior = models.CharField(max_length=15, blank=True, default='')
    estado_nuevo = models.CharField(max_length=15, blank=True, default='')
    comentario = models.TextField(blank=True, default='')
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
    )
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Evento de incidencia'
        verbose_name_plural = 'Eventos de incidencia'
        ordering = ['fecha']

    def __str__(self):
        return f'{self.get_tipo_evento_display()} — {self.incidencia} ({self.fecha:%d/%m/%Y %H:%M})'
