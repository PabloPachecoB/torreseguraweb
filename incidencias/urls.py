from django.urls import path

from . import views


urlpatterns = [
    path('', views.revision_list, name='incidencia-revision-list'),
    path('<int:incidencia_id>/', views.revision_detail, name='incidencia-revision-detail'),
    path('<int:incidencia_id>/ajustar/', views.ajustar_revision, name='incidencia-revision-ajustar'),
    path('<int:incidencia_id>/aprobar/', views.aprobar_revision, name='incidencia-revision-aprobar'),
    path(
        '<int:incidencia_id>/solicitar-revision/',
        views.solicitar_revision,
        name='incidencia-revision-solicitar',
    ),
    path(
        '<int:incidencia_id>/evidencias/<int:evidencia_id>/',
        views.descargar_evidencia,
        name='incidencia-evidencia-descargar',
    ),
]
