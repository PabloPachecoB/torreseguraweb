"""Servicios de dominio reutilizables para áreas comunes."""

from datetime import datetime, timedelta

from .models import Reserva


def available_slots(area, query_date, duration_minutes):
    """Calcula huecos reales dentro del horario y fuera de reservas activas."""
    opening = datetime.combine(query_date, area.horario_inicio)
    closing = datetime.combine(query_date, area.horario_fin)
    duration = timedelta(minutes=duration_minutes)
    occupied = list(
        Reserva.objects.filter(
            area_comun=area,
            fecha=query_date,
            estado__in=["pendiente", "confirmada"],
        )
        .order_by("hora_inicio")
        .values_list("hora_inicio", "hora_fin")
    )

    slots = []
    cursor = opening
    for occupied_start, occupied_end in occupied:
        gap_end = min(datetime.combine(query_date, occupied_start), closing)
        slot_start = cursor
        while slot_start + duration <= gap_end:
            slots.append((slot_start.time(), (slot_start + duration).time()))
            slot_start += duration
        cursor = max(cursor, datetime.combine(query_date, occupied_end))

    slot_start = cursor
    while slot_start + duration <= closing:
        slots.append((slot_start.time(), (slot_start + duration).time()))
        slot_start += duration

    return [
        {"hora_inicio": start.strftime("%H:%M"), "hora_fin": end.strftime("%H:%M")}
        for start, end in slots
    ]
