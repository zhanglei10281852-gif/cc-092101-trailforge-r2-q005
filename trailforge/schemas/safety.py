from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import (
    CheckInType,
    EmergencyStatus,
    EmergencyType,
    RiskLevel,
    TimelineEntryType,
)
from trailforge.schemas.common import (
    ORMModel,
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


class EmergencyIncidentResponse(VersionedResponse):
    expedition_id: int
    reported_by: int
    owner_id: int
    incident_type: EmergencyType
    risk_level: RiskLevel
    status: EmergencyStatus
    occurred_at: datetime
    resolved_at: datetime | None
    latitude: float | None
    longitude: float | None
    description: str
    resolution: str
    timeline_head_seq: int
    confirmed_handover_seq: int


class TimelineEntryCreate(BaseModel):
    entry_type: TimelineEntryType
    body: str = Field(min_length=1, max_length=10000)
    actor_id: int = Field(gt=0)
    suggested_status: EmergencyStatus | None = None
    handover_to_user_id: int | None = Field(default=None, gt=0)
    confirm_through_seq: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        return clean_text(value)

    @model_validator(mode="after")
    def type_specific_fields(self) -> TimelineEntryCreate:
        if self.entry_type is TimelineEntryType.HANDOVER:
            if self.handover_to_user_id is None or self.confirm_through_seq is None:
                raise ValueError(
                    "handover entries require handover_to_user_id and confirm_through_seq"
                )
            if self.suggested_status is not None:
                raise ValueError("handover entries cannot carry a suggested_status")
        elif self.entry_type is TimelineEntryType.STATUS_SUGGESTION:
            if self.suggested_status is None:
                raise ValueError("status suggestion entries require suggested_status")
            if self.handover_to_user_id is not None or self.confirm_through_seq is not None:
                raise ValueError("status suggestion entries cannot carry handover fields")
        elif (
            self.suggested_status is not None
            or self.handover_to_user_id is not None
            or self.confirm_through_seq is not None
        ):
            raise ValueError("observation and action entries only carry a body")
        return self


class TimelineEntryResponse(ORMModel):
    id: int
    incident_id: int
    seq: int
    entry_type: TimelineEntryType
    body: str
    actor_id: int
    suggested_status: EmergencyStatus | None
    handover_to_user_id: int | None
    confirm_through_seq: int | None
    created_at: datetime


class IncidentTimelineSlice(BaseModel):
    incident_id: int
    status: EmergencyStatus
    owner_id: int
    head_seq: int
    items: list[TimelineEntryResponse]


class IncidentStatusTransition(BaseModel):
    target_status: EmergencyStatus
    actor_id: int = Field(gt=0)
    final_action_seq: int | None = Field(default=None, ge=1)
    suggestion_seq: int | None = Field(default=None, ge=1)
    expected_version: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=2000)


class HandoverConfirm(BaseModel):
    actor_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)


class PendingHandover(BaseModel):
    incident_id: int
    expedition_id: int
    handover_seq: int
    confirm_through_seq: int
    handover_from_actor_id: int
    current_owner_id: int
    requested_at: datetime


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
