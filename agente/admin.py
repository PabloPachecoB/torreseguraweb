from django.contrib import admin

from .models import AgentAction


@admin.register(AgentAction)
class AgentActionAdmin(admin.ModelAdmin):
    list_display = ('id', 'usuario', 'tipo_accion', 'estado', 'fecha_creacion', 'fecha_confirmacion')
    list_filter = ('estado', 'tipo_accion')
    search_fields = ('usuario__username', 'tipo_accion')
    readonly_fields = ('fecha_creacion', 'fecha_confirmacion', 'confirmada_por', 'resultado')
