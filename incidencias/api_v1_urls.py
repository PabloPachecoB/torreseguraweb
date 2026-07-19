from django.urls import path

from . import api

urlpatterns = [
    path('crear/', api.crear_incidencia, name='api_v1_crear_incidencia'),
    path('mis-incidencias/', api.mis_incidencias, name='api_v1_mis_incidencias'),
    path('pendientes-revision/', api.pendientes_revision, name='api_v1_incidencias_pendientes_revision'),
    path('notificaciones/', api.notificaciones_incidencia, name='api_v1_notificaciones_incidencia'),
    path('<int:incidencia_id>/', api.detalle_incidencia, name='api_v1_detalle_incidencia'),
    path(
        '<int:incidencia_id>/evidencias/',
        api.agregar_evidencia,
        name='api_v1_agregar_evidencia',
    ),
    path(
        '<int:incidencia_id>/evidencias/<int:evidencia_id>/descargar/',
        api.descargar_evidencia,
        name='api_v1_descargar_evidencia',
    ),
    path(
        '<int:incidencia_id>/cambiar-estado/',
        api.cambiar_estado_incidencia,
        name='api_v1_cambiar_estado_incidencia',
    ),
    path('<int:incidencia_id>/revision/', api.ajustar_revision, name='api_v1_ajustar_revision_incidencia'),
    path('<int:incidencia_id>/aprobar/', api.aprobar_revision, name='api_v1_aprobar_revision_incidencia'),
    path('<int:incidencia_id>/solicitar-revision/', api.solicitar_revision, name='api_v1_solicitar_revision_incidencia'),
]
