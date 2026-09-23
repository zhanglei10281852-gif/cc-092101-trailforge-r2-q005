from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    INCIDENT_TRANSITIONS,
    TERMINAL_INCIDENT_STATUSES,
    ActivityStatus,
    AuditAction,
    EmergencyStatus,
    IncidentEntryKind,
    RiskLevel,
)
from trailforge.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedOperationError,
    ValidationError,
)
from trailforge.models.safety import (
    ActiveHandover,
    EmergencyIncident,
    HandoverConfirmation,
    IncidentTimelineEntry,
    ItineraryCheckIn,
    RiskAssessment,
    WeatherSnapshot,
)
from trailforge.repositories.activities import ExpeditionRepository
from trailforge.repositories.audit import IdempotencyRepository
from trailforge.repositories.base import apply_version
from trailforge.repositories.safety import SafetyRepository
from trailforge.repositories.users import UserRepository
from trailforge.schemas.safety import (
    CheckInResponse,
    CheckInScheduleCreate,
    CheckInSubmit,
    EmergencyIncidentCreate,
    EmergencyIncidentResponse,
    HandoverAppend,
    HandoverConfirm,
    HandoverConfirmationResponse,
    HandoverTodoItem,
    IncidentEntryAppend,
    IncidentTimelineEntryResponse,
    IncidentTimelinePage,
    IncidentTransition,
    OverdueCheckIn,
    RiskAssessmentCreate,
    RiskAssessmentResponse,
    SafetySummary,
    StatusSuggestionAppend,
    WeatherSnapshotCreate,
    WeatherSnapshotResponse,
)
from trailforge.services.base import ServiceBase

