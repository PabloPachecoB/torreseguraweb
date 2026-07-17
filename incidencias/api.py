import os

from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import EvidenciaIncidencia, Incidencia
from .serializers import IncidenciaListSerializer, IncidenciaSerializer

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
        incidencia = Incidencia.objects.select_related('residente__usuario').prefetch_related(
            'evidencias', 'eventos',
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

    incidencia = Incidencia.objects.create(
        residente=request.user.residente,
        categoria=categoria,
        titulo=titulo,
        descripcion=descripcion,
    )

    archivos = request.FILES.getlist('evidencias')
    for archivo in archivos:
        EvidenciaIncidencia.objects.create(
            incidencia=incidencia,
            archivo=archivo,
            tipo=_inferir_tipo_evidencia(archivo.name),
            subido_por=request.user,
        )

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
