"""Servicios de dominio reutilizables para puertas y autorizaciones de visita."""

from datetime import date, time
from typing import Any, Dict, Optional

import requests
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from viviendas.models import Residente

from .models import AperturaPuerta, Puerta, Visita
from .qr_firma_utils import generar_firma_qr


WEBHOOK_TIMEOUT = 5
OPENING_PENDING = 'pending'


def user_role(user) -> Optional[str]:
    return getattr(getattr(user, 'rol', None), 'nombre', None)


def allowed_doors(user):
    """Puertas activas que el usuario autenticado puede abrir."""
    qs = Puerta.objects.filter(activa=True).select_related(
        'edificio', 'vivienda', 'vivienda__edificio'
    )
    role = user_role(user)
    if user.is_superuser or role == 'Administrador':
        return qs
    if role == 'Vigilante':
        filters = Q(tipo=Puerta.TIPO_PRINCIPAL)
        guard = getattr(user, 'vigilante', None)
        if guard and guard.edificio_id:
            filters |= Q(tipo=Puerta.TIPO_EDIFICIO, edificio_id=guard.edificio_id)
        return qs.filter(filters)
    if role == 'Residente':
        try:
            resident = Residente.objects.select_related('vivienda__edificio').get(
                usuario=user,
                activo=True,
            )
        except Residente.DoesNotExist:
            return qs.none()
        if not resident.vivienda_id:
            return qs.filter(tipo=Puerta.TIPO_PRINCIPAL)
        return qs.filter(
            Q(tipo=Puerta.TIPO_PRINCIPAL)
            | Q(tipo=Puerta.TIPO_EDIFICIO, edificio_id=resident.vivienda.edificio_id)
            | Q(tipo=Puerta.TIPO_VIVIENDA, vivienda_id=resident.vivienda_id)
        )
    return qs.none()


def serialize_door(door: Puerta) -> Dict[str, Any]:
    return {
        'id': door.pk,
        'name': door.nombre,
        'type': door.tipo,
        'type_display': door.get_tipo_display(),
        'building': str(door.edificio) if door.edificio else None,
        'apartment': str(door.vivienda) if door.vivienda else None,
        'has_hardware': bool(door.webhook_url),
        'demo_enabled': door.habilitada_para_demo,
    }


