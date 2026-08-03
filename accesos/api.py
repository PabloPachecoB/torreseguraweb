import base64
import io
import json

import qrcode
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accesos.qr_firma_utils import generar_firma_qr, verificar_firma_qr
from viviendas.models import Residente, Vivienda

from .models import MovimientoResidente, Visita

@login_required
def historial_visitas(request):
    """API endpoint para obtener el historial de visitas completadas (con salida registrada)"""
    user = request.user
    rol_nombre = getattr(getattr(user, 'rol', None), 'nombre', None)
    
    if rol_nombre not in ['Administrador', 'Gerente', 'Vigilante']:
        return JsonResponse({'error': 'No autorizado'}, status=403)
    
    visitas = Visita.objects.filter(
        fecha_hora_salida__isnull=False
    ).select_related(
        'vivienda_destino', 'vivienda_destino__edificio',
        'residente_autoriza', 'residente_autoriza__usuario',
        'registrado_por',
    ).order_by('-fecha_hora_salida')

    # Gerente solo ve visitas de su edificio
    if rol_nombre == 'Gerente' and hasattr(user, 'gerente') and user.gerente and user.gerente.edificio:
        visitas = visitas.filter(vivienda_destino__edificio=user.gerente.edificio)

    # Vigilante solo ve visitas de su edificio asignado
    if rol_nombre == 'Vigilante':
        if hasattr(user, 'vigilante') and user.vigilante and user.vigilante.edificio:
            visitas = visitas.filter(vivienda_destino__edificio=user.vigilante.edificio)
        else:
            visitas = visitas.none()

    visitas = visitas[:100]
    
    data = []
    for visita in visitas:
        data.append({
            'id': visita.id,
            'nombre_visitante': visita.nombre_visitante,
            'documento_visitante': visita.documento_visitante,
            'vivienda_destino': str(visita.vivienda_destino),
            'residente_autoriza': str(visita.residente_autoriza),
            'fecha_hora_entrada': visita.fecha_hora_entrada.isoformat(),
            'fecha_hora_salida': visita.fecha_hora_salida.isoformat() if visita.fecha_hora_salida else None,
            'motivo': visita.motivo,
            'registrado_por': str(visita.registrado_por) if visita.registrado_por else None
        })
    
    return JsonResponse(data, safe=False)

@login_required
def residentes_por_vivienda(request, vivienda_id):
    """API endpoint para obtener los residentes de una vivienda específica (solo activos)"""
    user = request.user
    rol_nombre = getattr(getattr(user, 'rol', None), 'nombre', None)
    
    if rol_nombre not in ['Administrador', 'Gerente', 'Vigilante']:
        return JsonResponse({'error': 'No autorizado'}, status=403)
    
    try:
        vivienda = Vivienda.objects.get(pk=vivienda_id)
        
        # Gerente solo puede ver residentes de su edificio
        if rol_nombre == 'Gerente' and hasattr(user, 'gerente') and user.gerente and user.gerente.edificio:
            if vivienda.edificio != user.gerente.edificio:
                return JsonResponse({'error': 'No autorizado'}, status=403)
        
        residentes = vivienda.residentes.filter(activo=True).select_related('usuario')

        data = []
        for residente in residentes:
            data.append({
                'id': residente.id,
                'nombre': f"{residente.usuario.first_name} {residente.usuario.last_name}",
                'tipo': residente.tipo_residente,
                'es_propietario': residente.es_propietario,
                'activo': residente.activo
            })
        
        return JsonResponse(data, safe=False)
    except Vivienda.DoesNotExist:
        return JsonResponse({'error': 'Vivienda no encontrada'}, status=404)
    except Exception:
        return JsonResponse({'error': 'Error interno'}, status=500)

