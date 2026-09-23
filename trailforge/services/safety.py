from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    EMERGENCY_TRANSITIONS,
    TERMINAL_EMERGENCY_STATUSES,
    ActivityStatus,
    AuditAction,
    EmergencyStatus,
    RiskLevel,
    TimelineEntryType,
)
from trailforge.errors import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedOperationError,
    ValidationError,
)
from trailforge.models.safety import (
    EmergencyIncident,
    IncidentTimelineEntry,
    ItineraryCheckIn,
    RiskAssessment,
    WeatherSnapshot,
)
from trailforge.repositories.activities import ExpeditionRepository
from trailforge.repositories.base import apply_version
from trailforge.repositories.safety import SafetyRepository
from trailforge.repositories.users import UserRepository
from trailforge.schemas.safety import (
    CheckInResponse,
    CheckInScheduleCreate,
    CheckInSubmit,
    EmergencyIncidentCreate,
    EmergencyIncidentResponse,
    HandoverConfirm,
    IncidentStatusTransition,
    IncidentTimelineSlice,
    OverdueCheckIn,
    PendingHandover,
    RiskAssessmentCreate,
    RiskAssessmentResponse,
    SafetySummary,
    TimelineEntryCreate,
    TimelineEntryResponse,
    WeatherSnapshotCreate,
    WeatherSnapshotResponse,
)
from trailforge.services.base import ServiceBase


