"""Flujo de revisión y aprobación de incidencias y órdenes de trabajo."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from personal.models import Empleado
from usuarios.models import Gerente

from .models import (
    AprobacionIncidencia,
    EventoIncidencia,
    Incidencia,
    NotificacionIncidencia,
    OrdenTrabajo,
    RevisionIncidencia,
)

Usuario = get_user_model()

DEFAULT_ESTIMATED_HOURS = {
    Incidencia.URGENCIA_CRITICA: 2,
    Incidencia.URGENCIA_ALTA: 24,
    Incidencia.URGENCIA_MEDIA: 48,
    Incidencia.URGENCIA_BAJA: 120,
}
DEFAULT_COST_RANGES = {
    Incidencia.PLOMERIA: (100, 350),
    Incidencia.ELECTRICIDAD: (120, 400),
    Incidencia.ASCENSOR: (300, 1200),
    Incidencia.SEGURIDAD: (150, 500),
    Incidencia.LIMPIEZA: (50, 180),
    Incidencia.OTRO: (None, None),
}


def usuarios_administradores(incidencia):
    edificio = incidencia.residente.vivienda.edificio
    ids = set(
        Usuario.objects.filter(rol__nombre='Administrador', is_active=True)
        .values_list('id', flat=True)
    )
    ids.update(
        Gerente.objects.filter(usuario__is_active=True)
        .filter(
            models_q_gerente_edificio(edificio)
        )
        .values_list('usuario_id', flat=True)
    )
    return Usuario.objects.filter(pk__in=ids)


def models_q_gerente_edificio(edificio):
    from django.db.models import Q

    query = Q(edificio=edificio)
    if edificio.condominio_id:
        query |= Q(condominio_id=edificio.condominio_id)
    return query


def notificar(incidencia, destinatarios, tipo, mensaje):
    destinatario_ids = {
        user.pk for user in destinatarios if user and user.pk
    }
    NotificacionIncidencia.objects.bulk_create([
        NotificacionIncidencia(
            incidencia=incidencia,
            destinatario_id=user_id,
            tipo=tipo,
            mensaje=mensaje,
        )
        for user_id in destinatario_ids
    ])


def partes_de(incidencia):
    parties = [incidencia.residente.usuario, *usuarios_administradores(incidencia)]
    if incidencia.empleado_asignado_id:
        parties.append(incidencia.empleado_asignado.usuario)
    return parties


def revision_vigente(incidencia):
    return incidencia.revisiones.filter(vigente=True).order_by('-version').first()


@transaction.atomic
def crear_evaluacion_inicial(incidencia, usuario):
    current = revision_vigente(incidencia)
    if current:
        return current
    estimate = incidencia.estimacion_preliminar or {}
    default_cost_min, default_cost_max = DEFAULT_COST_RANGES[incidencia.categoria]
    revision = RevisionIncidencia.objects.create(
        incidencia=incidencia,
        version=1,
        categoria=incidencia.categoria,
        prioridad=incidencia.urgencia,
        costo_estimado_min=estimate.get('estimated_cost_min', default_cost_min),
        costo_estimado_max=estimate.get('estimated_cost_max', default_cost_max),
        moneda=estimate.get('currency', 'BOB'),
        tiempo_estimado_horas=estimate.get(
            'estimated_hours', DEFAULT_ESTIMATED_HOURS[incidencia.urgencia],
        ),
        comentario=estimate.get('disclaimer', ''),
        origen=RevisionIncidencia.AGENTE,
        creada_por=usuario,
    )
    AprobacionIncidencia.objects.create(
        revision=revision,
        rol=AprobacionIncidencia.RESIDENTE,
        decision=AprobacionIncidencia.APROBADA,
        usuario=incidencia.residente.usuario,
        comentario='El residente confirmó la evaluación inicial.',
    )
    incidencia.cambiar_estado(
        usuario=usuario,
        nuevo_estado=Incidencia.EN_REVISION,
        comentario='Evaluación inicial confirmada; pendiente de revisión.',
    )
    reviewers = list(usuarios_administradores(incidencia))
    if incidencia.empleado_asignado_id:
        reviewers.append(incidencia.empleado_asignado.usuario)
    notificar(
        incidencia,
        reviewers,
        'REVISION_REQUERIDA',
        f'La incidencia #{incidencia.pk} requiere revisión y aprobación.',
    )
    return revision


def rol_aprobador(incidencia, usuario):
    if incidencia.residente.usuario_id == usuario.pk:
        return AprobacionIncidencia.RESIDENTE
    if incidencia.empleado_asignado_id and incidencia.empleado_asignado.usuario_id == usuario.pk:
        return AprobacionIncidencia.TECNICO
    if usuario.is_superuser or usuarios_administradores(incidencia).filter(pk=usuario.pk).exists():
        return AprobacionIncidencia.ADMINISTRADOR
    raise PermissionError('No formas parte del flujo de aprobación de esta incidencia.')


@transaction.atomic
def revisar_incidencia(incidencia, usuario, cambios):
    actor_role = rol_aprobador(incidencia, usuario)
    if actor_role not in {
        AprobacionIncidencia.ADMINISTRADOR,
        AprobacionIncidencia.TECNICO,
    }:
        raise PermissionError('Solo el administrador o técnico puede ajustar la evaluación.')
    if actor_role == AprobacionIncidencia.TECNICO and not incidencia.empleado_asignado_id:
        raise PermissionError('La incidencia no tiene técnico asignado.')
    if hasattr(incidencia, 'orden_trabajo'):
        raise ValueError('La incidencia ya tiene una orden de trabajo aprobada.')

    current = revision_vigente(incidencia)
    if current is None:
        raise ValueError('La incidencia no tiene una evaluación inicial.')

    employee_id = cambios.pop('empleado_id', None)
    if employee_id is not None:
        if actor_role != AprobacionIncidencia.ADMINISTRADOR:
            raise PermissionError('Solo el administrador puede asignar al técnico.')
        employee = Empleado.objects.filter(
            pk=employee_id,
            activo=True,
            edificio_id=incidencia.residente.vivienda.edificio_id,
        ).select_related('usuario').first()
        if employee is None:
            raise ValueError('El empleado no está activo en el edificio de la incidencia.')
        incidencia.empleado_asignado = employee
        incidencia.save(update_fields=['empleado_asignado', 'fecha_actualizacion'])

    current.vigente = False
    current.save(update_fields=['vigente'])
    cost_min = cambios.get('costo_estimado_min', current.costo_estimado_min)
    cost_max = cambios.get('costo_estimado_max', current.costo_estimado_max)
    if cost_min is not None and cost_max is not None and cost_max < cost_min:
        raise ValueError('El costo máximo debe ser mayor o igual al mínimo.')
    revision = RevisionIncidencia.objects.create(
        incidencia=incidencia,
        version=current.version + 1,
        categoria=cambios.get('categoria', current.categoria),
        prioridad=cambios.get('prioridad', current.prioridad),
        costo_estimado_min=cost_min,
        costo_estimado_max=cost_max,
        moneda=cambios.get('moneda', current.moneda),
        tiempo_estimado_horas=cambios.get(
            'tiempo_estimado_horas', current.tiempo_estimado_horas,
        ),
        comentario=cambios.get('comentario', ''),
        origen=(
            RevisionIncidencia.ADMINISTRADOR
            if actor_role == AprobacionIncidencia.ADMINISTRADOR
            else RevisionIncidencia.TECNICO
        ),
        creada_por=usuario,
    )
    EventoIncidencia.objects.create(
        incidencia=incidencia,
        tipo_evento=EventoIncidencia.COMENTARIO,
        estado_nuevo=incidencia.estado,
        comentario=f'Evaluación actualizada a versión {revision.version}.',
        usuario=usuario,
    )
    notificar(
        incidencia,
        partes_de(incidencia),
        'EVALUACION_ACTUALIZADA',
        f'La evaluación de la incidencia #{incidencia.pk} fue actualizada. Debe aprobarse nuevamente.',
    )
    return revision


@transaction.atomic
def decidir_revision(incidencia, usuario, aprobar, comentario=''):
    revision = revision_vigente(incidencia)
    if revision is None:
        raise ValueError('La incidencia no tiene una evaluación vigente.')
    role = rol_aprobador(incidencia, usuario)
    decision = (
        AprobacionIncidencia.APROBADA
        if aprobar else AprobacionIncidencia.REVISION_SOLICITADA
    )
    approval, _ = AprobacionIncidencia.objects.update_or_create(
        revision=revision,
        rol=role,
        defaults={
            'decision': decision,
            'usuario': usuario,
            'comentario': comentario,
        },
    )
    if not aprobar:
        notificar(
            incidencia,
            partes_de(incidencia),
            'REVISION_SOLICITADA',
            f'{approval.get_rol_display()} solicitó revisar la incidencia #{incidencia.pk}.',
        )
        return approval, None

    order = finalizar_si_corresponde(incidencia, revision, usuario)
    return approval, order


def roles_requeridos(incidencia):
    roles = {
        AprobacionIncidencia.RESIDENTE,
        AprobacionIncidencia.ADMINISTRADOR,
    }
    if incidencia.empleado_asignado_id:
        roles.add(AprobacionIncidencia.TECNICO)
    return roles


def finalizar_si_corresponde(incidencia, revision, usuario):
    approved = set(
        revision.aprobaciones.filter(
            decision=AprobacionIncidencia.APROBADA,
        ).values_list('rol', flat=True)
    )
    if not roles_requeridos(incidencia).issubset(approved):
        return None
    order, _ = OrdenTrabajo.objects.get_or_create(
        incidencia=incidencia,
        defaults={
            'revision_aprobada': revision,
            'codigo': f'OT-{timezone.localdate():%Y}-{incidencia.pk:05d}',
            'tecnico': incidencia.empleado_asignado,
        },
    )
    if incidencia.estado != Incidencia.APROBADA:
        incidencia.cambiar_estado(
            usuario=usuario,
            nuevo_estado=Incidencia.APROBADA,
            comentario=f'Orden de trabajo {order.codigo} aprobada por todas las partes.',
        )
    notificar(
        incidencia,
        [incidencia.residente.usuario],
        'ORDEN_APROBADA',
        f'La orden de trabajo {order.codigo} fue aprobada.',
    )
    return order