@require_GET
@login_required
def generar_qr_visita(request, visita_id):
    try:
        visita = Visita.objects.get(pk=visita_id, residente_autoriza__usuario=request.user)

        if visita.fecha_hora_entrada:
            fecha_str = visita.fecha_hora_entrada.strftime("%Y-%m-%d %H:%M")
        elif visita.fecha_visita:
            fecha_str = f"{visita.fecha_visita} {visita.hora_inicio}-{visita.hora_fin}"
        else:
            fecha_str = None

        datos_qr = {
            "id": visita.id,
            "nonce": visita.qr_nonce,
            "nombre_visitante": visita.nombre_visitante,
            "documento_visitante": visita.documento_visitante,
            "vivienda": str(visita.vivienda_destino),
            "autorizado_por": str(visita.residente_autoriza),
            "fecha": fecha_str,
            "cantidad_personas": visita.cantidad_personas,
            "firma": generar_firma_qr(visita.id, nonce=visita.qr_nonce),
        }

        qr = qrcode.make(datos_qr)
        buffer = io.BytesIO()
        qr.save(buffer, format='PNG')
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()

        return JsonResponse({"qr_base64": qr_base64})
    
    except Visita.DoesNotExist:
        return JsonResponse({"error": "Visita no encontrada o no autorizada para este usuario"}, status=404)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def verificar_qr_visita(request):
    # Solo seguridad (Vigilante) y Administrador deben verificar QRs
    rol = getattr(getattr(request.user, 'rol', None), 'nombre', None)
    if not (request.user.is_superuser or rol in ['Vigilante', 'Administrador']):
        return Response(
            {'valido': False, 'mensaje': 'No tienes permisos para verificar códigos QR.'},
            status=403,
        )

    data = request.data
    visita_id = data.get('id')
    firma = data.get('firma')
    nonce = data.get('nonce')

    if not visita_id or not firma:
        return Response({'valido': False, 'mensaje': 'ID y firma requeridos.'}, status=400)

    try:
        # Lock de fila + atomicidad: dos escaneos simultáneos del mismo QR no
        # pueden ambos pasar el chequeo anti-replay (evita doble ingreso).
        with transaction.atomic():
            visita = Visita.objects.select_for_update().get(id=visita_id)

            # Anti-replay: si ya fue usado, rechazar
            if getattr(visita, 'qr_usado', False):
                return Response(
                    {'valido': False, 'mensaje': 'Este QR ya fue utilizado.'},
                    status=409,
                )

            return _procesar_qr_visita(visita, visita_id, firma, nonce)

    except Visita.DoesNotExist:
        return Response({'valido': False, 'mensaje': 'No se encontró una visita válida con ese ID.'}, status=404)


def _procesar_qr_visita(visita, visita_id, firma, nonce):
    """Valida firma/ventana y consume el QR. Corre dentro del lock de la visita."""
    # Verificar la firma antes de continuar
    # - Si el request trae nonce, validamos con el esquema nuevo.
    # - Si no trae nonce, aceptamos temporalmente el esquema antiguo por compatibilidad.
    if nonce:
        firma_ok = verificar_firma_qr(int(visita_id), firma, nonce=str(nonce))
    else:
        firma_ok = verificar_firma_qr(int(visita_id), firma)

    if not firma_ok:
        return Response({'valido': False, 'mensaje': 'QR inválido o alterado.'}, status=403)

    if visita.fecha_hora_salida:
        return Response({'valido': False, 'mensaje': 'Esta visita ya fue finalizada.'})

    # Ventana de validez (solo aplica a reservas a futuro, no a registros inmediatos)
    if visita.ventana_no_iniciada:
        return Response({
            'valido': False,
            'mensaje': f'Esta visita todavia no esta habilitada. Ingreso valido desde las {visita.hora_inicio}.',
        }, status=403)
    if visita.ventana_expirada:
        if visita.estado == Visita.RESERVADA:
            visita.estado = Visita.EXPIRADA
            visita.save(update_fields=['estado'])
        return Response({
            'valido': False,
            'mensaje': f'El QR expiro — la ventana de ingreso era hasta las {visita.hora_fin}.',
        }, status=403)

    # Consumir QR (anti-replay): se marca usado al primer escaneo exitoso
    visita.qr_usado = True
    visita.qr_usado_en = timezone.now()
    visita.fecha_hora_entrada = visita.fecha_hora_entrada or timezone.now()
    visita.estado = Visita.CONFIRMADA
    visita.save(update_fields=['qr_usado', 'qr_usado_en', 'fecha_hora_entrada', 'estado'])

    return Response({
        'valido': True,
        'mensaje': 'QR verificado correctamente.',
        'visitante': visita.nombre_visitante,
        'documento': visita.documento_visitante,
        'vivienda': str(visita.vivienda_destino),
        'fecha': visita.fecha_hora_entrada.strftime('%Y-%m-%d %H:%M'),
        'cantidad_personas': visita.cantidad_personas,
        'motivo': visita.motivo,
        'autorizado_por': str(visita.residente_autoriza)
    })



