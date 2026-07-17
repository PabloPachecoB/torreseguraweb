from django.urls import path

from . import api, api_puertas

urlpatterns = [
    # Endpoints DRF/JWT existentes (mobile-ready)
    path("visitas/crear/", api.crear_visita, name="api_v1_crear_visita"),
    path("visitas/verificar-qr/", api.verificar_qr_visita, name="api_v1_verificar_qr_visita"),

    # Control de puertas
    path("puertas/", api_puertas.listar_puertas, name="api_v1_listar_puertas"),
    path("puertas/<int:puerta_id>/abrir/", api_puertas.abrir_puerta, name="api_v1_abrir_puerta"),
    path("puertas/aperturas/", api_puertas.historial_aperturas, name="api_v1_historial_aperturas"),
]
