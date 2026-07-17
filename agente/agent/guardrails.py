"""Guardias deterministas para respuestas generadas por el modelo."""

import re
import unicodedata
from typing import Tuple


UNVERIFIED_ACTION_MESSAGE = (
    "No puedo presentar esta solicitud como ejecutada porque no existe una "
    "acción verificada. Puedo ayudarte a preparar un borrador, pero una reserva "
    "o incidencia solo queda creada después de la confirmación autenticada y la "
    "verificación del backend."
)

_ACTION_CLAIM_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bgracias\s+por\s+confirmar\b",
        (
            r"\b(?:esta|ya\s+esta|ha\s+sido|fue|quedo|se\s+encuentra)\s+"
            r"(?:reservad[oa]|confirmad[oa]|programad[oa]|agendad[oa]|"
            r"cread[oa]|registrad[oa]|reportad[oa])s?\b"
        ),
        (
            r"\b(?:he|hemos|se\s+ha|ya\s+se)\s+"
            r"(?:reservado|confirmado|programado|agendado|creado|registrado|"
            r"reportado)\b"
        ),
        (
            r"\b(?:reserva|reservacion|reunion|evento|incidencia|reporte|"
            r"solicitud|visita)\s+"
            r"(?:reservad[oa]|confirmad[oa]|programad[oa]|agendad[oa]|"
            r"cread[oa]|registrad[oa]|reportad[oa])s?\b"
        ),
        r"\b(?:confirmacion|reserva|registro)\s+(?:completad[oa]|exitosa?)\b",
    )
)


def guard_unverified_action_claim(response: str) -> Tuple[str, bool]:
    """Reemplaza afirmaciones de ejecución sin respaldo de una herramienta."""
    normalized = unicodedata.normalize("NFKD", response)
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).lower()
    if any(pattern.search(normalized) for pattern in _ACTION_CLAIM_PATTERNS):
        return UNVERIFIED_ACTION_MESSAGE, True
    return response, False
