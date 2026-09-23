from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from trailforge.domain.enums import EmergencyStatus, TimelineEntryType
from trailforge.models.safety import (
    EmergencyIncident,
    IncidentTimelineEntry,
    ItineraryCheckIn,
    RiskAssessment,
    WeatherSnapshot,
)
from trailforge.repositories.base import BaseRepository


class SafetyRepository(BaseRepository[ItineraryCheckIn]):
    model = ItineraryCheckIn
    sortable = {
        "created_at": ItineraryCheckIn.created_at,
        "due_at": ItineraryCheckIn.due_at,
        "checked_in_at": ItineraryCheckIn.checked_in_at,
    }

    def get_check_in(
        self, check_in_id: int, *, for_update: bool = False
    ) -> ItineraryCheckIn | None:
        statement = select(ItineraryCheckIn).where(ItineraryCheckIn.id == check_in_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def check_ins(self, expedition_id: int) -> list[ItineraryCheckIn]:
        return list(
            self.session.scalars(
                select(ItineraryCheckIn)
                .where(ItineraryCheckIn.expedition_id == expedition_id)
                .order_by(ItineraryCheckIn.due_at)
            )
        )

    def overdue_check_ins(self, now: datetime) -> list[ItineraryCheckIn]:
        statement = (
            select(ItineraryCheckIn)
            .where(
                ItineraryCheckIn.due_at < now,
                ItineraryCheckIn.checked_in_at.is_(None),
            )
            .order_by(ItineraryCheckIn.due_at)
        )
        return list(self.session.scalars(statement))

    def get_incident(
        self, incident_id: int, *, for_update: bool = False
    ) -> EmergencyIncident | None:
        statement = select(EmergencyIncident).where(EmergencyIncident.id == incident_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def lock_incident(self, incident_id: int) -> None:
        """Take the SQLite writer lock on the incident row without changing it.

        Any UPDATE upgrades the connection to a write transaction, so later
        statements in this transaction observe the latest committed state and
        concurrent writers serialize behind this one.
        """
        self.session.execute(
            update(EmergencyIncident)
            .where(EmergencyIncident.id == incident_id)
            .values(confirmed_handover_seq=EmergencyIncident.confirmed_handover_seq)
            .execution_options(synchronize_session=False)
        )

    def allocate_timeline_seq(self, incident_id: int) -> int:
        """Atomically advance the per-incident sequence counter and return it."""
        result = self.session.execute(
            update(EmergencyIncident)
            .where(EmergencyIncident.id == incident_id)
            .values(timeline_head_seq=EmergencyIncident.timeline_head_seq + 1)
            .returning(EmergencyIncident.timeline_head_seq)
            .execution_options(synchronize_session=False)
        )
        return int(result.scalar_one())

    def add_timeline_entry(self, entry: IncidentTimelineEntry) -> IncidentTimelineEntry:
        self.session.add(entry)
        self.session.flush()
        return entry

    def get_timeline_entry_by_id(self, entry_id: int) -> IncidentTimelineEntry | None:
        return self.session.get(IncidentTimelineEntry, entry_id)

    def get_timeline_entry(self, incident_id: int, seq: int) -> IncidentTimelineEntry | None:
        return self.session.scalar(
            select(IncidentTimelineEntry).where(
                IncidentTimelineEntry.incident_id == incident_id,
                IncidentTimelineEntry.seq == seq,
            )
        )

    def timeline_entries(
        self, incident_id: int, *, after_seq: int = 0, limit: int | None = None
    ) -> list[IncidentTimelineEntry]:
        statement = (
            select(IncidentTimelineEntry)
            .where(
                IncidentTimelineEntry.incident_id == incident_id,
                IncidentTimelineEntry.seq > after_seq,
            )
            .order_by(IncidentTimelineEntry.seq)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    def latest_handover(self, incident_id: int) -> IncidentTimelineEntry | None:
        return self.session.scalar(
            select(IncidentTimelineEntry)
            .where(
                IncidentTimelineEntry.incident_id == incident_id,
                IncidentTimelineEntry.entry_type == TimelineEntryType.HANDOVER,
            )
            .order_by(IncidentTimelineEntry.seq.desc())
            .limit(1)
        )

    def pending_handovers(
        self, user_id: int
    ) -> list[tuple[IncidentTimelineEntry, EmergencyIncident]]:
        latest = (
            select(
                IncidentTimelineEntry.incident_id.label("incident_id"),
                func.max(IncidentTimelineEntry.seq).label("max_seq"),
            )
            .where(IncidentTimelineEntry.entry_type == TimelineEntryType.HANDOVER)
            .group_by(IncidentTimelineEntry.incident_id)
            .subquery()
        )
        statement = (
            select(IncidentTimelineEntry, EmergencyIncident)
            .join(
                latest,
                (IncidentTimelineEntry.incident_id == latest.c.incident_id)
                & (IncidentTimelineEntry.seq == latest.c.max_seq),
            )
            .join(EmergencyIncident, EmergencyIncident.id == IncidentTimelineEntry.incident_id)
            .where(
                IncidentTimelineEntry.handover_to_user_id == user_id,
                EmergencyIncident.status.in_(
                    [EmergencyStatus.OPEN, EmergencyStatus.MONITORING]
                ),
                IncidentTimelineEntry.seq > EmergencyIncident.confirmed_handover_seq,
            )
            .order_by(IncidentTimelineEntry.created_at, IncidentTimelineEntry.id)
        )
        return [(entry, incident) for entry, incident in self.session.execute(statement).all()]

    def incidents(
        self,
        expedition_id: int | None = None,
        *,
        status: EmergencyStatus | None = None,
    ) -> list[EmergencyIncident]:
        statement = select(EmergencyIncident)
        if expedition_id is not None:
            statement = statement.where(EmergencyIncident.expedition_id == expedition_id)
        if status is not None:
            statement = statement.where(EmergencyIncident.status == status)
        return list(self.session.scalars(statement.order_by(EmergencyIncident.occurred_at.desc())))

    def assessments(self, expedition_id: int | None = None) -> list[RiskAssessment]:
        statement = select(RiskAssessment)
        if expedition_id is not None:
            statement = statement.where(RiskAssessment.expedition_id == expedition_id)
        return list(self.session.scalars(statement.order_by(RiskAssessment.score.desc())))

    def weather_snapshots(self, expedition_id: int) -> list[WeatherSnapshot]:
        statement = (
            select(WeatherSnapshot)
            .where(WeatherSnapshot.expedition_id == expedition_id)
            .order_by(WeatherSnapshot.observed_at.desc())
        )
        return list(self.session.scalars(statement))

    def latest_weather(self, expedition_id: int) -> WeatherSnapshot | None:
        statement = (
            select(WeatherSnapshot)
            .where(WeatherSnapshot.expedition_id == expedition_id)
            .order_by(WeatherSnapshot.observed_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)
