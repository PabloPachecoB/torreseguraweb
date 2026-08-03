import os

from django.db import IntegrityError, transaction
from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import EvidenciaIncidencia, Incidencia, NotificacionIncidencia
from .serializers import (
    DecisionRevisionSerializer,
    IncidenciaListSerializer,
    IncidenciaSerializer,
    NotificacionIncidenciaSerializer,
    RevisionUpdateSerializer,
)
from .services import (
    crear_evaluacion_inicial,
    decidir_revision,
    revisar_incidencia,
    usuarios_administradores,
)

EXTENSIONES_FOTO = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic'}
EXTENSIONES_VIDEO = {'.mp4', '.mov', '.avi', '.webm', '.mkv'}

ROLES_STAFF_MANTENIMIENTO = ['Administrador', 'Gerente', 'Personal']


def _rol(user):
    return getattr(getattr(user, 'rol', None), 'nombre', None)


def _es_staff_mantenimiento(user):
    return user.is_superuser or _rol(user) in ROLES_STAFF_MANTENIMIENTO


def _inferir_tipo_evidencia(nombre_archivo):
    ext = os.path.splitext(nombre_archivo)[1].lower()
    if ext in EXTENSIONES_FOTO:
        return EvidenciaIncidencia.FOTO
    if ext in EXTENSIONES_VIDEO:
        return EvidenciaIncidencia.VIDEO
    return EvidenciaIncidencia.DOCUMENTO


