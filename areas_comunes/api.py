from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AreaComun, Reserva
from .serializers import AreaComunSerializer, ReservaSerializer
from .services import available_slots

DURACION_MINUTOS_DEFAULT = 60
DIAS_BUSQUEDA_ALTERNATIVAS = 7


def _area_del_usuario_o_none(request, area_id):
    """Busca el area activa validando que pertenezca al edificio del usuario
    (salvo Administrador, que puede consultar cualquiera). Devuelve None si no
    existe o no le corresponde al usuario.
    """
    try:
        area = AreaComun.objects.select_related("edificio").get(pk=area_id, activo=True)
    except AreaComun.DoesNotExist:
        return None

    rol = getattr(getattr(request.user, "rol", None), "nombre", None)
    if request.user.is_superuser or rol == "Administrador":
        return area

    edificio = _edificio_del_usuario(request.user)
    if edificio and area.edificio_id == edificio.id:
        return area
    return None


def _slots_disponibles(area, fecha, duracion_minutos):
    """Alias compatible; la lógica compartida vive en servicios de dominio."""
    return available_slots(area, fecha, duracion_minutos)


def _edificio_del_usuario(user):
    """Devuelve el edificio asociado al usuario según su rol, o None."""
    if hasattr(user, "residente") and user.residente and user.residente.vivienda:
        return user.residente.vivienda.edificio
    if hasattr(user, "vigilante") and user.vigilante:
        return user.vigilante.edificio
    if hasattr(user, "gerente") and user.gerente:
        return user.gerente.edificio
    if hasattr(user, "empleado") and user.empleado:
        return user.empleado.edificio
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_areas(request):
    """Lista las areas comunes activas del edificio del usuario.

    Administradores ven todas; el resto solo las de su edificio.
    """
    areas = AreaComun.objects.filter(activo=True).select_related("edificio")

    rol = getattr(getattr(request.user, "rol", None), "nombre", None)
    if not (request.user.is_superuser or rol == "Administrador"):
        edificio = _edificio_del_usuario(request.user)
        if edificio:
            areas = areas.filter(edificio=edificio)
        else:
            areas = areas.none()

    serializer = AreaComunSerializer(areas, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def crear_reserva(request, area_id):
    """Crea una reserva para un area comun. Solo residentes."""
    if not hasattr(request.user, "residente"):
        return Response(
            {"mensaje": "Solo los residentes pueden hacer reservas."},
            status=status.HTTP_403_FORBIDDEN,
        )

    area = _area_del_usuario_o_none(request, area_id)
    if area is None:
        return Response(
            {"mensaje": "Area comun no encontrada."},
            status=status.HTTP_404_NOT_FOUND,
        )

    fecha = request.data.get("fecha")
    hora_inicio = request.data.get("hora_inicio")
    hora_fin = request.data.get("hora_fin")
    cantidad_personas = request.data.get("cantidad_personas", 1)
    motivo = request.data.get("motivo", "")
    idempotency_key = request.headers.get("Idempotency-Key") or None

    if not fecha or not hora_inicio or not hora_fin:
        return Response(
            {"mensaje": "Fecha, hora de inicio y hora de fin son obligatorios."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    existing = (
        Reserva.objects.filter(idempotency_key=idempotency_key).first()
        if idempotency_key
        else None
    )
    if existing is not None:
        if existing.residente_id != request.user.residente.pk:
            return Response(
                {"mensaje": "La clave de idempotencia ya está en uso."},
                status=status.HTTP_409_CONFLICT,
            )
        same_parameters = all(
            [
                existing.area_comun_id == area.pk,
                existing.fecha.isoformat() == str(fecha),
                existing.hora_inicio.strftime("%H:%M") == str(hora_inicio)[:5],
                existing.hora_fin.strftime("%H:%M") == str(hora_fin)[:5],
                str(existing.cantidad_personas) == str(cantidad_personas),
                existing.motivo == motivo,
            ]
        )
        if not same_parameters:
            return Response(
                {
                    "mensaje": (
                        "La clave de idempotencia fue usada con otros parámetros."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ReservaSerializer(existing, context={"request": request})
        return Response(
            {
                "mensaje": "Reserva ya procesada anteriormente.",
                "reserva": serializer.data,
                "replayed": True,
            }
        )

    try:
        with transaction.atomic():
            locked_area = AreaComun.objects.select_for_update().get(pk=area.pk)
            reserva = Reserva(
                area_comun=locked_area,
                residente=request.user.residente,
                fecha=fecha,
                hora_inicio=hora_inicio,
                hora_fin=hora_fin,
                cantidad_personas=cantidad_personas,
                motivo=motivo,
                idempotency_key=idempotency_key,
            )
            reserva.save()
    except ValidationError as e:
        msg = e.message if hasattr(e, "message") else str(e)
        if hasattr(e, "message_dict"):
            msg = " ".join(
                f"{v[0]}" if isinstance(v, list) else str(v)
                for v in e.message_dict.values()
            )
        return Response(
            {"mensaje": msg},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except IntegrityError:
        existing = Reserva.objects.filter(
            idempotency_key=idempotency_key,
            residente=request.user.residente,
        ).first()
        if existing is None:
            return Response(
                {"mensaje": "No se pudo determinar el resultado de la reserva."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ReservaSerializer(existing, context={"request": request})
        return Response(
            {
                "mensaje": "Reserva ya procesada anteriormente.",
                "reserva": serializer.data,
                "replayed": True,
            }
        )

    serializer = ReservaSerializer(reserva, context={"request": request})
    return Response(
        {"mensaje": "Reserva creada correctamente.", "reserva": serializer.data},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_reservas_area(request, area_id):
    """Lista las reservas futuras de un area (para ver disponibilidad)."""
    hoy = date.today()
    reservas = (
        Reserva.objects.filter(
            area_comun_id=area_id,
            fecha__gte=hoy,
            estado__in=["pendiente", "confirmada"],
        )
        .select_related("residente__usuario", "area_comun")
        .order_by("fecha", "hora_inicio")
    )
    serializer = ReservaSerializer(reservas, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mis_reservas(request):
    """Lista las reservas del residente autenticado."""
    if not hasattr(request.user, "residente"):
        return Response(
            {"mensaje": "Solo los residentes tienen reservas."},
            status=status.HTTP_403_FORBIDDEN,
        )

    reservas = (
        Reserva.objects.filter(residente=request.user.residente)
        .select_related("area_comun", "residente__usuario")
        .order_by("-fecha", "-hora_inicio")[:50]
    )
    serializer = ReservaSerializer(reservas, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def cancelar_reserva(request, reserva_id):
    """Cancela una reserva. Solo el residente dueno o admin."""
    try:
        reserva = Reserva.objects.select_related(
            "residente__usuario", "area_comun"
        ).get(pk=reserva_id)
    except Reserva.DoesNotExist:
        return Response(
            {"mensaje": "Reserva no encontrada."},
            status=status.HTTP_404_NOT_FOUND,
        )

    rol = getattr(getattr(request.user, "rol", None), "nombre", None)
    es_admin = request.user.is_superuser or rol == "Administrador"
    es_dueno = (
        hasattr(request.user, "residente")
        and reserva.residente_id == request.user.residente.pk
    )

    if not es_admin and not es_dueno:
        return Response(
            {"mensaje": "No tienes permisos para cancelar esta reserva."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if reserva.estado == "cancelada":
        return Response(
            {"mensaje": "La reserva ya esta cancelada."},
            status=status.HTTP_409_CONFLICT,
        )

    if reserva.estado == "completada":
        return Response(
            {"mensaje": "No se puede cancelar una reserva completada."},
            status=status.HTTP_409_CONFLICT,
        )

    reserva.estado = "cancelada"
    reserva.save(update_fields=["estado", "updated_at"])

    serializer = ReservaSerializer(reserva, context={"request": request})
    return Response(
        {"mensaje": "Reserva cancelada correctamente.", "reserva": serializer.data}
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def consultar_disponibilidad(request, area_id):
    """HU-02.1: consulta huecos libres reales de un area comun para una fecha,
    respetando su horario de atencion y las reservas ya existentes (nunca
    inventa horarios). Si no hay lugar en la fecha pedida, busca alternativas
    en los proximos dias con la misma logica.

    Query params:
      - fecha (obligatorio): YYYY-MM-DD
      - duracion_minutos (opcional, default 60)
    """
    area = _area_del_usuario_o_none(request, area_id)
    if area is None:
        return Response(
            {"mensaje": "Area comun no encontrada."},
            status=status.HTTP_404_NOT_FOUND,
        )

    fecha_str = request.query_params.get("fecha")
    if not fecha_str:
        return Response(
            {"mensaje": "Fecha es obligatoria.", "campos_faltantes": ["fecha"]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        fecha = date.fromisoformat(fecha_str)
    except ValueError:
        return Response(
            {"mensaje": "Fecha invalida, use el formato YYYY-MM-DD."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if fecha < timezone.localdate():
        return Response(
            {"mensaje": "La fecha debe ser hoy o una fecha futura."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    duracion_str = request.query_params.get("duracion_minutos", DURACION_MINUTOS_DEFAULT)
    try:
        duracion_minutos = int(duracion_str)
        if duracion_minutos <= 0:
            raise ValueError
    except ValueError:
        return Response(
            {"mensaje": "duracion_minutos debe ser un entero positivo."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    slots = _slots_disponibles(area, fecha, duracion_minutos)

    alternativas = []
    if not slots:
        for i in range(1, DIAS_BUSQUEDA_ALTERNATIVAS + 1):
            fecha_alterna = fecha + timedelta(days=i)
            slots_alterna = _slots_disponibles(area, fecha_alterna, duracion_minutos)
            if slots_alterna:
                alternativas.append(
                    {"fecha": fecha_alterna.isoformat(), "slots_disponibles": slots_alterna}
                )
            if len(alternativas) >= 3:
                break

    return Response(
        {
            "area": AreaComunSerializer(area, context={"request": request}).data,
            "fecha_consultada": fecha.isoformat(),
            "duracion_minutos": duracion_minutos,
            "slots_disponibles": slots,
            "alternativas": alternativas,
        }
    )
