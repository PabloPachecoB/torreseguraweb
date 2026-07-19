"""Matriz central de datos que un residente puede consultar."""

from typing import Any, Dict, Optional


class ResidentVisibilityPolicy:
    TOPIC_SCOPES = {
        "resident_overview": "apartment",
        "profile_info": "resident",
        "housing_info": "apartment",
        "pending_fees": "apartment",
        "paid_fees": "apartment",
        "payment_history": "apartment",
        "my_payments": "resident",
        "pending_payment_qrs": "apartment",
        "account_statements": "apartment",
        "common_areas": "building",
        "area_availability": "building",
        "my_reservations": "resident",
        "scheduled_visits": "apartment",
        "visit_history": "apartment",
        "allowed_doors": "resident",
        "access_history": "resident",
        "my_incidents": "resident",
        "incident_detail": "resident",
        "announcements": "building",
        "building_alerts": "building",
        "active_polls": "building",
    }
    CONTEXT_KEYS = {
        "resident": "resident_id",
        "apartment": "apartment_id",
        "building": "building_id",
        "condominium": "condominium_id",
    }

    @classmethod
    def authorize(
        cls, context: Dict[str, Any], topic: str
    ) -> Optional[Dict[str, str]]:
        scope = cls.TOPIC_SCOPES.get(topic)
        key = cls.CONTEXT_KEYS.get(scope or "")
        if not scope:
            return cls.error("unsupported_information_topic", "Consulta no permitida.")
        if not context.get("resident_active") or not key or not context.get(key):
            return cls.error(
                "resident_context_required",
                "No existe un residente activo con el contexto necesario.",
            )
        return None

    @staticmethod
    def mask_document(value: str) -> str:
        cleaned = str(value or "").strip()
        if len(cleaned) <= 2:
            return "**"
        return "*" * (len(cleaned) - 2) + cleaned[-2:]

    @staticmethod
    def error(code: str, message: str) -> Dict[str, str]:
        return {"status": "error", "error_code": code, "message": message}
