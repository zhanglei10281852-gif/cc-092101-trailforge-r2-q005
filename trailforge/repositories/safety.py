from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from trailforge.domain.enums import EmergencyStatus
from trailforge.models.safety import (
    ActiveHandover,
    EmergencyIncident,
    HandoverConfirmation,
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

    def max_timeline_seq(self, incident_id: int) -> int:
        statement = select(func.coalesce(func.max(IncidentTimelineEntry.seq), 0)).where(
            IncidentTimelineEntry.incident_id == incident_id
        )
        return int(self.session.scalar(statement) or 0)

    def get_timeline_entry(self, entry_id: int) -> IncidentTimelineEntry | None:
        return self.session.get(IncidentTimelineEntry, entry_id)

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

    def get_confirmation(self, handover_entry_id: int) -> HandoverConfirmation | None:
        return self.session.scalar(
            select(HandoverConfirmation).where(
                HandoverConfirmation.handover_entry_id == handover_entry_id
            )
        )

    def confirmations_for_entries(
        self, entry_ids: list[int]
    ) -> dict[int, HandoverConfirmation]:
        if not entry_ids:
            return {}
        statement = select(HandoverConfirmation).where(
            HandoverConfirmation.handover_entry_id.in_(entry_ids)
        )
        return {
            confirmation.handover_entry_id: confirmation
            for confirmation in self.session.scalars(statement)
        }

    def latest_confirmed_handover(self, incident_id: int) -> IncidentTimelineEntry | None:
        statement = (
            select(IncidentTimelineEntry)
            .join(
                HandoverConfirmation,
                HandoverConfirmation.handover_entry_id == IncidentTimelineEntry.id,
            )
            .where(IncidentTimelineEntry.incident_id == incident_id)
            .order_by(IncidentTimelineEntry.seq.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def get_active_handover(self, incident_id: int) -> ActiveHandover | None:
        return self.session.get(ActiveHandover, incident_id)

    def pending_handover(self, incident_id: int) -> IncidentTimelineEntry | None:
        active = self.get_active_handover(incident_id)
        if active is None:
            return None
        return self.get_timeline_entry(active.entry_id)

    def pending_handovers_for(self, successor_id: int) -> list[IncidentTimelineEntry]:
        statement = (
            select(IncidentTimelineEntry)
            .join(ActiveHandover, ActiveHandover.entry_id == IncidentTimelineEntry.id)
            .join(
                EmergencyIncident,
                EmergencyIncident.id == IncidentTimelineEntry.incident_id,
            )
            .where(
                ActiveHandover.successor_id == successor_id,
                EmergencyIncident.status.notin_(
                    [EmergencyStatus.RESOLVED.value, EmergencyStatus.FALSE_ALARM.value]
                ),
            )
            .order_by(IncidentTimelineEntry.created_at)
        )
        return list(self.session.scalars(statement))
