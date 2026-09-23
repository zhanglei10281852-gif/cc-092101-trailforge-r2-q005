from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import (
    CheckInType,
    EmergencyStatus,
    EmergencyType,
    IncidentEntryKind,
    RiskLevel,
)
from trailforge.schemas.common import (
    TimestampedResponse,
    VersionedResponse,
    clean_text,
    require_aware,
)


class CheckInScheduleCreate(BaseModel):
    user_id: int = Field(gt=0)
    check_in_type: CheckInType
    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class CheckInSubmit(BaseModel):
    checked_in_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    note: str = Field(default="", max_length=4000)
    is_safe: bool
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("checked_in_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def coordinate_pair(self) -> CheckInSubmit:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class CheckInResponse(TimestampedResponse):
    expedition_id: int
    user_id: int
    check_in_type: CheckInType
    due_at: datetime
    checked_in_at: datetime | None
    latitude: float | None
    longitude: float | None
    note: str
    is_safe: bool | None
    late_minutes: int


class OverdueCheckIn(BaseModel):
    check_in_id: int
    expedition_id: int
    expedition_name: str
    user_id: int
    display_name: str
    check_in_type: CheckInType
    due_at: datetime
    overdue_minutes: int
    risk_level: RiskLevel


class EmergencyIncidentCreate(BaseModel):
    expedition_id: int = Field(gt=0)
    reported_by: int = Field(gt=0)
    incident_type: EmergencyType
    risk_level: RiskLevel
    occurred_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    description: str = Field(min_length=1, max_length=10000)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return clean_text(value)

    @model_validator(mode="after")
    def coordinate_pair(self) -> EmergencyIncidentCreate:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class IncidentEntryAppend(BaseModel):
    author_id: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=10000)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return clean_text(value)


class StatusSuggestionAppend(IncidentEntryAppend):
    suggested_status: EmergencyStatus


class HandoverAppend(IncidentEntryAppend):
    successor_id: int = Field(gt=0)


class HandoverConfirm(BaseModel):
    successor_id: int = Field(gt=0)
    confirm_through_seq: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)


class IncidentTransition(BaseModel):
    actor_id: int = Field(gt=0)
    target_status: EmergencyStatus
    final_action_entry_id: int | None = Field(default=None, gt=0)
    resolved_at: datetime | None = None
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("resolved_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None


class HandoverConfirmationResponse(TimestampedResponse):
    handover_entry_id: int
    incident_id: int
    confirmed_by: int
    confirmed_through_seq: int


class IncidentTimelineEntryResponse(TimestampedResponse):
    incident_id: int
    seq: int
    kind: IncidentEntryKind
    author_id: int
    content: str
    suggested_status: EmergencyStatus | None
    successor_id: int | None
    required_through_seq: int | None
    handover_confirmation: HandoverConfirmationResponse | None


class IncidentTimelinePage(BaseModel):
    incident_id: int
    entries: list[IncidentTimelineEntryResponse]
    next_after_seq: int
    has_more: bool


class HandoverTodoItem(BaseModel):
    incident_id: int
    expedition_id: int
    entry_id: int
    entry_seq: int
    required_through_seq: int
    current_head_seq: int
    from_owner_id: int
    successor_id: int
    created_at: datetime


class EmergencyIncidentResponse(VersionedResponse):
    expedition_id: int
    reported_by: int
    incident_type: EmergencyType
    risk_level: RiskLevel
    status: EmergencyStatus
    occurred_at: datetime
    resolved_at: datetime | None
    latitude: float | None
    longitude: float | None
    description: str


class RiskAssessmentCreate(BaseModel):
    expedition_id: int = Field(gt=0)
    assessor_id: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=80)
    hazard: str = Field(min_length=1, max_length=240)
    likelihood: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    mitigation: str = Field(min_length=1, max_length=10000)
    residual_risk: str = Field(default="", max_length=4000)

    @field_validator("category", "hazard", "mitigation")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return clean_text(value)


class RiskAssessmentResponse(TimestampedResponse):
    expedition_id: int
    assessor_id: int
    category: str
    hazard: str
    likelihood: int
    impact: int
    score: int
    risk_level: RiskLevel
    mitigation: str
    residual_risk: str


class WeatherSnapshotCreate(BaseModel):
    expedition_id: int = Field(gt=0)
    recorded_by: int = Field(gt=0)
    observed_at: datetime
    location_label: str = Field(min_length=1, max_length=160)
    temperature_c: float = Field(ge=-80, le=70)
    wind_speed_kph: float = Field(ge=0, le=500)
    precipitation_mm: float = Field(default=0, ge=0, le=5000)
    visibility_km: float = Field(ge=0, le=1000)
    conditions: str = Field(min_length=1, max_length=160)
    source_note: str = Field(min_length=1, max_length=2000)
    is_manual_observation: bool = True

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("location_label", "conditions", "source_note")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return clean_text(value)


class WeatherSnapshotResponse(TimestampedResponse):
    expedition_id: int
    recorded_by: int
    observed_at: datetime
    location_label: str
    temperature_c: float
    wind_speed_kph: float
    precipitation_mm: float
    visibility_km: float
    conditions: str
    source_note: str
    is_manual_observation: bool


class SafetySummary(BaseModel):
    expedition_id: int
    generated_at: datetime
    scheduled_check_ins: int
    completed_check_ins: int
    overdue_check_ins: int
    unsafe_check_ins: int
    open_incidents: int
    highest_incident_risk: RiskLevel | None
    assessment_count: int
    highest_assessment_score: int | None
    latest_weather_snapshot: WeatherSnapshotResponse | None
    warnings: list[str]


class RiskStatistics(BaseModel):
    total_incidents: int
    open_incidents: int
    resolved_incidents: int
    incidents_by_type: dict[str, int]
    incidents_by_level: dict[str, int]
    overdue_check_ins: int
    unsafe_check_ins: int
    average_assessment_score: float
