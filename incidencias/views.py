from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from personal.models import Empleado
from viviendas.services import edificios_administrados

from .forms import RevisionIncidenciaForm, SolicitarRevisionForm
from .models import AprobacionIncidencia, EvidenciaIncidencia, Incidencia
from .services import (
    decidir_revision,
    revisar_incidencia,
    revision_vigente,
    roles_requeridos,
)


def _rol_nombre(user):
    return getattr(getattr(user, 'rol', None), 'nombre', None)


def _es_administrador(user):
    return user.is_superuser or _rol_nombre(user) in {'Administrador', 'Gerente'}


def _incidencias_permitidas(user):
    queryset = Incidencia.objects.select_related(
        'residente__usuario',
        'residente__vivienda__edificio',
        'empleado_asignado__usuario',
        'empleado_asignado__puesto',
    )
    if _es_administrador(user):
        return queryset.filter(
            residente__vivienda__edificio__in=edificios_administrados(user),
        )
    empleado = getattr(user, 'empleado', None)
    if empleado is not None:
        return queryset.filter(empleado_asignado=empleado)
    raise PermissionDenied('No tienes acceso al panel de revisión de incidencias.')


def _incidencia_para_usuario(user, incidencia_id):
    return get_object_or_404(_incidencias_permitidas(user), pk=incidencia_id)


@login_required
def revision_list(request):
    incidencias = _incidencias_permitidas(request.user)
    totales = {
        'pendientes': incidencias.filter(estado=Incidencia.EN_REVISION).count(),
        'aprobadas': incidencias.filter(estado=Incidencia.APROBADA).count(),
        'activas': incidencias.exclude(
            estado__in={Incidencia.RESUELTA, Incidencia.CANCELADA},
        ).count(),
    }

    estado = request.GET.get('estado', '')
    query = request.GET.get('q', '').strip()
    if estado in dict(Incidencia.ESTADOS):
        incidencias = incidencias.filter(estado=estado)
    if query:
        incidencias = incidencias.filter(
            Q(titulo__icontains=query)
            | Q(descripcion__icontains=query)
            | Q(residente__usuario__first_name__icontains=query)
            | Q(residente__usuario__last_name__icontains=query)
            | Q(residente__vivienda__numero__icontains=query)
        )

    return render(request, 'incidencias/revision_list.html', {
        'incidencias': incidencias,
        'estados': Incidencia.ESTADOS,
        'estado_seleccionado': estado,
        'query': query,
        'totales': totales,
    })


@login_required
def revision_detail(request, incidencia_id):
    incidencia = _incidencia_para_usuario(request.user, incidencia_id)
    revision = revision_vigente(incidencia)
    if revision is None:
        raise PermissionDenied('La incidencia todavía no tiene una evaluación inicial.')

    aprobaciones = {item.rol: item for item in revision.aprobaciones.select_related('usuario')}
    approval_rows = []
    role_labels = dict(AprobacionIncidencia.ROLES)
    for role in (
        AprobacionIncidencia.RESIDENTE,
        AprobacionIncidencia.ADMINISTRADOR,
        AprobacionIncidencia.TECNICO,
    ):
        required = role in roles_requeridos(incidencia)
        approval_rows.append({
            'rol': role,
            'nombre': role_labels[role],
            'requerido': required,
            'aprobacion': aprobaciones.get(role),
        })

    puede_asignar = _es_administrador(request.user)
    form = RevisionIncidenciaForm(
        incidencia=incidencia,
        revision=revision,
        puede_asignar=puede_asignar,
    )
    return render(request, 'incidencias/revision_detail.html', {
        'incidencia': incidencia,
        'revision': revision,
        'approval_rows': approval_rows,
        'form': form,
        'solicitar_form': SolicitarRevisionForm(),
        'puede_asignar': puede_asignar,
        'orden': getattr(incidencia, 'orden_trabajo', None),
    })


@login_required
@require_POST
def ajustar_revision(request, incidencia_id):
    incidencia = _incidencia_para_usuario(request.user, incidencia_id)
    revision = revision_vigente(incidencia)
    if revision is None:
        messages.error(request, 'La incidencia no tiene una evaluación para ajustar.')
        return redirect('incidencia-revision-detail', incidencia_id=incidencia.pk)

    puede_asignar = _es_administrador(request.user)
    form = RevisionIncidenciaForm(
        request.POST,
        incidencia=incidencia,
        revision=revision,
        puede_asignar=puede_asignar,
    )
    if not form.is_valid():
        messages.error(
            request,
            'No se guardaron los cambios. Revisa los valores del formulario.',
        )
        aprobaciones = {item.rol: item for item in revision.aprobaciones.select_related('usuario')}
        role_labels = dict(AprobacionIncidencia.ROLES)
        rows = [{
            'rol': role,
            'nombre': role_labels[role],
            'requerido': role in roles_requeridos(incidencia),
            'aprobacion': aprobaciones.get(role),
        } for role in (
            AprobacionIncidencia.RESIDENTE,
            AprobacionIncidencia.ADMINISTRADOR,
            AprobacionIncidencia.TECNICO,
        )]
        return render(request, 'incidencias/revision_detail.html', {
            'incidencia': incidencia,
            'revision': revision,
            'approval_rows': rows,
            'form': form,
            'solicitar_form': SolicitarRevisionForm(),
            'puede_asignar': puede_asignar,
            'orden': getattr(incidencia, 'orden_trabajo', None),
        }, status=400)

    cambios = {
        key: value for key, value in form.cleaned_data.items()
        if key != 'empleado'
    }
    empleado = form.cleaned_data.get('empleado') if puede_asignar else None
    if empleado is not None:
        cambios['empleado_id'] = empleado.pk
    try:
        nueva_revision = revisar_incidencia(incidencia, request.user, cambios)
    except (PermissionError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'Evaluación actualizada a la versión {nueva_revision.version}. '
            'Las partes deben aprobar nuevamente.',
        )
    return redirect('incidencia-revision-detail', incidencia_id=incidencia.pk)


@login_required
@require_POST
def aprobar_revision(request, incidencia_id):
    incidencia = _incidencia_para_usuario(request.user, incidencia_id)
    try:
        _, orden = decidir_revision(incidencia, request.user, aprobar=True)
    except (PermissionError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        if orden:
            messages.success(request, f'Orden de trabajo {orden.codigo} aprobada y creada.')
        else:
            messages.success(request, 'Aprobación registrada. Aún faltan otras partes por aprobar.')
    return redirect('incidencia-revision-detail', incidencia_id=incidencia.pk)


@login_required
@require_POST
def solicitar_revision(request, incidencia_id):
    incidencia = _incidencia_para_usuario(request.user, incidencia_id)
    form = SolicitarRevisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Indica qué aspecto de la evaluación debe revisarse.')
    else:
        try:
            decidir_revision(
                incidencia,
                request.user,
                aprobar=False,
                comentario=form.cleaned_data['comentario'],
            )
        except (PermissionError, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            messages.warning(request, 'Solicitud de revisión enviada a las demás partes.')
    return redirect('incidencia-revision-detail', incidencia_id=incidencia.pk)


@login_required
def descargar_evidencia(request, incidencia_id, evidencia_id):
    incidencia = _incidencia_para_usuario(request.user, incidencia_id)
    evidencia = get_object_or_404(
        EvidenciaIncidencia, pk=evidencia_id, incidencia=incidencia,
    )
    return FileResponse(
        evidencia.archivo.open('rb'),
        as_attachment=True,
        filename=evidencia.archivo.name.rsplit('/', 1)[-1],
    )
