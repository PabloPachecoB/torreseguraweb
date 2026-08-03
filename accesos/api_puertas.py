"""API v1 de control de puertas.

Permisos por rol:
- Administrador / superuser: todas las puertas.
- Vigilante: puerta principal (y las de su edificio asignado, si tiene).
- Residente: puerta principal + puerta de su edificio + puerta de su vivienda.

Si la puerta tiene `webhook_url`, se envía la orden al hardware (ESP32/relé);
si no, la apertura queda en modo software (se registra y responde OK).
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Puerta, AperturaPuerta
from .services import allowed_doors, open_door, serialize_door, user_role

def _rol(user):
    return user_role(user)


def _puertas_permitidas(user):
    """Queryset de puertas activas que el usuario puede abrir."""
    return allowed_doors(user)


def _serializar_puerta(puerta):
    data = serialize_door(puerta)
    return {
        'id': data['id'],
        'nombre': data['name'],
        'tipo': data['type'],
        'tipo_display': data['type_display'],
        'edificio': data['building'],
        'vivienda': data['apartment'],
        'tiene_hardware': data['has_hardware'],
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def listar_puertas(request):
    """Puertas que el usuario autenticado puede abrir."""
    puertas = _puertas_permitidas(request.user)
    return Response([_serializar_puerta(p) for p in puertas])


def ejecutar_apertura(puerta, usuario):
    """Ejecuta la apertura física (webhook al ESP32 o modo software) y la
    registra en la bitácora. Devuelve (exito, detalle).

    HU-04.3: nunca fingir éxito — un timeout o error de red se reporta como
    fallo, jamás como apertura correcta.
    """
    result = open_door(usuario, puerta.pk)
    return bool(result.get('success')), result.get('message', '')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def abrir_puerta(request, puerta_id):
    """Paso 1 de HU-04.2: solicita la apertura.

    No abre nada todavía: valida permiso + demo controlada y crea una
    AgentAction PENDIENTE (tipo CERRADURA_ABRIR) que el usuario debe
    confirmar con su contraseña (segundo factor) en
    POST /api/v1/agente/acciones/<id>/confirmar/.
    """
    try:
        puerta = _puertas_permitidas(request.user).get(pk=puerta_id)
    except Puerta.DoesNotExist:
        return Response(
            {'abierta': False, 'mensaje': 'Puerta no encontrada o sin permiso para abrirla.'},
            status=403,
        )

    # LOCK-04: la apertura remota solo funciona en demo controlada.
    if not puerta.habilitada_para_demo:
        return Response(
            {'abierta': False, 'mensaje': 'Esta puerta no está habilitada para apertura remota (demo controlada).'},
            status=403,
        )

    from datetime import timedelta
    from django.utils import timezone
    from agente.models import AgentAction
    from uuid import uuid4

    # Reusar una solicitud pendiente reciente de esta misma puerta/usuario en
    # vez de acumular acciones colgando si el residente toca "Abrir" varias veces.
    ahora = timezone.now()
    accion = (
        AgentAction.objects.filter(
            usuario=request.user,
            tipo_accion='CERRADURA_ABRIR',
            estado=AgentAction.PENDIENTE,
            payload__door_id=puerta.id,
            expira_en__gt=ahora,
        )
        .order_by('-fecha_creacion')
        .first()
    )
    if accion is None:
        accion = AgentAction.objects.create(
            usuario=request.user,
            tipo_accion='CERRADURA_ABRIR',
            payload={'door_id': puerta.id},
            idempotency_key=uuid4().hex,
            tool_name='open_door',
            expira_en=ahora + timedelta(minutes=5),
        )

    return Response(
        {
            'abierta': False,
            'requiere_confirmacion': True,
            'accion_id': accion.id,
            'mensaje': f'Confirma la apertura de {puerta.nombre} con tu contraseña.',
            'puerta': _serializar_puerta(puerta),
        },
        status=202,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def historial_aperturas(request):
    """Últimas aperturas visibles para el usuario (admin/vigilante ven todo lo suyo permitido; residente solo las propias)."""
    rol = _rol(request.user)
    qs = AperturaPuerta.objects.select_related('puerta', 'usuario')
    if not (request.user.is_superuser or rol in ['Administrador', 'Vigilante']):
        qs = qs.filter(usuario=request.user)
    data = [
        {
            'id': a.id,
            'puerta': a.puerta.nombre,
            'usuario': a.usuario.username if a.usuario else None,
            'fecha_hora': a.fecha_hora.isoformat(),
            'exito': a.exito,
            'detalle': a.detalle,
        }
        for a in qs[:50]
    ]
    return Response(data)