MAX_SEQ_ATTEMPTS = 5


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
        incident = EmergencyIncident(**incident_data)
        self.session.add(incident)
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
            actor_id=data.reported_by,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.EMERGENCY_RECORDED,
            after=self.snapshot(incident),
            correlation_id=data.idempotency_key,
        )
        return response

    def append_observation(
        self, incident_id: int, data: IncidentEntryAppend
    ) -> IncidentTimelineEntryResponse:
        return self._append_entry(incident_id, data, kind=IncidentEntryKind.OBSERVATION)

    def append_action(
        self, incident_id: int, data: IncidentEntryAppend
    ) -> IncidentTimelineEntryResponse:
        return self._append_entry(incident_id, data, kind=IncidentEntryKind.ACTION)

    def append_status_suggestion(
        self, incident_id: int, data: StatusSuggestionAppend
    ) -> IncidentTimelineEntryResponse:
        incident = self._require_open_incident(incident_id)
        if data.suggested_status == incident.status:
            raise ValidationError("suggested status must differ from the current status")
        return self._append_entry(
            incident_id,
            data,
            kind=IncidentEntryKind.STATUS_SUGGESTION,
            suggested_status=data.suggested_status,
            incident=incident,
        )

    def append_handover(
        self, incident_id: int, data: HandoverAppend
    ) -> IncidentTimelineEntryResponse:
        incident = self._require_open_incident(incident_id)
        owner_id = self._effective_owner_id(incident)
        if data.author_id != owner_id:
            raise UnauthorizedOperationError(
                "only the current responsible owner can hand over the incident"
            )
        self.users.require(data.successor_id)
        if data.successor_id == owner_id:
            raise ValidationError("handover successor must differ from the current owner")
        if self.safety.pending_handover(incident.id) is not None:
            raise ConflictError("a handover is already awaiting confirmation")
        return self._append_entry(
            incident_id,
            data,
            kind=IncidentEntryKind.HANDOVER,
            successor_id=data.successor_id,
            incident=incident,
        )

    def _append_entry(
        self,
        incident_id: int,
        data: IncidentEntryAppend,
        *,
        kind: IncidentEntryKind,
        suggested_status: EmergencyStatus | None = None,
        successor_id: int | None = None,
        incident: EmergencyIncident | None = None,
    ) -> IncidentTimelineEntryResponse:
        scope = f"safety:incident:{incident_id}:timeline:{kind}"
        payload: dict[str, object] = {
            "author_id": data.author_id,
            "content": data.content,
            "suggested_status": str(suggested_status) if suggested_status else None,
            "successor_id": successor_id,
        }
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=payload)
        if prior is not None:
            entry = self.safety.get_timeline_entry(prior.resource_id)
            if entry is None:
                raise ConflictError("idempotency record references missing timeline entry")
            return self._entry_response(entry)
        if incident is None:
            incident = self._require_open_incident(incident_id)
        self._require_timeline_author(incident, data.author_id)
        response: IncidentTimelineEntryResponse | None = None
        for _ in range(MAX_SEQ_ATTEMPTS):
            entry = IncidentTimelineEntry(
                incident_id=incident.id,
                seq=self.safety.max_timeline_seq(incident.id) + 1,
                kind=kind,
                author_id=data.author_id,
                content=data.content,
                suggested_status=suggested_status,
                successor_id=successor_id,
            )
            if kind is IncidentEntryKind.HANDOVER:
                entry.required_through_seq = entry.seq
            try:
                with self.session.begin_nested():
                    self.session.add(entry)
                    self.session.flush()
                    if kind is IncidentEntryKind.HANDOVER:
                        # The active-handover pointer enforces one pending handover
                        # per incident at the database level, even when two
                        # handover requests race past the pre-check above.
                        self.session.add(
                            ActiveHandover(
                                incident_id=incident.id,
                                entry_id=entry.id,
                                successor_id=successor_id,
                            )
                        )
                        self.session.flush()
                    response = self._entry_response(entry)
                    self.save_idempotent(
                        scope=scope,
                        key=data.idempotency_key,
                        payload=payload,
                        resource_type="incident_timeline_entry",
                        resource_id=entry.id,
                        response=response.model_dump(mode="json"),
                    )
                break
            except IntegrityError as exc:
                # The savepoint rollback keeps the outer transaction usable. Figure
                # out which constraint lost the race before deciding how to proceed.
                self.session.expire_all()
                existing = IdempotencyRepository(self.session).get_key(
                    scope, data.idempotency_key
                )
                if existing is not None:
                    # A concurrent retry of the same idempotency key won the race.
                    if existing.request_hash != self.request_hash(payload):
                        raise IdempotencyConflictError(
                            "idempotency key was already used with a different request",
                            context={"scope": scope, "key": data.idempotency_key},
                        ) from exc
                    entry = self.safety.get_timeline_entry(existing.resource_id)
                    if entry is None:
                        raise ConflictError(
                            "idempotency record references missing timeline entry"
                        ) from exc
                    return self._entry_response(entry)
                if (
                    kind is IncidentEntryKind.HANDOVER
                    and self.safety.get_active_handover(incident.id) is not None
                ):
                    raise ConflictError(
                        "a handover is already awaiting confirmation"
                    ) from exc
                continue  # sequence collision: recompute and retry
        if response is None:
            raise ConflictError(
                "could not allocate a timeline sequence; retry the request",
                context={"incident_id": incident_id},
            )
        self.audit(
            actor_id=data.author_id,
            entity_type="incident_timeline_entry",
            entity_id=entry.id,
            action=AuditAction.TIMELINE_APPENDED,
            after=self.snapshot(entry),
            context={
                "incident_id": incident.id,
                "incident_status": str(incident.status),
                "seq": entry.seq,
                "kind": str(entry.kind),
                "successor_id": successor_id,
                "required_through_seq": entry.required_through_seq,
            },
            correlation_id=data.idempotency_key,
        )
        return response

    def confirm_handover(
        self, incident_id: int, entry_id: int, data: HandoverConfirm
    ) -> HandoverConfirmationResponse:
        scope = f"safety:incident:{incident_id}:handover:{entry_id}:confirm"
        payload = {
            "successor_id": data.successor_id,
            "confirm_through_seq": data.confirm_through_seq,
        }
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=payload)
        if prior is not None:
            confirmation = self.session.get(HandoverConfirmation, prior.resource_id)
            if confirmation is None:
                raise ConflictError("idempotency record references missing confirmation")
            return HandoverConfirmationResponse.model_validate(confirmation)
        incident = self.safety.get_incident(incident_id, for_update=True)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        if incident.status in TERMINAL_INCIDENT_STATUSES:
            raise InvalidStateError("closed incidents cannot confirm handovers")
        entry = self.safety.get_timeline_entry(entry_id)
        if (
            entry is None
            or entry.incident_id != incident_id
            or entry.kind != IncidentEntryKind.HANDOVER
        ):
            raise NotFoundError(f"Handover entry {entry_id} was not found")
        if self.safety.get_confirmation(entry.id) is not None:
            raise ConflictError("handover has already been confirmed")
        if entry.successor_id != data.successor_id:
            raise UnauthorizedOperationError(
                "only the designated successor can confirm this handover"
            )
        head_seq = self.safety.max_timeline_seq(incident_id)
        required = entry.required_through_seq or entry.seq
        if data.confirm_through_seq != head_seq or data.confirm_through_seq < required:
            raise ConflictError(
                "confirmation must acknowledge the latest timeline sequence",
                context={
                    "confirm_through_seq": data.confirm_through_seq,
                    "required_through_seq": required,
                    "current_head_seq": head_seq,
                },
            )
        owner_before = self._effective_owner_id(incident)
        confirmation = HandoverConfirmation(
            handover_entry_id=entry.id,
            incident_id=incident_id,
            confirmed_by=data.successor_id,
            confirmed_through_seq=data.confirm_through_seq,
        )
        try:
            with self.session.begin_nested():
                self.session.add(confirmation)
                self.session.flush()
                active = self.safety.get_active_handover(incident_id)
                if active is not None and active.entry_id == entry.id:
                    self.session.delete(active)
                    self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("handover has already been confirmed") from exc
        response = HandoverConfirmationResponse.model_validate(confirmation)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=payload,
            resource_type="handover_confirmation",
            resource_id=confirmation.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.successor_id,
            entity_type="emergency_incident",
            entity_id=incident.id,
            action=AuditAction.HANDOVER_CONFIRMED,
            before={"owner_id": owner_before},
            after={"owner_id": data.successor_id},
            context={
                "handover_entry_id": entry.id,
                "handover_seq": entry.seq,
                "required_through_seq": required,
                "confirmed_through_seq": data.confirm_through_seq,
                "incident_status": str(incident.status),
            },
            correlation_id=data.idempotency_key,
        )
        return response

    def transition_incident(
        self, incident_id: int, data: IncidentTransition
    ) -> EmergencyIncidentResponse:
        scope = f"safety:incident:{incident_id}:transition"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            incident = self.safety.get_incident(prior.resource_id)
            if incident is None:
                raise ConflictError("idempotency record references missing incident")
            return EmergencyIncidentResponse.model_validate(incident)
        incident = self.safety.get_incident(incident_id, for_update=True)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        apply_version(incident, data.expected_version)
        current = EmergencyStatus(incident.status)
        target = data.target_status
        if target not in INCIDENT_TRANSITIONS[current]:
            raise InvalidStateError(
                f"incident status cannot change from {current} to {target}",
                context={"current": str(current), "target": str(target)},
            )
        self._require_transition_actor(incident, data.actor_id)
        final_action_seq: int | None = None
        if target in TERMINAL_INCIDENT_STATUSES:
            if data.final_action_entry_id is None:
                raise ValidationError(
                    "closing an incident requires a final action timeline entry"
                )
            final_entry = self.safety.get_timeline_entry(data.final_action_entry_id)
            if final_entry is None or final_entry.incident_id != incident.id:
                raise ValidationError("final action entry must belong to this incident")
            if final_entry.kind != IncidentEntryKind.ACTION:
                raise ValidationError("final entry must be an action timeline record")
            if data.resolved_at is None:
                raise ValidationError("closed incidents require resolved_at")
            final_action_seq = final_entry.seq
            incident.resolved_at = data.resolved_at
            # Closing voids any unconfirmed handover; the timeline record stays.
            active = self.safety.get_active_handover(incident.id)
            if active is not None:
                self.session.delete(active)
        before = self.snapshot(incident, "status", "resolved_at")
        incident.status = target
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
            action=AuditAction.STATUS_CHANGED,
            before=before,
            after=self.snapshot(incident, "status", "resolved_at"),
            context={
                "final_action_entry_id": data.final_action_entry_id,
                "final_action_seq": final_action_seq,
            },
            correlation_id=data.idempotency_key,
        )
        return response

    def timeline(
        self, incident_id: int, *, after_seq: int = 0, limit: int = 100
    ) -> IncidentTimelinePage:
        if self.safety.get_incident(incident_id) is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        fetched = self.safety.timeline_entries(incident_id, after_seq=after_seq, limit=limit + 1)
        has_more = len(fetched) > limit
        entries = fetched[:limit]
        confirmations = self.safety.confirmations_for_entries(
            [entry.id for entry in entries if entry.kind == IncidentEntryKind.HANDOVER]
        )
        return IncidentTimelinePage(
            incident_id=incident_id,
            entries=[
                self._entry_response(entry, confirmations.get(entry.id)) for entry in entries
            ],
            next_after_seq=entries[-1].seq if entries else after_seq,
            has_more=has_more,
        )

    def handover_todos(self, user_id: int) -> list[HandoverTodoItem]:
        self.users.require(user_id)
        todos: list[HandoverTodoItem] = []
        for entry in self.safety.pending_handovers_for(user_id):
            incident = self.safety.get_incident(entry.incident_id)
            if incident is None:
                continue
            todos.append(
                HandoverTodoItem(
                    incident_id=incident.id,
                    expedition_id=incident.expedition_id,
                    entry_id=entry.id,
                    entry_seq=entry.seq,
                    required_through_seq=entry.required_through_seq or entry.seq,
                    current_head_seq=self.safety.max_timeline_seq(incident.id),
                    from_owner_id=self._effective_owner_id(incident),
                    successor_id=user_id,
                    created_at=entry.created_at,
                )
            )
        return todos

    def _require_open_incident(self, incident_id: int) -> EmergencyIncident:
        incident = self.safety.get_incident(incident_id, for_update=True)
        if incident is None:
            raise NotFoundError(f"EmergencyIncident {incident_id} was not found")
        if incident.status in TERMINAL_INCIDENT_STATUSES:
            raise InvalidStateError("closed incidents cannot be modified")
        return incident

    def _effective_owner_id(self, incident: EmergencyIncident) -> int:
        confirmed = self.safety.latest_confirmed_handover(incident.id)
        return confirmed.successor_id if confirmed is not None else incident.reported_by

    def _require_timeline_author(self, incident: EmergencyIncident, author_id: int) -> None:
        self.users.require(author_id)
        expedition = self.expeditions.get(incident.expedition_id)
        if author_id == self._effective_owner_id(incident):
            return
        if expedition is not None and author_id == expedition.organizer_id:
            return
        registration = self.expeditions.get_registration(incident.expedition_id, author_id)
        if registration is not None and str(registration.status) in {"confirmed", "pending"}:
            return
        raise UnauthorizedOperationError(
            "timeline entries require an active participant or the responsible owner"
        )

    def _require_transition_actor(self, incident: EmergencyIncident, actor_id: int) -> None:
        self.users.require(actor_id)
        if actor_id == self._effective_owner_id(incident):
            return
        expedition = self.expeditions.get(incident.expedition_id)
        if expedition is not None and actor_id == expedition.organizer_id:
            return
        raise UnauthorizedOperationError(
            "status transitions require the responsible owner or the expedition organizer"
        )

    @staticmethod
    def _entry_response(
        entry: IncidentTimelineEntry,
        confirmation: HandoverConfirmation | None = None,
    ) -> IncidentTimelineEntryResponse:
        return IncidentTimelineEntryResponse(
            id=entry.id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            incident_id=entry.incident_id,
            seq=entry.seq,
            kind=IncidentEntryKind(entry.kind),
            author_id=entry.author_id,
            content=entry.content,
            suggested_status=(
                EmergencyStatus(entry.suggested_status) if entry.suggested_status else None
            ),
            successor_id=entry.successor_id,
            required_through_seq=entry.required_through_seq,
            handover_confirmation=(
                HandoverConfirmationResponse.model_validate(confirmation)
                if confirmation is not None
                else None
            ),
        )

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
