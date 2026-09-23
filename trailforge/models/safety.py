from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from trailforge.database.base import Base, UTCDateTime, utc_now
from trailforge.domain.enums import (
    CheckInType,
    EmergencyStatus,
    EmergencyType,
    RiskLevel,
    TimelineEntryType,
)
from trailforge.models.mixins import IntegerPrimaryKeyMixin, TimestampMixin, VersionMixin


class ItineraryCheckIn(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "itinerary_check_ins"
    __table_args__ = (
        UniqueConstraint(
            "expedition_id", "user_id", "check_in_type", "due_at", name="uq_check_in_slot"
        ),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN -180 AND 180", name="longitude_range"
        ),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    check_in_type: Mapped[CheckInType] = mapped_column(String(24), nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_safe: Mapped[bool | None] = mapped_column(Boolean)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class EmergencyIncident(IntegerPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "emergency_incidents"
    __table_args__ = (
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN -180 AND 180", name="longitude_range"
        ),
        CheckConstraint("timeline_head_seq >= 0", name="timeline_head_nonnegative"),
        CheckConstraint("confirmed_handover_seq >= 0", name="confirmed_handover_nonnegative"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    reported_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    incident_type: Mapped[EmergencyType] = mapped_column(String(32), nullable=False, index=True)
    risk_level: Mapped[RiskLevel] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[EmergencyStatus] = mapped_column(
        String(24), default=EmergencyStatus.OPEN, nullable=False, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)
    timeline_head_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confirmed_handover_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class IncidentTimelineEntry(IntegerPrimaryKeyMixin, Base):
    __tablename__ = "incident_timeline_entries"
    __table_args__ = (
        UniqueConstraint("incident_id", "seq", name="uq_incident_timeline_seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(
            "entry_type != 'handover' OR "
            "(handover_to_user_id IS NOT NULL AND confirm_through_seq IS NOT NULL)",
            name="handover_fields",
        ),
        CheckConstraint(
            "entry_type != 'status_suggestion' OR suggested_status IS NOT NULL",
            name="suggestion_fields",
        ),
    )

    incident_id: Mapped[int] = mapped_column(
        ForeignKey("emergency_incidents.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[TimelineEntryType] = mapped_column(String(24), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    suggested_status: Mapped[EmergencyStatus | None] = mapped_column(String(24))
    handover_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    confirm_through_seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


_APPEND_ONLY_MESSAGE = "incident timeline entries are append-only"

TIMELINE_APPEND_ONLY_TRIGGERS = (
    DDL(
        "CREATE TRIGGER trg_incident_timeline_no_update "
        "BEFORE UPDATE ON incident_timeline_entries "
        f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MESSAGE}'); END"
    ),
    DDL(
        "CREATE TRIGGER trg_incident_timeline_no_delete "
        "BEFORE DELETE ON incident_timeline_entries "
        f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MESSAGE}'); END"
    ),
)

for _trigger in TIMELINE_APPEND_ONLY_TRIGGERS:
    event.listen(IncidentTimelineEntry.__table__, "after_create", _trigger)


class RiskAssessment(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "risk_assessments"
    __table_args__ = (
        CheckConstraint("likelihood BETWEEN 1 AND 5", name="likelihood_range"),
        CheckConstraint("impact BETWEEN 1 AND 5", name="impact_range"),
        CheckConstraint("score BETWEEN 1 AND 25", name="score_range"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    assessor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    hazard: Mapped[str] = mapped_column(String(240), nullable=False)
    likelihood: Mapped[int] = mapped_column(Integer, nullable=False)
    impact: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(String(24), nullable=False, index=True)
    mitigation: Mapped[str] = mapped_column(Text, nullable=False)
    residual_risk: Mapped[str] = mapped_column(Text, default="", nullable=False)


class WeatherSnapshot(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "weather_snapshots"
    __table_args__ = (
        CheckConstraint("temperature_c BETWEEN -80 AND 70", name="temperature_range"),
        CheckConstraint("wind_speed_kph BETWEEN 0 AND 500", name="wind_range"),
        CheckConstraint("precipitation_mm >= 0", name="precipitation_nonnegative"),
        CheckConstraint("visibility_km >= 0", name="visibility_nonnegative"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    recorded_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    location_label: Mapped[str] = mapped_column(String(160), nullable=False)
    temperature_c: Mapped[float] = mapped_column(Float, nullable=False)
    wind_speed_kph: Mapped[float] = mapped_column(Float, nullable=False)
    precipitation_mm: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    visibility_km: Mapped[float] = mapped_column(Float, nullable=False)
    conditions: Mapped[str] = mapped_column(String(160), nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    is_manual_observation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