def _incidencia_visible_o_none(request, incidencia_id):
    """Devuelve la incidencia si el usuario puede verla (dueno o staff de
    mantenimiento), o None. Mismo patron 404-en-vez-de-403 que `agente` y
    `areas_comunes`, para no confirmar la existencia de incidencias ajenas.
    """
    try:
        incidencia = Incidencia.objects.select_related(
            'residente__usuario', 'residente__vivienda__edificio',
            'empleado_asignado__usuario',
        ).prefetch_related(
            'evidencias', 'eventos', 'revisiones__aprobaciones',
        ).get(pk=incidencia_id)
    except Incidencia.DoesNotExist:
        return None

    if _es_staff_mantenimiento(request.user):
        return incidencia
    if hasattr(request.user, 'residente') and incidencia.residente_id == request.user.residente.pk:
        return incidencia
    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def crear_incidencia(request):
    """HU-03.1 (INC-01/INC-02): crea una incidencia con evidencia adjunta.

    Requiere `multipart/form-data` (no JSON) porque incluye archivos.
    Campos: `categoria`, `titulo`, `descripcion`, y `evidencias` (uno o mas
    archivos, mismo nombre de campo repetido).
    """
    if not hasattr(request.user, 'residente'):
        return Response(
            {'mensaje': 'Solo los residentes pueden reportar incidencias.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    titulo = request.data.get('titulo')
    descripcion = request.data.get('descripcion')
    categoria = request.data.get('categoria', Incidencia.OTRO)
    ubicacion = request.data.get('ubicacion', '')
    urgencia = request.data.get('urgencia', Incidencia.URGENCIA_MEDIA)
    idempotency_key = request.headers.get('Idempotency-Key') or None

    if not titulo or not descripcion:
        faltantes = [c for c, v in [('titulo', titulo), ('descripcion', descripcion)] if not v]
        return Response(
            {'mensaje': 'Titulo y descripcion son obligatorios.', 'campos_faltantes': faltantes},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if categoria not in dict(Incidencia.CATEGORIAS):
        return Response(
            {'mensaje': f'Categoria invalida. Opciones: {", ".join(dict(Incidencia.CATEGORIAS))}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if urgencia not in dict(Incidencia.URGENCIAS):
        return Response(
            {'mensaje': f'Urgencia invalida. Opciones: {", ".join(dict(Incidencia.URGENCIAS))}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    existing = (
        Incidencia.objects.filter(idempotency_key=idempotency_key).first()
        if idempotency_key
        else None
    )
    if existing is not None:
        if existing.residente_id != request.user.residente.pk:
            return Response(
                {'mensaje': 'La clave de idempotencia ya está en uso.'},
                status=status.HTTP_409_CONFLICT,
            )
        same_parameters = all([
            existing.titulo == titulo,
            existing.descripcion == descripcion,
            existing.categoria == categoria,
            existing.ubicacion == ubicacion,
            existing.urgencia == urgencia,
        ])
        if not same_parameters:
            return Response(
                {'mensaje': 'La clave de idempotencia fue usada con otros parámetros.'},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = IncidenciaSerializer(existing, context={'request': request})
        return Response({
            'mensaje': 'Incidencia ya procesada anteriormente.',
            'incidencia': serializer.data,
            'replayed': True,
        })

    try:
        with transaction.atomic():
            incidencia = Incidencia.objects.create(
                residente=request.user.residente,
                categoria=categoria,
                titulo=titulo,
                descripcion=descripcion,
                ubicacion=ubicacion,
                urgencia=urgencia,
                idempotency_key=idempotency_key,
            )

            archivos = request.FILES.getlist('evidencias')
            for archivo in archivos:
                EvidenciaIncidencia.objects.create(
                    incidencia=incidencia,
                    archivo=archivo,
                    tipo=_inferir_tipo_evidencia(archivo.name),
                    subido_por=request.user,
                )
            crear_evaluacion_inicial(incidencia, request.user)
    except IntegrityError:
        existing = Incidencia.objects.filter(
            idempotency_key=idempotency_key,
            residente=request.user.residente,
        ).first()
        if existing is None:
            return Response(
                {'mensaje': 'No se pudo determinar el resultado de la incidencia.'},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = IncidenciaSerializer(existing, context={'request': request})
        return Response({
            'mensaje': 'Incidencia ya procesada anteriormente.',
            'incidencia': serializer.data,
            'replayed': True,
        })

    incidencia.refresh_from_db()
    serializer = IncidenciaSerializer(incidencia, context={'request': request})
    return Response(
        {'mensaje': 'Incidencia creada correctamente.', 'incidencia': serializer.data},
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mis_incidencias(request):
    """Lista las incidencias del residente autenticado (mas recientes primero)."""
    if not hasattr(request.user, 'residente'):
        return Response(
            {'mensaje': 'Solo los residentes tienen incidencias.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    incidencias = Incidencia.objects.filter(residente=request.user.residente)[:50]
    serializer = IncidenciaListSerializer(incidencias, many=True, context={'request': request})
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def detalle_incidencia(request, incidencia_id):
    """Detalle completo: evidencia + timeline de eventos (INC-06)."""
    incidencia = _incidencia_visible_o_none(request, incidencia_id)
    if incidencia is None:
        return Response({'mensaje': 'Incidencia no encontrada.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = IncidenciaSerializer(incidencia, context={'request': request})
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def descargar_evidencia(request, incidencia_id, evidencia_id):
    """Descarga protegida de un archivo de evidencia — nunca se sirve por la
    URL cruda de MEDIA_URL. Mismo chequeo de permisos que el detalle.
    """
    incidencia = _incidencia_visible_o_none(request, incidencia_id)
    if incidencia is None:
        return Response({'mensaje': 'Incidencia no encontrada.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        evidencia = incidencia.evidencias.get(pk=evidencia_id)
    except EvidenciaIncidencia.DoesNotExist:
        return Response({'mensaje': 'Evidencia no encontrada.'}, status=status.HTTP_404_NOT_FOUND)

    return FileResponse(
        evidencia.archivo.open('rb'),
        as_attachment=True,
        filename=os.path.basename(evidencia.archivo.name),
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def agregar_evidencia(request, incidencia_id):
    """Agrega evidencia mediante el backend después de crear la incidencia."""
    incidencia = _incidencia_visible_o_none(request, incidencia_id)
    if incidencia is None:
        return Response(
            {'mensaje': 'Incidencia no encontrada.'},
            status=status.HTTP_404_NOT_FOUND,
        )
    archivos = request.FILES.getlist('evidencias')
    if not archivos:
        return Response(
            {'mensaje': 'Debe adjuntar al menos un archivo.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    for archivo in archivos:
        EvidenciaIncidencia.objects.create(
            incidencia=incidencia,
            archivo=archivo,
            tipo=_inferir_tipo_evidencia(archivo.name),
            subido_por=request.user,
        )
    incidencia.refresh_from_db()
    return Response(
        IncidenciaSerializer(incidencia, context={'request': request}).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def cambiar_estado_incidencia(request, incidencia_id):
    """Cambia el estado de una incidencia (solo Administrador/Gerente/Personal).

    Alimenta el timeline (INC-06) via `Incidencia.cambiar_estado()`.
    """
    if not _es_staff_mantenimiento(request.user):
        return Response(
            {'mensaje': 'No tienes permisos para cambiar el estado de esta incidencia.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        incidencia = Incidencia.objects.get(pk=incidencia_id)
    except Incidencia.DoesNotExist:
        return Response({'mensaje': 'Incidencia no encontrada.'}, status=status.HTTP_404_NOT_FOUND)

    nuevo_estado = request.data.get('estado')
    if nuevo_estado not in dict(Incidencia.ESTADOS):
        return Response(
            {'mensaje': f'Estado invalido. Opciones: {", ".join(dict(Incidencia.ESTADOS))}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    incidencia.cambiar_estado(
        usuario=request.user,
        nuevo_estado=nuevo_estado,
        comentario=request.data.get('comentario', ''),
    )
    serializer = IncidenciaSerializer(incidencia, context={'request': request})
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pendientes_revision(request):
    queryset = Incidencia.objects.filter(estado=Incidencia.EN_REVISION).select_related(
        'residente__vivienda__edificio', 'empleado_asignado__usuario',
    ).prefetch_related('revisiones__aprobaciones')
    if hasattr(request.user, 'residente'):
        queryset = queryset.filter(residente=request.user.residente)
    elif hasattr(request.user, 'empleado'):
        queryset = queryset.filter(empleado_asignado=request.user.empleado)
    elif not request.user.is_superuser and _rol(request.user) not in {'Administrador', 'Gerente'}:
        return Response([], status=status.HTTP_200_OK)
    elif _rol(request.user) == 'Gerente':
        edificios = request.user.gerente.condominio.edificios.all() if request.user.gerente.condominio_id else [request.user.gerente.edificio]
        queryset = queryset.filter(residente__vivienda__edificio__in=edificios)
    return Response(IncidenciaListSerializer(queryset[:100], many=True).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ajustar_revision(request, incidencia_id):
    incidencia = _incidencia_visible_o_none(request, incidencia_id)
    if incidencia is None:
        return Response({'mensaje': 'Incidencia no encontrada.'}, status=status.HTTP_404_NOT_FOUND)
    serializer = RevisionUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        revisar_incidencia(incidencia, request.user, dict(serializer.validated_data))
    except PermissionError as exc:
        return Response({'mensaje': str(exc)}, status=status.HTTP_403_FORBIDDEN)
    except ValueError as exc:
        return Response({'mensaje': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    incidencia.refresh_from_db()
    return Response(IncidenciaSerializer(incidencia, context={'request': request}).data)


def _decidir(request, incidencia_id, aprobar):
    incidencia = _incidencia_visible_o_none(request, incidencia_id)
    if incidencia is None:
        return Response({'mensaje': 'Incidencia no encontrada.'}, status=status.HTTP_404_NOT_FOUND)
    serializer = DecisionRevisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        _, order = decidir_revision(
            incidencia,
            request.user,
            aprobar=aprobar,
            comentario=serializer.validated_data.get('comentario', ''),
        )
    except PermissionError as exc:
        return Response({'mensaje': str(exc)}, status=status.HTTP_403_FORBIDDEN)
    except ValueError as exc:
        return Response({'mensaje': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    incidencia.refresh_from_db()
    payload = IncidenciaSerializer(incidencia, context={'request': request}).data
    return Response({'incidencia': payload, 'orden_generada': bool(order)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def aprobar_revision(request, incidencia_id):
    return _decidir(request, incidencia_id, aprobar=True)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def solicitar_revision(request, incidencia_id):
    return _decidir(request, incidencia_id, aprobar=False)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def notificaciones_incidencia(request):
    notifications = NotificacionIncidencia.objects.filter(
        destinatario=request.user,
    ).select_related('incidencia')[:100]
    return Response(NotificacionIncidenciaSerializer(notifications, many=True).data)
