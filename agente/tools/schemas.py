"""Esquemas tipados de entrada para tools del agente."""

from datetime import date, time
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_id: int = Field(gt=0)
    date: date
    start_time: time
    end_time: time
    attendees: int = Field(gt=0)
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time debe ser posterior a start_time")
        return self


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    error_code: Optional[str] = None
    message: str = ""


class PreliminaryIncidentEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "PLOMERIA",
        "ELECTRICIDAD",
        "ASCENSOR",
        "SEGURIDAD",
        "LIMPIEZA",
        "OTRO",
    ]
    urgency: Literal["BAJA", "MEDIA", "ALTA", "CRITICA"]
    response_window: str = Field(max_length=200)
    cost_note: str = Field(max_length=200)
    disclaimer: str = Field(max_length=300)


class IncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=150)
    description: str = Field(min_length=5, max_length=4000)
    location: str = Field(min_length=2, max_length=200)
    category: Literal[
        "PLOMERIA",
        "ELECTRICIDAD",
        "ASCENSOR",
        "SEGURIDAD",
        "LIMPIEZA",
        "OTRO",
    ]
    urgency: Literal["BAJA", "MEDIA", "ALTA", "CRITICA"]
    preliminary_estimate: PreliminaryIncidentEstimate
