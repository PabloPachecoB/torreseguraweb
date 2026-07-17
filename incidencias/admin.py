from django.contrib import admin

from .models import EventoIncidencia, EvidenciaIncidencia, Incidencia


class EvidenciaIncidenciaInline(admin.TabularInline):
    model = EvidenciaIncidencia
    extra = 0
    readonly_fields = ('archivo', 'tipo', 'subido_por', 'fecha_subida')


class EventoIncidenciaInline(admin.TabularInline):
    model = EventoIncidencia
    extra = 0
    readonly_fields = ('tipo_evento', 'estado_anterior', 'estado_nuevo', 'comentario', 'usuario', 'fecha')


@admin.register(Incidencia)
class IncidenciaAdmin(admin.ModelAdmin):
    list_display = ('id', 'titulo', 'categoria', 'estado', 'residente', 'fecha_creacion')
    list_filter = ('estado', 'categoria')
    search_fields = ('titulo', 'descripcion', 'residente__usuario__username')
    inlines = [EvidenciaIncidenciaInline, EventoIncidenciaInline]
