"""API v1 de control de puertas.

Permisos por rol:
- Administrador / superuser: todas las puertas.
- Vigilante: puerta principal (y las de su edificio asignado, si tiene).
- Residente: puerta principal + puerta de su edificio + puerta de su vivienda.

Si la puerta tiene `webhook_url`, se envía la orden al hardware (ESP32/relé);
si no, la apertura queda en modo software (se registra y responde OK).
"""
import requests
from django.conf import settings
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from viviendas.models import Residente
from .models import Puerta, AperturaPuerta

WEBHOOK_TIMEOUT = 5  # segundos


def _rol(user):
    return getattr(getattr(user, 'rol', None), 'nombre', None)


def _puertas_permitidas(user):
    """Queryset de puertas activas que el usuario puede abrir."""
    qs = Puerta.objects.filter(activa=True).select_related('edificio', 'vivienda', 'vivienda__edificio')
    rol = _rol(user)

    if user.is_superuser or rol == 'Administrador':
        return qs

    if rol == 'Vigilante':
        filtro = Q(tipo=Puerta.TIPO_PRINCIPAL)
        vigilante = getattr(user, 'vigilante', None)
        if vigilante and getattr(vigilante, 'edificio', None):
            filtro |= Q(tipo=Puerta.TIPO_EDIFICIO, edificio=vigilante.edificio)
        return qs.filter(filtro)

    if rol == 'Residente':
        try:
            residente = Residente.objects.select_related('vivienda__edificio').get(usuario=user, activo=True)
        except Residente.DoesNotExist:
            return qs.none()
        if not residente.vivienda:
            return qs.filter(tipo=Puerta.TIPO_PRINCIPAL)
        return qs.filter(
            Q(tipo=Puerta.TIPO_PRINCIPAL)
            | Q(tipo=Puerta.TIPO_EDIFICIO, edificio=residente.vivienda.edificio)
            | Q(tipo=Puerta.TIPO_VIVIENDA, vivienda=residente.vivienda)
        )

    return qs.none()


def _serializar_puerta(puerta):
    return {
        'id': puerta.id,
        'nombre': puerta.nombre,
        'tipo': puerta.tipo,
        'tipo_display': puerta.get_tipo_display(),
        'edificio': str(puerta.edificio) if puerta.edificio else None,
        'vivienda': str(puerta.vivienda) if puerta.vivienda else None,
        'tiene_hardware': bool(puerta.webhook_url),
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
    exito = True
    detalle = 'Apertura en modo software (sin hardware conectado).'

    if puerta.webhook_url:
        headers = {}
        token = getattr(settings, 'PUERTA_WEBHOOK_TOKEN', '')
        if token:
            headers['X-Auth-Token'] = token
        try:
            resp = requests.post(
                puerta.webhook_url,
                json={'accion': 'abrir', 'puerta_id': puerta.id, 'usuario': usuario.username},
                headers=headers,
                timeout=WEBHOOK_TIMEOUT,
            )
            exito = resp.ok
            detalle = f'Hardware respondió HTTP {resp.status_code}.'
        except requests.Timeout:
            exito = False
            detalle = 'Timeout: el hardware no respondió a tiempo.'
        except requests.RequestException as e:
            exito = False
            detalle = f'No se pudo contactar el hardware: {type(e).__name__}'

    AperturaPuerta.objects.create(
        puerta=puerta,
        usuario=usuario,
        exito=exito,
        detalle=detalle[:200],
    )
    return exito, detalle


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

    # Reusar una solicitud pendiente reciente de esta misma puerta/usuario en
    # vez de acumular acciones colgando si el residente toca "Abrir" varias veces.
    ahora = timezone.now()
    accion = (
        AgentAction.objects.filter(
            usuario=request.user,
            tipo_accion='CERRADURA_ABRIR',
            estado=AgentAction.PENDIENTE,
            payload__puerta_id=puerta.id,
            expira_en__gt=ahora,
        )
        .order_by('-fecha_creacion')
        .first()
    )
    if accion is None:
        accion = AgentAction.objects.create(
            usuario=request.user,
            tipo_accion='CERRADURA_ABRIR',
            payload={'puerta_id': puerta.id, 'puerta_nombre': puerta.nombre},
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
