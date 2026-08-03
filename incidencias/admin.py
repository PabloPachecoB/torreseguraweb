from django.contrib import admin

from .models import (
    AprobacionIncidencia,
    EventoIncidencia,
    EvidenciaIncidencia,
    Incidencia,
    NotificacionIncidencia,
    OrdenTrabajo,
    RevisionIncidencia,
)


class EvidenciaIncidenciaInline(admin.TabularInline):
    model = EvidenciaIncidencia
    extra = 0
    readonly_fields = ('archivo', 'tipo', 'subido_por', 'fecha_subida')


class EventoIncidenciaInline(admin.TabularInline):
    model = EventoIncidencia
    extra = 0
    readonly_fields = ('tipo_evento', 'estado_anterior', 'estado_nuevo', 'comentario', 'usuario', 'fecha')


class RevisionIncidenciaInline(admin.TabularInline):
    model = RevisionIncidencia
    extra = 0
    readonly_fields = ('version', 'origen', 'creada_por', 'vigente', 'fecha_creacion')


@admin.register(Incidencia)
class IncidenciaAdmin(admin.ModelAdmin):
    list_display = ('id', 'titulo', 'categoria', 'estado', 'residente', 'fecha_creacion')
    list_filter = ('estado', 'categoria')
    search_fields = ('titulo', 'descripcion', 'residente__usuario__username')
    inlines = [EvidenciaIncidenciaInline, EventoIncidenciaInline, RevisionIncidenciaInline]


admin.site.register(RevisionIncidencia)
admin.site.register(AprobacionIncidencia)
admin.site.register(OrdenTrabajo)
admin.site.register(NotificacionIncidencia)