class SafetyService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.safety = SafetyRepository(session)
        self.expeditions = ExpeditionRepository(session)
        self.users = UserRepository(session)

    def schedule_check_in(
        self, expedition_id: int, data: CheckInScheduleCreate, *, actor_id: int
    ) -> CheckInResponse:
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        self.users.require(data.user_id)
        registration = self.expeditions.get_registration(expedition_id, data.user_id)
        if registration is None or str(registration.status) not in {"confirmed", "pending"}:
            raise ValidationError("check-in can only be scheduled for an active participant")
        lower_bound = min(expedition.meeting_at, expedition.start_at)
        upper_bound = expedition.end_at
        if data.due_at < lower_bound or data.due_at > upper_bound:
            raise ValidationError("check-in due time must fall within the expedition window")
        check_in = ItineraryCheckIn(expedition_id=expedition_id, **data.model_dump())
        try:
            with self.session.begin_nested():
                self.session.add(check_in)
                self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("the same check-in slot already exists") from exc
        self.audit(
            actor_id=actor_id,
            entity_type="itinerary_check_in",
            entity_id=check_in.id,
            action=AuditAction.CREATED,
            after=self.snapshot(check_in),
        )
        return CheckInResponse.model_validate(check_in)

    def submit_check_in(self, check_in_id: int, data: CheckInSubmit) -> CheckInResponse:
        scope = f"safety:check-in:{check_in_id}:submit"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            check_in = self.safety.get_check_in(check_in_id)
            if check_in is None:
                raise ConflictError("idempotency record references missing check-in")
            return CheckInResponse.model_validate(check_in)
        check_in = self.safety.get_check_in(check_in_id, for_update=True)
        if check_in is None:
            raise NotFoundError(f"ItineraryCheckIn {check_in_id} was not found")
        if check_in.checked_in_at is not None:
            raise ConflictError("check-in has already been submitted")
        delta_seconds = (data.checked_in_at - check_in.due_at).total_seconds()
        check_in.checked_in_at = data.checked_in_at
        check_in.latitude = data.latitude
        check_in.longitude = data.longitude
        check_in.note = data.note
        check_in.is_safe = data.is_safe
        check_in.late_minutes = max(int(delta_seconds // 60), 0)
        self.session.flush()
        response = CheckInResponse.model_validate(check_in)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="itinerary_check_in",
            resource_id=check_in.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=check_in.user_id,
            entity_type="itinerary_check_in",
            entity_id=check_in.id,
            action=AuditAction.CHECKED_IN,
            after={
                "checked_in_at": check_in.checked_in_at,
                "is_safe": check_in.is_safe,
                "late_minutes": check_in.late_minutes,
            },
            correlation_id=data.idempotency_key,
        )
        return response

    def overdue(self, *, now: datetime | None = None) -> list[OverdueCheckIn]:
        current = now or utc_now()
        results: list[OverdueCheckIn] = []
        for check_in in self.safety.overdue_check_ins(current):
            expedition = self.expeditions.get(check_in.expedition_id)
            user = self.users.get(check_in.user_id)
            if expedition is None or user is None:
                continue
            overdue_minutes = max(int((current - check_in.due_at).total_seconds() // 60), 0)
            risk_level = self._overdue_risk(overdue_minutes)
            results.append(
                OverdueCheckIn(
                    check_in_id=check_in.id,
                    expedition_id=expedition.id,
                    expedition_name=expedition.name,
                    user_id=user.id,
                    display_name=user.display_name,
                    check_in_type=check_in.check_in_type,
                    due_at=check_in.due_at,
                    overdue_minutes=overdue_minutes,
                    risk_level=risk_level,
                )
            )
        return results

    def record_incident(self, data: EmergencyIncidentCreate) -> EmergencyIncidentResponse:
        scope = f"safety:expedition:{data.expedition_id}:incident"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            incident = self.safety.get_incident(prior.resource_id)
            if incident is None:
                raise ConflictError("idempotency record references missing incident")
            return EmergencyIncidentResponse.model_validate(incident)
        expedition = self.expeditions.get(data.expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {data.expedition_id} was not found")
        self.users.require(data.reported_by)
        if expedition.status in {ActivityStatus.COMPLETED, ActivityStatus.CANCELLED}:
            raise InvalidStateError("cannot open an incident for a closed expedition")
        incident_data = data.model_dump(exclude={"idempotency_key"})
        incident = EmergencyIncident(**incident_data, owner_id=data.reported_by)
        self.session.add(incident)
        self.session.flush()
        entry = self._append_entry(
            incident,
            entry_type=TimelineEntryType.OBSERVATION,
            body=data.description,
            actor_id=data.reported_by,
        )
        response = EmergencyIncidentResponse.model_validate(incident)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="emergency_incident",
            resource_id=incident.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.reported_by,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.EMERGENCY_RECORDED,
            after=self.snapshot(incident),
            context={"timeline_seq": entry.seq},
            correlation_id=data.idempotency_key,
        )
        return response

    def append_timeline_entry(
        self, incident_id: int, data: TimelineEntryCreate
    ) -> TimelineEntryResponse:
        scope = f"safety:incident:{incident_id}:timeline"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            entry = self.safety.get_timeline_entry_by_id(prior.resource_id)
            if entry is None:
                raise ConflictError("idempotency record references missing timeline entry")
            return TimelineEntryResponse.model_validate(entry)
        incident = self.safety.get_incident(incident_id)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        if EmergencyStatus(incident.status) in TERMINAL_EMERGENCY_STATUSES:
            raise InvalidStateError("closed incidents cannot accept new timeline entries")
        self.users.require(data.actor_id)
        if data.entry_type is TimelineEntryType.HANDOVER:
            self.users.require(data.handover_to_user_id)
            if data.confirm_through_seq > incident.timeline_head_seq:
                raise ValidationError(
                    "confirm_through_seq cannot exceed the current timeline head",
                    context={
                        "confirm_through_seq": data.confirm_through_seq,
                        "head_seq": incident.timeline_head_seq,
                    },
                )
        if data.entry_type is TimelineEntryType.STATUS_SUGGESTION:
            current = EmergencyStatus(incident.status)
            allowed = EMERGENCY_TRANSITIONS[current]
            if data.suggested_status not in allowed:
                raise ValidationError(
                    "suggested status is not reachable from the current status",
                    context={
                        "current": current.value,
                        "suggested": data.suggested_status,
                        "allowed": sorted(allowed),
                    },
                )
        entry = self._append_entry(
            incident,
            entry_type=data.entry_type,
            body=data.body,
            actor_id=data.actor_id,
            suggested_status=data.suggested_status,
            handover_to_user_id=data.handover_to_user_id,
            confirm_through_seq=data.confirm_through_seq,
        )
        response = TimelineEntryResponse.model_validate(entry)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="incident_timeline_entry",
            resource_id=entry.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.actor_id,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.TIMELINE_ENTRY_APPENDED,
            after={
                "seq": entry.seq,
                "entry_type": entry.entry_type,
                "body": entry.body,
                "suggested_status": entry.suggested_status,
                "handover_to_user_id": entry.handover_to_user_id,
                "confirm_through_seq": entry.confirm_through_seq,
            },
            context={"incident_status": EmergencyStatus(incident.status).value},
            correlation_id=data.idempotency_key,
        )
        return response

    def transition_incident_status(
        self, incident_id: int, data: IncidentStatusTransition
    ) -> EmergencyIncidentResponse:
        incident = self.safety.get_incident(incident_id)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        self.users.require(data.actor_id)
        self.safety.lock_incident(incident.id)
        self.session.expire(incident)
        current = EmergencyStatus(incident.status)
        target = EmergencyStatus(data.target_status)
        allowed = EMERGENCY_TRANSITIONS[current]
        if target not in allowed:
            raise InvalidStateError(
                "incident status transition is not allowed",
                context={
                    "current": current.value,
                    "target": target.value,
                    "allowed": sorted(allowed),
                },
            )
        apply_version(incident, data.expected_version)
        final_entry: IncidentTimelineEntry | None = None
        if target in TERMINAL_EMERGENCY_STATUSES:
            if data.final_action_seq is None:
                raise ValidationError(
                    "closing an incident requires final_action_seq "
                    "referencing a final action entry"
                )
            final_entry = self.safety.get_timeline_entry(incident.id, data.final_action_seq)
            if final_entry is None:
                raise ValidationError(
                    "final_action_seq does not reference an existing timeline entry",
                    context={"final_action_seq": data.final_action_seq},
                )
            if final_entry.entry_type != TimelineEntryType.ACTION:
                raise ValidationError("final_action_seq must reference an action entry")
        elif data.final_action_seq is not None:
            raise ValidationError("final_action_seq is only allowed when closing an incident")
        if data.suggestion_seq is not None:
            suggestion = self.safety.get_timeline_entry(incident.id, data.suggestion_seq)
            if suggestion is None or suggestion.entry_type != TimelineEntryType.STATUS_SUGGESTION:
                raise ValidationError("suggestion_seq must reference a status suggestion entry")
            if EmergencyStatus(suggestion.suggested_status) is not target:
                raise ValidationError(
                    "suggestion does not match the target status",
                    context={
                        "suggested": suggestion.suggested_status,
                        "target": target.value,
                    },
                )
        before = self.snapshot(incident, "status", "resolution", "resolved_at", "owner_id")
        incident.status = target
        if final_entry is not None:
            incident.resolution = final_entry.body
            incident.resolved_at = utc_now()
        self.session.flush()
        self.audit(
            actor_id=data.actor_id,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.STATUS_CHANGED,
            before=before,
            after=self.snapshot(incident, "status", "resolution", "resolved_at", "owner_id"),
            context={
                "final_action_seq": data.final_action_seq,
                "suggestion_seq": data.suggestion_seq,
                "reason": data.reason,
            },
        )
        return EmergencyIncidentResponse.model_validate(incident)

    def confirm_handover(
        self, incident_id: int, handover_seq: int, data: HandoverConfirm
    ) -> EmergencyIncidentResponse:
        scope = f"safety:incident:{incident_id}:handover:{handover_seq}:confirm"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            incident = self.safety.get_incident(prior.resource_id)
            if incident is None:
                raise ConflictError("idempotency record references missing incident")
            return EmergencyIncidentResponse.model_validate(incident)
        incident = self.safety.get_incident(incident_id)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        self.safety.lock_incident(incident.id)
        self.session.expire(incident)
        if EmergencyStatus(incident.status) in TERMINAL_EMERGENCY_STATUSES:
            raise InvalidStateError("closed incidents cannot confirm handovers")
        entry = self.safety.get_timeline_entry(incident.id, handover_seq)
        if entry is None:
            raise NotFoundError(
                f"timeline entry {handover_seq} was not found on incident {incident_id}"
            )
        if entry.entry_type != TimelineEntryType.HANDOVER:
            raise ValidationError("only handover entries can be confirmed")
        latest = self.safety.latest_handover(incident.id)
        if (
            latest is None
            or entry.seq != latest.seq
            or entry.seq <= incident.confirmed_handover_seq
        ):
            raise ConflictError(
                "only the latest unconfirmed handover can be confirmed",
                context={
                    "handover_seq": handover_seq,
                    "latest_handover_seq": latest.seq if latest else None,
                    "confirmed_handover_seq": incident.confirmed_handover_seq,
                },
            )
        if data.actor_id != entry.handover_to_user_id:
            raise UnauthorizedOperationError(
                "only the designated receiver can confirm the handover"
            )
        before = self.snapshot(incident, "owner_id", "confirmed_handover_seq", "status")
        incident.owner_id = entry.handover_to_user_id
        incident.confirmed_handover_seq = entry.seq
        apply_version(incident, None)
        self.session.flush()
        response = EmergencyIncidentResponse.model_validate(incident)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="emergency_incident",
            resource_id=incident.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.actor_id,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.HANDOVER_CONFIRMED,
            before=before,
            after=self.snapshot(incident, "owner_id", "confirmed_handover_seq", "status"),
            context={
                "handover_seq": entry.seq,
                "confirm_through_seq": entry.confirm_through_seq,
            },
            correlation_id=data.idempotency_key,
        )
        return response

    def timeline(
        self, incident_id: int, *, after_seq: int = 0, limit: int | None = None
    ) -> IncidentTimelineSlice:
        incident = self.safety.get_incident(incident_id)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        entries = self.safety.timeline_entries(incident_id, after_seq=after_seq, limit=limit)
        return IncidentTimelineSlice(
            incident_id=incident.id,
            status=EmergencyStatus(incident.status),
            owner_id=incident.owner_id,
            head_seq=incident.timeline_head_seq,
            items=[TimelineEntryResponse.model_validate(entry) for entry in entries],
        )

    def pending_handovers(self, user_id: int) -> list[PendingHandover]:
        self.users.require(user_id)
        results: list[PendingHandover] = []
        for entry, incident in self.safety.pending_handovers(user_id):
            results.append(
                PendingHandover(
                    incident_id=incident.id,
                    expedition_id=incident.expedition_id,
                    handover_seq=entry.seq,
                    confirm_through_seq=entry.confirm_through_seq,
                    handover_from_actor_id=entry.actor_id,
                    current_owner_id=incident.owner_id,
                    requested_at=entry.created_at,
                )
            )
        return results

    def _append_entry(
        self,
        incident: EmergencyIncident,
        *,
        entry_type: TimelineEntryType,
        body: str,
        actor_id: int,
        suggested_status: EmergencyStatus | None = None,
        handover_to_user_id: int | None = None,
        confirm_through_seq: int | None = None,
    ) -> IncidentTimelineEntry:
        seq = self.safety.allocate_timeline_seq(incident.id)
        self.session.expire(incident, ["timeline_head_seq"])
        entry = IncidentTimelineEntry(
            incident_id=incident.id,
            seq=seq,
            entry_type=entry_type,
            body=body,
            actor_id=actor_id,
            suggested_status=suggested_status,
            handover_to_user_id=handover_to_user_id,
            confirm_through_seq=confirm_through_seq,
        )
        return self.safety.add_timeline_entry(entry)

    def assess_risk(self, data: RiskAssessmentCreate) -> RiskAssessmentResponse:
        if self.expeditions.get(data.expedition_id) is None:
            raise NotFoundError(f"Expedition {data.expedition_id} was not found")
        self.users.require(data.assessor_id)
        score = data.likelihood * data.impact
        level = self._score_level(score)
        assessment = RiskAssessment(
            **data.model_dump(),
            score=score,
            risk_level=level,
        )
        self.session.add(assessment)
        self.session.flush()
        self.audit(
            actor_id=data.assessor_id,
            entity_type="risk_assessment",
            entity_id=assessment.id,
            action=AuditAction.RISK_RECORDED,
            after=self.snapshot(assessment),
        )
        return RiskAssessmentResponse.model_validate(assessment)

    def add_weather_snapshot(self, data: WeatherSnapshotCreate) -> WeatherSnapshotResponse:
        if self.expeditions.get(data.expedition_id) is None:
            raise NotFoundError(f"Expedition {data.expedition_id} was not found")
        self.users.require(data.recorded_by)
        snapshot = WeatherSnapshot(**data.model_dump())
        self.session.add(snapshot)
        self.session.flush()
        self.audit(
            actor_id=data.recorded_by,
            entity_type="weather_snapshot",
            entity_id=snapshot.id,
            action=AuditAction.CREATED,
            after=self.snapshot(snapshot),
            context={"offline_snapshot": True, "no_realtime_claim": True},
        )
        return WeatherSnapshotResponse.model_validate(snapshot)

    def summary(self, expedition_id: int, *, now: datetime | None = None) -> SafetySummary:
        if self.expeditions.get(expedition_id) is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        current = now or utc_now()
        check_ins = self.safety.check_ins(expedition_id)
        incidents = self.safety.incidents(expedition_id)
        assessments = self.safety.assessments(expedition_id)
        latest_weather = self.safety.latest_weather(expedition_id)
        overdue_count = sum(
            item.checked_in_at is None and item.due_at < current for item in check_ins
        )
        unsafe_count = sum(item.is_safe is False for item in check_ins)
        open_incidents = [
            item
            for item in incidents
            if item.status in {EmergencyStatus.OPEN, EmergencyStatus.MONITORING}
        ]
        warnings: list[str] = []
        if overdue_count:
            warnings.append(f"{overdue_count} overdue check-in(s)")
        if unsafe_count:
            warnings.append(f"{unsafe_count} unsafe check-in(s)")
        if open_incidents:
            warnings.append(f"{len(open_incidents)} open incident(s)")
        if latest_weather is None:
            warnings.append("no offline weather snapshot recorded")
        risk_rank = {
            RiskLevel.LOW: 1,
            RiskLevel.MODERATE: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }
        highest = (
            max((RiskLevel(item.risk_level) for item in incidents), key=risk_rank.get)
            if incidents
            else None
        )
        return SafetySummary(
            expedition_id=expedition_id,
            generated_at=current,
            scheduled_check_ins=len(check_ins),
            completed_check_ins=sum(item.checked_in_at is not None for item in check_ins),
            overdue_check_ins=overdue_count,
            unsafe_check_ins=unsafe_count,
            open_incidents=len(open_incidents),
            highest_incident_risk=highest,
            assessment_count=len(assessments),
            highest_assessment_score=max((item.score for item in assessments), default=None),
            latest_weather_snapshot=(
                WeatherSnapshotResponse.model_validate(latest_weather) if latest_weather else None
            ),
            warnings=warnings,
        )

    @staticmethod
    def _overdue_risk(minutes: int) -> RiskLevel:
        if minutes < 30:
            return RiskLevel.LOW
        if minutes < 120:
            return RiskLevel.MODERATE
        if minutes < 360:
            return RiskLevel.HIGH
        return RiskLevel.CRITICAL

    @staticmethod
    def _score_level(score: int) -> RiskLevel:
        if score <= 4:
            return RiskLevel.LOW
        if score <= 9:
            return RiskLevel.MODERATE
        if score <= 16:
            return RiskLevel.HIGH
        return RiskLevel.CRITICAL
