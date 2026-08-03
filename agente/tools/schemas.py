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
    estimated_hours: int = Field(gt=0, le=720)
    estimated_cost_min: Optional[float] = Field(default=None, ge=0)
    estimated_cost_max: Optional[float] = Field(default=None, ge=0)
    currency: str = Field(default="BOB", min_length=3, max_length=3)
    cost_note: str = Field(max_length=200)
    disclaimer: str = Field(max_length=300)

    @model_validator(mode="after")
    def validate_cost_range(self):
        if (
            self.estimated_cost_min is not None
            and self.estimated_cost_max is not None
            and self.estimated_cost_max < self.estimated_cost_min
        ):
            raise ValueError("estimated_cost_max debe ser mayor o igual al mínimo")
        return self


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


class DoorOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    door_id: int = Field(gt=0)


class VisitAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=100)
    document: str = Field(min_length=6, max_length=20)
    date: date
    start_time: time
    end_time: time
    attendees: int = Field(gt=0)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time debe ser posterior a start_time")
        return self