@api_view(['POST'])
@permission_classes([IsAuthenticated])
def crear_visita(request):
    """Crea una visita. Si no se manda `fecha_visita`/`hora_inicio`/`hora_fin`,
    se comporta exactamente igual que antes: ingreso inmediato, un solo visitante
    (`cantidad_personas` default 1).

    Si SI se mandan esos tres campos, es una RESERVA a futuro para un grupo de
    `cantidad_personas` personas — el QR queda valido solo dentro de esa ventana
    horaria (`hora_inicio`-`hora_fin` de `fecha_visita`) y `fecha_hora_entrada`
    queda vacia hasta que el vigilante confirme el ingreso real por QR.
    """
    data = request.data
    nombre = data.get('nombre_visitante')
    documento = data.get('documento_visitante')
    vivienda_id = data.get('vivienda_destino_id')
    fecha_visita = data.get('fecha_visita')
    hora_inicio = data.get('hora_inicio')
    hora_fin = data.get('hora_fin')
    if not all([nombre, documento, vivienda_id]):
        return Response({'error': 'Faltan datos obligatorios'}, status=400)

    es_reserva = bool(fecha_visita or hora_inicio or hora_fin)
    if es_reserva and not all([fecha_visita, hora_inicio, hora_fin]):
        missing = [
            key for key, value in (
                ('fecha_visita', fecha_visita),
                ('hora_inicio', hora_inicio),
                ('hora_fin', hora_fin),
            ) if not value
        ]
        return Response(
            {
                'error': 'Para reservar a futuro son obligatorios fecha_visita, hora_inicio y hora_fin.',
                'campos_faltantes': missing,
            },
            status=400,
        )

    from .services import create_visit_authorization

    idempotency_key = request.headers.get('Idempotency-Key') or None
    if es_reserva:
        result = create_visit_authorization(
            request.user,
            {
                'name': nombre,
                'document': documento,
                'apartment_id': vivienda_id,
                'attendees': data.get('cantidad_personas', 1),
                'date': fecha_visita,
                'start_time': hora_inicio,
                'end_time': hora_fin,
                'reason': data.get('motivo', ''),
            },
            idempotency_key=idempotency_key,
        )
        if result['status'] != 'success':
            status_code = 403 if result['status'] == 'unauthorized' else 409 if result['status'] == 'conflict' else 400
            return Response({'error': result['message'], 'error_code': result['error_code']}, status=status_code)
        visita = Visita.objects.get(pk=result['visit_id'])
    else:
        try:
            cantidad_personas = int(data.get('cantidad_personas', 1))
            if cantidad_personas < 1:
                raise ValueError
            residente = Residente.objects.get(usuario=request.user, activo=True)
            if int(vivienda_id) != residente.vivienda_id:
                return Response({'error': 'Solo puede registrar visitas para su propia vivienda'}, status=403)
            visita = Visita(
                nombre_visitante=nombre,
                documento_visitante=documento,
                vivienda_destino=residente.vivienda,
                residente_autoriza=residente,
                motivo=data.get('motivo', ''),
                registrado_por=request.user,
                cantidad_personas=cantidad_personas,
                estado=Visita.CONFIRMADA,
                fecha_hora_entrada=timezone.now(),
                idempotency_key=idempotency_key,
            )
            visita.full_clean()
            visita.save()
        except (TypeError, ValueError):
            return Response({'error': 'cantidad_personas debe ser un entero mayor o igual a 1.'}, status=400)
        except Residente.DoesNotExist:
            return Response({'error': 'Residente no autorizado'}, status=403)
        except DjangoValidationError as exc:
            return Response({'error': str(exc)}, status=400)

    qr_payload = {
        'id': visita.pk,
        'nonce': visita.qr_nonce,
        'firma': generar_firma_qr(visita.pk, nonce=visita.qr_nonce),
    }
    qr = qrcode.make(json.dumps(qr_payload))
    buffer = io.BytesIO()
    qr.save(buffer, format='PNG')
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()
    return Response({
        'mensaje': 'Visita reservada correctamente' if es_reserva else 'Visita registrada correctamente',
        'id': visita.pk,
        'estado': visita.estado,
        'cantidad_personas': visita.cantidad_personas,
        'fecha_visita': visita.fecha_visita,
        'hora_inicio': visita.hora_inicio,
        'hora_fin': visita.hora_fin,
        'qr_base64': qr_base64,
        'qr_payload': qr_payload,
        'replayed': bool(es_reserva and result.get('replayed')),
    })

# vehiculo? crear visita para usuarios con vehiculo.
