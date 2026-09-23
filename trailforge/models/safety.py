from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from trailforge.database.base import Base, UTCDateTime
from trailforge.domain.enums import (
    CheckInType,
    EmergencyStatus,
    EmergencyType,
    IncidentEntryKind,
    RiskLevel,
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
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    reported_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
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


class IncidentTimelineEntry(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only timeline record for an emergency incident.

    Rows are never updated or deleted (enforced by database triggers); history is
    preserved so the next shift can see exactly what happened and who confirmed it.
    """

    __tablename__ = "incident_timeline_entries"
    __table_args__ = (
        UniqueConstraint("incident_id", "seq", name="uq_timeline_incident_seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(
            "kind != 'handover' OR (successor_id IS NOT NULL AND required_through_seq IS NOT NULL)",
            name="handover_fields",
        ),
        Index("ix_timeline_incident_seq", "incident_id", "seq"),
    )

    incident_id: Mapped[int] = mapped_column(
        ForeignKey("emergency_incidents.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[IncidentEntryKind] = mapped_column(String(24), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_status: Mapped[EmergencyStatus | None] = mapped_column(String(24))
    successor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    required_through_seq: Mapped[int | None] = mapped_column(Integer)


class ActiveHandover(TimestampMixin, Base):
    """Mutable pointer to the single unconfirmed handover of an incident.

    This is derived state, not history: the authoritative record stays in the
    append-only timeline. A row exists only while a handover awaits confirmation;
    its primary key on incident_id hard-blocks two pending handovers even under
    concurrent requests, and it is removed once the handover is confirmed or the
    incident is closed.
    """

    __tablename__ = "active_handovers"

    incident_id: Mapped[int] = mapped_column(
        ForeignKey("emergency_incidents.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("incident_timeline_entries.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    successor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class HandoverConfirmation(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only record that a handover successor accepted responsibility.

    Kept in a separate table so the timeline itself stays strictly insert-only.
    """

    __tablename__ = "handover_confirmations"

    handover_entry_id: Mapped[int] = mapped_column(
        ForeignKey("incident_timeline_entries.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("emergency_incidents.id", ondelete="CASCADE"), index=True
    )
    confirmed_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    confirmed_through_seq: Mapped[int] = mapped_column(Integer, nullable=False)


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