def open_door(
    user,
    door_id: int,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Abre una puerta autorizada una sola vez por clave de idempotencia."""
    try:
        door = allowed_doors(user).get(pk=door_id)
    except Puerta.DoesNotExist:
        return _error('unauthorized', 'door_not_allowed', 'Puerta inexistente o sin permiso.')
    if not door.habilitada_para_demo:
        return _error(
            'unauthorized',
            'demo_disabled',
            'La puerta no está habilitada para apertura remota.',
        )

    if idempotency_key:
        opening, created = AperturaPuerta.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                'puerta': door,
                'usuario': user,
                'exito': False,
                'detalle': 'Orden reservada; resultado pendiente.',
                'hardware_status': OPENING_PENDING,
            },
        )
        if not created:
            if opening.usuario_id != user.pk or opening.puerta_id != door.pk:
                return _error(
                    'conflict',
                    'idempotency_key_conflict',
                    'La clave de idempotencia pertenece a otra operación.',
                )
            return _opening_result(opening, replayed=True)
    else:
        opening = AperturaPuerta.objects.create(
            puerta=door,
            usuario=user,
            exito=False,
            detalle='Orden reservada; resultado pendiente.',
            hardware_status=OPENING_PENDING,
        )

    success = True
    hardware_status = 'software_confirmed'
    error_code = ''
    detail = 'Apertura confirmada en modo software sin hardware conectado.'
    if door.webhook_url:
        headers = {}
        token = getattr(settings, 'PUERTA_WEBHOOK_TOKEN', '')
        if token:
            headers['X-Auth-Token'] = token
        try:
            response = requests.post(
                door.webhook_url,
                json={'accion': 'abrir', 'puerta_id': door.pk, 'usuario': user.username},
                headers=headers,
                timeout=WEBHOOK_TIMEOUT,
            )
            success = response.ok
            hardware_status = 'confirmed' if success else 'rejected'
            error_code = '' if success else 'hardware_rejected'
            detail = f'Hardware respondió HTTP {response.status_code}.'
        except requests.Timeout:
            success = False
            hardware_status = 'timeout'
            error_code = 'hardware_timeout'
            detail = 'Timeout: el hardware no respondió a tiempo.'
        except requests.RequestException as exc:
            success = False
            hardware_status = 'unavailable'
            error_code = 'hardware_unavailable'
            detail = f'No se pudo contactar el hardware: {type(exc).__name__}.'

    AperturaPuerta.objects.filter(pk=opening.pk).update(
        exito=success,
        detalle=detail[:200],
        hardware_status=hardware_status,
        error_code=error_code,
    )
    opening.refresh_from_db()
    return _opening_result(opening, replayed=False)


def verify_opening(user, opening_id: int, idempotency_key: str) -> Dict[str, Any]:
    opening = AperturaPuerta.objects.filter(
        pk=opening_id,
        usuario=user,
        idempotency_key=idempotency_key,
    ).first()
    if opening is None:
        return _error('not_found', 'opening_not_verified', 'No se encontró la apertura.')
    return _opening_result(opening, replayed=True)


def create_visit_authorization(
    user,
    parameters: Dict[str, Any],
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Crea una autorización de visita ligada al residente autenticado."""
    try:
        resident = user.residente
    except ObjectDoesNotExist:
        return _error('unauthorized', 'resident_context_required', 'No existe residente asociado.')
    if not resident.activo or not resident.vivienda_id:
        return _error(
            'unauthorized',
            'resident_context_required',
            'El residente no está activo o no tiene vivienda.',
        )
    requested_apartment = parameters.get('apartment_id')
    if requested_apartment and int(requested_apartment) != resident.vivienda_id:
        return _error('unauthorized', 'apartment_not_allowed', 'La vivienda no pertenece al residente.')

    try:
        visit_date = _parse_date(parameters.get('date'))
        start_time = _parse_time(parameters.get('start_time'))
        end_time = _parse_time(parameters.get('end_time'))
        people = int(parameters.get('attendees', 1))
    except (TypeError, ValueError):
        return _error('validation_error', 'invalid_visit_parameters', 'Fecha, horas o cantidad inválidas.')
    if not visit_date or not start_time or not end_time:
        return _error('validation_error', 'schedule_required', 'Fecha y horario son obligatorios.')
    if visit_date < timezone.localdate():
        return _error('validation_error', 'past_date', 'La fecha no puede estar en el pasado.')
    if end_time <= start_time or people < 1:
        return _error('validation_error', 'invalid_visit_parameters', 'Horario o cantidad inválidos.')

    if idempotency_key:
        existing = Visita.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.residente_autoriza_id != resident.pk:
                return _error(
                    'conflict',
                    'idempotency_key_conflict',
                    'La clave de idempotencia pertenece a otra autorización.',
                )
            return _visit_result(existing, replayed=True)

    visit = Visita(
        nombre_visitante=str(parameters.get('name', '')).strip(),
        documento_visitante=str(parameters.get('document', '')).strip(),
        vivienda_destino=resident.vivienda,
        residente_autoriza=resident,
        motivo=str(parameters.get('reason', '')).strip(),
        registrado_por=user,
        cantidad_personas=people,
        fecha_visita=visit_date,
        hora_inicio=start_time,
        hora_fin=end_time,
        estado=Visita.RESERVADA,
        fecha_hora_entrada=None,
        idempotency_key=idempotency_key,
    )
    try:
        visit.full_clean()
        with transaction.atomic():
            visit.save()
    except ValidationError as exc:
        return _error('validation_error', 'invalid_visit_parameters', _validation_message(exc))
    except IntegrityError:
        existing = Visita.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.residente_autoriza_id == resident.pk:
            return _visit_result(existing, replayed=True)
        return _error('conflict', 'idempotency_key_conflict', 'No se pudo reservar la clave.')
    return _visit_result(visit, replayed=False)


def verify_visit(user, visit_id: int, idempotency_key: str) -> Dict[str, Any]:
    visit = Visita.objects.filter(
        pk=visit_id,
        residente_autoriza__usuario=user,
        idempotency_key=idempotency_key,
    ).first()
    if visit is None:
        return _error('not_found', 'visit_not_verified', 'No se encontró la autorización.')
    return _visit_result(visit, replayed=True)


def report_visit_arrival(user, visit_id: int, photo=None) -> Dict[str, Any]:
    """Registra localmente una llegada y la deja pendiente del residente."""
    role = user_role(user)
    visit = Visita.objects.select_related('vivienda_destino').filter(pk=visit_id).first()
    if visit is None:
        return _error('not_found', 'visit_not_found', 'La visita no existe.')
    if not (user.is_superuser or role == 'Administrador'):
        guard = getattr(user, 'vigilante', None)
        if role != 'Vigilante' or not guard or guard.edificio_id != visit.vivienda_destino.edificio_id:
            return _error('unauthorized', 'guard_not_allowed', 'No puedes reportar esta llegada.')
    if photo:
        if getattr(photo, 'size', 0) > 5 * 1024 * 1024:
            return _error('validation_error', 'photo_too_large', 'La foto supera 5 MB.')
        content_type = getattr(photo, 'content_type', '')
        if content_type and not content_type.startswith('image/'):
            return _error('validation_error', 'invalid_photo_type', 'El archivo no es una imagen.')

    updated = Visita.objects.filter(
        pk=visit.pk,
        estado=Visita.RESERVADA,
    ).update(
        estado=Visita.PENDIENTE_APROBACION,
        llegada_reportada_en=timezone.now(),
        notificacion_estado='REGISTRADA_LOCAL',
    )
    visit.refresh_from_db()
    if not updated:
        return _error(
            'conflict',
            'invalid_visit_state',
            f'La visita no está reservada (estado: {visit.estado}).',
        )
    if photo:
        visit.foto_visitante = photo
        visit.save(update_fields=['foto_visitante'])
    result = _visit_result(visit, replayed=False)
    result['notification_delivery'] = 'local_polling'
    return result


def decide_visit_arrival(user, visit_id: int, approved: bool) -> Dict[str, Any]:
    """El residente dueño aprueba o rechaza una llegada reportada."""
    visit = Visita.objects.filter(
        pk=visit_id,
        residente_autoriza__usuario=user,
    ).first()
    if visit is None:
        return _error('unauthorized', 'visit_not_allowed', 'La visita no pertenece al residente.')
    new_status = Visita.CONFIRMADA if approved else Visita.RECHAZADA
    values = {
        'estado': new_status,
        'decision_residente_en': timezone.now(),
    }
    if approved:
        values['fecha_hora_entrada'] = timezone.now()
    updated = Visita.objects.filter(
        pk=visit.pk,
        estado=Visita.PENDIENTE_APROBACION,
    ).update(**values)
    visit.refresh_from_db()
    if not updated:
        return _error(
            'conflict',
            'invalid_visit_state',
            f'La visita no espera aprobación (estado: {visit.estado}).',
        )
    return _visit_result(visit, replayed=False)


def _opening_result(opening: AperturaPuerta, replayed: bool) -> Dict[str, Any]:
    pending = opening.hardware_status == OPENING_PENDING
    status = 'pending' if pending else 'success' if opening.exito else 'hardware_error'
    return {
        'status': status,
        'success': opening.exito,
        'abierta': opening.exito,
        'door_id': opening.puerta_id,
        'opening_id': opening.pk,
        'hardware_status': opening.hardware_status,
        'error_code': opening.error_code or None,
        'message': opening.detalle,
        'detalle': opening.detalle,
        'replayed': replayed,
    }


def _visit_result(visit: Visita, replayed: bool) -> Dict[str, Any]:
    return {
        'status': 'success',
        'success': True,
        'visit_id': visit.pk,
        'visit_status': visit.estado,
        'qr_created': bool(visit.qr_nonce),
        'notification_status': visit.notificacion_estado,
        'qr_payload': {
            'id': visit.pk,
            'nonce': visit.qr_nonce,
            'firma': generar_firma_qr(visit.pk, nonce=visit.qr_nonce),
        },
        'replayed': replayed,
    }


def _parse_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)) if value else None


def _parse_time(value):
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value)) if value else None


def _validation_message(exc: ValidationError) -> str:
    if hasattr(exc, 'message_dict'):
        return '; '.join(
            str(message)
            for messages in exc.message_dict.values()
            for message in messages
        )
    return '; '.join(exc.messages)


def _error(status: str, error_code: str, message: str) -> Dict[str, Any]:
    return {'status': status, 'success': False, 'error_code': error_code, 'message': message}
