"""Consultas autenticadas y de solo lectura para residentes."""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db.models import Q
from django.utils import timezone

from accesos.models import AperturaPuerta, MovimientoResidente, Visita
from accesos.services import allowed_doors, serialize_door
from agente.policies import ResidentVisibilityPolicy
from alertas.models import Alerta, Anuncio, Voto
from areas_comunes.models import AreaComun, Reserva
from areas_comunes.services import available_slots
from financiero.models import Cuota, EstadoCuenta, Pago, PagoQR
from incidencias.models import Incidencia
from viviendas.models import Residente


class InformationTools:
    """Expone datos del dominio sin confiar en identificadores del mensaje."""

    tool_name = "query_resident_information"
    policy = ResidentVisibilityPolicy

    def get_resident_overview(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "resident_overview")
        if error:
            return error
        today = timezone.localdate()
        apartment_id = context["apartment_id"]
        building_id = context["building_id"]
        return {
            "status": "success",
            "topic": "resident_overview",
            "profile": self.get_profile(context)["profile"],
            "housing": self.get_housing(context)["housing"],
            "counts": {
                "pending_fees": Cuota.objects.filter(
                    vivienda_id=apartment_id, pagada=False
                ).count(),
                "scheduled_visits": Visita.objects.filter(
                    vivienda_destino_id=apartment_id,
                    fecha_visita__gte=today,
                    estado__in=[Visita.RESERVADA, Visita.PENDIENTE_APROBACION],
                ).count(),
                "upcoming_reservations": Reserva.objects.filter(
                    residente_id=context["resident_id"], fecha__gte=today
                ).exclude(estado="cancelada").count(),
                "open_incidents": Incidencia.objects.filter(
                    residente_id=context["resident_id"]
                ).exclude(
                    estado__in=[Incidencia.RESUELTA, Incidencia.RECHAZADA, Incidencia.CANCELADA]
                ).count(),
                "active_announcements": Anuncio.objects.filter(
                    edificio_id=building_id, activo=True
                ).count(),
                "active_polls": Anuncio.objects.filter(
                    Q(fecha_cierre_votacion__isnull=True)
                    | Q(fecha_cierre_votacion__gte=timezone.now()),
                    edificio_id=building_id,
                    activo=True,
                    es_votacion=True,
                ).count(),
            },
        }

    def list_common_areas(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self._require_resident_context(context, "building_id")
        if error:
            return error
        areas = AreaComun.objects.filter(
            edificio_id=context["building_id"], activo=True
        ).select_related("edificio").order_by("nombre")
        return {
            "status": "success",
            "topic": "common_areas",
            "building": (
                areas[0].edificio.nombre if areas else self._building_name(context)
            ),
            "areas": [self._serialize_area(area) for area in areas],
        }

    def get_area_availability(
        self,
        context: Dict[str, Any],
        area_id: int,
        query_date: date,
        duration_minutes: int = 60,
    ) -> Dict[str, Any]:
        error = self._require_resident_context(context, "building_id")
        if error:
            return error
        if query_date < timezone.localdate():
            return self._error("past_date", "La fecha no puede estar en el pasado.")
        if duration_minutes <= 0 or duration_minutes > 720:
            return self._error(
                "invalid_duration", "La duración debe estar entre 1 y 720 minutos."
            )
        area = AreaComun.objects.filter(
            pk=area_id,
            edificio_id=context["building_id"],
            activo=True,
        ).first()
        if area is None:
            return self._error(
                "area_not_found",
                "El área no existe o no pertenece al edificio del residente.",
            )
        return {
            "status": "success",
            "topic": "area_availability",
            "area": self._serialize_area(area),
            "date": query_date.isoformat(),
            "duration_minutes": duration_minutes,
            "slots": available_slots(area, query_date, duration_minutes),
        }

    def list_my_reservations(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self._require_resident_context(context, "resident_id")
        if error:
            return error
        reservations = (
            Reserva.objects.filter(
                residente_id=context["resident_id"],
                fecha__gte=timezone.localdate(),
            )
            .exclude(estado="cancelada")
            .select_related("area_comun")
            .order_by("fecha", "hora_inicio")[:20]
        )
        return {
            "status": "success",
            "topic": "my_reservations",
            "reservations": [
                {
                    "id": item.pk,
                    "area": item.area_comun.nombre,
                    "date": item.fecha.isoformat(),
                    "start_time": item.hora_inicio.strftime("%H:%M"),
                    "end_time": item.hora_fin.strftime("%H:%M"),
                    "status": item.get_estado_display(),
                }
                for item in reservations
            ],
        }

    def get_pending_fees(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self._require_resident_context(context, "apartment_id")
        if error:
            return error
        fees = Cuota.objects.filter(
            vivienda_id=context["apartment_id"], pagada=False
        ).select_related("concepto").order_by("fecha_vencimiento")
        items = []
        total = Decimal("0.00")
        today = timezone.localdate()
        for fee in fees:
            surcharge = fee.calcular_recargo()
            amount = fee.monto + surcharge
            total += amount
            items.append(
                {
                    "id": fee.pk,
                    "concept": fee.concepto.nombre,
                    "amount": str(fee.monto),
                    "surcharge": str(surcharge),
                    "total": str(amount),
                    "due_date": fee.fecha_vencimiento.isoformat(),
                    "overdue": fee.fecha_vencimiento < today,
                }
            )
        return {
            "status": "success",
            "topic": "pending_fees",
            "total": str(total),
            "fees": items,
        }

    def get_paid_fees(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self._require_resident_context(context, "apartment_id")
        if error:
            return error
        fees = Cuota.objects.filter(
            vivienda_id=context["apartment_id"], pagada=True
        ).select_related("concepto").order_by("-fecha_vencimiento")[:50]
        return {
            "status": "success",
            "topic": "paid_fees",
            "fees": [
                {
                    "id": fee.pk,
                    "concept": fee.concepto.nombre,
                    "amount": str(fee.monto),
                    "due_date": fee.fecha_vencimiento.isoformat(),
                }
                for fee in fees
            ],
        }

    def get_payment_history(
        self, context: Dict[str, Any], only_resident: bool = False
    ) -> Dict[str, Any]:
        error = self._require_resident_context(context, "apartment_id")
        if not error and only_resident:
            error = self._require_resident_context(context, "resident_id")
        if error:
            return error
        payments = Pago.objects.filter(vivienda_id=context["apartment_id"])
        if only_resident:
            payments = payments.filter(residente_id=context["resident_id"])
        payments = payments.order_by("-fecha_pago", "-id")[:50]
        return {
            "status": "success",
            "topic": "my_payments" if only_resident else "payment_history",
            "payments": [
                {
                    "id": payment.pk,
                    "amount": str(payment.monto),
                    "date": payment.fecha_pago.isoformat(),
                    "method": payment.get_metodo_pago_display(),
                    "status": payment.get_estado_display(),
                    "reference": payment.referencia,
                }
                for payment in payments
            ],
        }

    def get_profile(self, context: Dict[str, Any]) -> Dict[str, Any]:
        resident = self._resident(context)
        if resident is None:
            return self._resident_error()
        user = resident.usuario
        return {
            "status": "success",
            "topic": "profile_info",
            "profile": {
                "name": user.get_full_name().strip() or user.username,
                "username": user.username,
                "email": user.email,
                "phone": user.telefono,
                "resident_type": resident.tipo_residente,
                "vehicles": resident.vehiculos,
                "active": resident.activo,
            },
        }

    def get_housing(self, context: Dict[str, Any]) -> Dict[str, Any]:
        resident = self._resident(context)
        if resident is None or resident.vivienda is None:
            return self._resident_error()
        housing = resident.vivienda
        building = housing.edificio
        return {
            "status": "success",
            "topic": "housing_info",
            "housing": {
                "number": housing.numero,
                "floor": housing.piso,
                "building": building.nombre,
                "condominium": building.condominio.nombre if building.condominio else "",
                "address": building.direccion,
                "square_meters": str(housing.metros_cuadrados),
                "rooms": housing.habitaciones,
                "bathrooms": housing.baños,
            },
        }

    def get_pending_payment_qrs(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "pending_payment_qrs")
        if error:
            return error
        items = PagoQR.objects.filter(
            vivienda_id=context["apartment_id"],
            qr_estado="GENERADO",
            fecha_expiracion__gte=timezone.localdate(),
        ).order_by("-fecha_creacion")[:20]
        return {
            "status": "success",
            "topic": "pending_payment_qrs",
            "qrs": [
                {
                    "amount": str(item.monto),
                    "description": item.glosa,
                    "expires": item.fecha_expiracion.isoformat(),
                    "status": item.get_qr_estado_display(),
                }
                for item in items
            ],
        }

    def get_account_statements(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "account_statements")
        if error:
            return error
        items = EstadoCuenta.objects.filter(
            vivienda_id=context["apartment_id"]
        ).order_by("-fecha_fin")[:20]
        return {
            "status": "success",
            "topic": "account_statements",
            "statements": [
                {
                    "period_start": item.fecha_inicio.isoformat(),
                    "period_end": item.fecha_fin.isoformat(),
                    "fees": str(item.total_cuotas),
                    "payments": str(item.total_pagos),
                    "surcharges": str(item.total_recargos),
                    "balance": str(item.saldo_final),
                }
                for item in items
            ],
        }

    def get_visits(
        self, context: Dict[str, Any], scheduled_only: bool = True
    ) -> Dict[str, Any]:
        topic = "scheduled_visits" if scheduled_only else "visit_history"
        error = self.policy.authorize(context, topic)
        if error:
            return error
        visits = Visita.objects.filter(vivienda_destino_id=context["apartment_id"])
        if scheduled_only:
            visits = visits.filter(
                fecha_visita__gte=timezone.localdate(),
                estado__in=[Visita.RESERVADA, Visita.PENDIENTE_APROBACION],
            ).order_by("fecha_visita", "hora_inicio")
        else:
            visits = visits.order_by("-id")
        return {
            "status": "success",
            "topic": topic,
            "visits": [self._serialize_visit(item) for item in visits[:50]],
        }

    def get_allowed_doors(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "allowed_doors")
        if error:
            return error
        resident = self._resident(context)
        return {
            "status": "success",
            "topic": "allowed_doors",
            "doors": [serialize_door(item) for item in allowed_doors(resident.usuario)],
        }

    def get_access_history(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "access_history")
        if error:
            return error
        openings = AperturaPuerta.objects.filter(
            usuario_id=context["user_id"]
        ).select_related("puerta").order_by("-fecha_hora")[:25]
        movements = MovimientoResidente.objects.filter(
            residente_id=context["resident_id"]
        ).order_by("-fecha_hora_entrada", "-fecha_hora_salida")[:25]
        return {
            "status": "success",
            "topic": "access_history",
            "openings": [
                {
                    "door": item.puerta.nombre,
                    "date": item.fecha_hora.isoformat(),
                    "success": item.exito,
                }
                for item in openings
            ],
            "movements": [
                {
                    "entry": item.fecha_hora_entrada.isoformat() if item.fecha_hora_entrada else None,
                    "exit": item.fecha_hora_salida.isoformat() if item.fecha_hora_salida else None,
                    "vehicle": item.vehiculo,
                    "plate": item.placa_vehiculo,
                }
                for item in movements
            ],
        }

    def get_my_incidents(
        self, context: Dict[str, Any], incident_id: Optional[int] = None
    ) -> Dict[str, Any]:
        topic = "incident_detail" if incident_id else "my_incidents"
        error = self.policy.authorize(context, topic)
        if error:
            return error
        qs = Incidencia.objects.filter(residente_id=context["resident_id"])
        if incident_id:
            qs = qs.filter(pk=incident_id).prefetch_related("eventos")
        items = list(qs[:50])
        if incident_id and not items:
            return self._error("incident_not_found", "Incidencia no encontrada.")
        return {
            "status": "success",
            "topic": topic,
            "incidents": [self._serialize_incident(item, bool(incident_id)) for item in items],
        }

    def get_announcements(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "announcements")
        if error:
            return error
        items = Anuncio.objects.filter(
            edificio_id=context["building_id"], activo=True
        ).order_by("-fijado", "-fecha_creacion")[:50]
        return {
            "status": "success",
            "topic": "announcements",
            "announcements": [
                {
                    "title": item.titulo,
                    "content": item.contenido,
                    "category": item.get_categoria_display(),
                    "date": item.fecha_creacion.isoformat(),
                    "pinned": item.fijado,
                }
                for item in items
            ],
        }

    def get_building_alerts(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "building_alerts")
        if error:
            return error
        items = Alerta.objects.filter(
            edificio_id=context["building_id"], vivienda__isnull=True
        ).exclude(tipo="Incidencia").order_by("-fecha")[:50]
        return {
            "status": "success",
            "topic": "building_alerts",
            "alerts": [
                {
                    "type": item.tipo,
                    "description": item.descripcion,
                    "status": item.get_estado_display(),
                    "date": item.fecha.isoformat(),
                }
                for item in items
            ],
        }

    def get_active_polls(self, context: Dict[str, Any]) -> Dict[str, Any]:
        error = self.policy.authorize(context, "active_polls")
        if error:
            return error
        polls = Anuncio.objects.filter(
            Q(fecha_cierre_votacion__isnull=True)
            | Q(fecha_cierre_votacion__gte=timezone.now()),
            edificio_id=context["building_id"],
            activo=True,
            es_votacion=True,
        ).prefetch_related("opciones__votos").order_by("-fecha_creacion")[:20]
        results = []
        for poll in polls:
            own_vote = Voto.objects.filter(
                opcion__anuncio=poll, usuario_id=context["user_id"]
            ).select_related("opcion").first()
            results.append(
                {
                    "title": poll.titulo,
                    "open": poll.votacion_abierta,
                    "closes": poll.fecha_cierre_votacion.isoformat() if poll.fecha_cierre_votacion else None,
                    "my_vote": own_vote.opcion.texto if own_vote else None,
                    "options": [
                        {"text": option.texto, "votes": option.votos.count()}
                        for option in poll.opciones.all()
                    ],
                }
            )
        return {"status": "success", "topic": "active_polls", "polls": results}

    def query(self, context: Dict[str, Any], topic: str, fields: Dict[str, Any]):
        dispatch = {
            "resident_overview": self.get_resident_overview,
            "common_areas": self.list_common_areas,
            "my_reservations": self.list_my_reservations,
            "pending_fees": self.get_pending_fees,
            "paid_fees": self.get_paid_fees,
            "payment_history": self.get_payment_history,
            "my_payments": lambda ctx: self.get_payment_history(ctx, True),
            "pending_payment_qrs": self.get_pending_payment_qrs,
            "account_statements": self.get_account_statements,
            "housing_info": self.get_housing,
            "profile_info": self.get_profile,
            "scheduled_visits": self.get_visits,
            "visit_history": lambda ctx: self.get_visits(ctx, False),
            "allowed_doors": self.get_allowed_doors,
            "access_history": self.get_access_history,
            "my_incidents": self.get_my_incidents,
            "incident_detail": lambda ctx: self.get_my_incidents(ctx, fields.get("record_id")),
            "announcements": self.get_announcements,
            "building_alerts": self.get_building_alerts,
            "active_polls": self.get_active_polls,
        }
        if topic == "area_availability":
            query_date = fields["date"]
            if isinstance(query_date, str):
                query_date = date.fromisoformat(query_date)
            return self.get_area_availability(
                context,
                fields["area_id"],
                query_date,
                fields.get("duration_minutes", 60),
            )
        handler = dispatch.get(topic)
        return handler(context) if handler else self.policy.authorize(context, topic)

    @classmethod
    def _serialize_visit(cls, item: Visita) -> Dict[str, Any]:
        return {
            "name": item.nombre_visitante,
            "document": cls.policy.mask_document(item.documento_visitante),
            "date": item.fecha_visita.isoformat() if item.fecha_visita else None,
            "start_time": item.hora_inicio.strftime("%H:%M") if item.hora_inicio else None,
            "end_time": item.hora_fin.strftime("%H:%M") if item.hora_fin else None,
            "people": item.cantidad_personas,
            "status": item.get_estado_display(),
        }

    @staticmethod
    def _serialize_incident(item: Incidencia, detail: bool) -> Dict[str, Any]:
        result = {
            "id": item.pk,
            "title": item.titulo,
            "category": item.get_categoria_display(),
            "urgency": item.get_urgencia_display(),
            "status": item.get_estado_display(),
            "updated": item.fecha_actualizacion.isoformat(),
        }
        if detail:
            result["description"] = item.descripcion
            result["location"] = item.ubicacion
            result["timeline"] = [
                {
                    "type": event.get_tipo_evento_display(),
                    "comment": event.comentario,
                    "date": event.fecha.isoformat(),
                }
                for event in item.eventos.all()
            ]
        return result

    @staticmethod
    def _serialize_area(area: AreaComun) -> Dict[str, Any]:
        return {
            "id": area.pk,
            "name": area.nombre,
            "description": area.descripcion,
            "capacity": area.capacidad_maxima,
            "opening_time": area.horario_inicio.strftime("%H:%M"),
            "closing_time": area.horario_fin.strftime("%H:%M"),
        }

    @staticmethod
    def _require_resident_context(
        context: Dict[str, Any], required_key: str
    ) -> Optional[Dict[str, Any]]:
        if not context.get("resident_active") or not context.get(required_key):
            return InformationTools._resident_error()
        return None

    @staticmethod
    def _resident(context: Dict[str, Any]):
        if not context.get("resident_active") or not context.get("resident_id"):
            return None
        return Residente.objects.select_related(
            "usuario", "vivienda__edificio__condominio"
        ).filter(pk=context["resident_id"], activo=True).first()

    @staticmethod
    def _building_name(context: Dict[str, Any]) -> str:
        resident = InformationTools._resident(context)
        if resident and resident.vivienda:
            return resident.vivienda.edificio.nombre
        return ""

    @staticmethod
    def _resident_error() -> Dict[str, Any]:
        return InformationTools._error(
            "resident_context_required",
            "No existe un residente activo con vivienda asociada.",
        )

    @staticmethod
    def _error(error_code: str, message: str) -> Dict[str, Any]:
        return {"status": "error", "error_code": error_code, "message": message}
