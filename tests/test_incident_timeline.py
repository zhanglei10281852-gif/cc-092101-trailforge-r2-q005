from __future__ import annotations

import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import create_expedition, create_route, create_user
from trailforge.config import Settings
from trailforge.database.migrations import initialize_database
from trailforge.database.session import Database
from trailforge.domain.enums import AuditAction, EmergencyStatus
from trailforge.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedOperationError,
    ValidationError,
)
from trailforge.models.audit import AuditLog, IdempotencyRecord
from trailforge.models.safety import EmergencyIncident, IncidentTimelineEntry
from trailforge.schemas.safety import (
    EmergencyIncidentCreate,
    HandoverConfirm,
    IncidentStatusTransition,
    TimelineEntryCreate,
)
from trailforge.services.safety import SafetyService


@pytest.fixture
def busy_database(tmp_path: Path) -> Generator[Database, None, None]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'busy.db'}",
        sqlite_timeout_seconds=10,
        sqlite_busy_retries=10,
        sqlite_busy_backoff_seconds=0.005,
    )
    database = Database(settings)
    initialize_database(database)
    yield database
    database.engine.dispose()


def _incident(session, *, reporter_email: str = "reporter@example.com") -> tuple[int, int]:
    organizer = create_user(session, email=reporter_email, name="Reporter")
    route = create_route(session, actor_id=organizer)
    expedition = create_expedition(session, organizer_id=organizer, route_id=route)
    incident = SafetyService(session).record_incident(
        EmergencyIncidentCreate(
            expedition_id=expedition,
            reported_by=organizer,
            incident_type="injury",
            risk_level="high",
            occurred_at=datetime.now(UTC),
            description="Climber fell on descent",
            idempotency_key=f"incident-{reporter_email}",
        )
    )
    return organizer, incident.id


def _entry(
    entry_type: str = "observation",
    *,
    actor: int,
    key: str,
    body: str = "note",
    **extra: object,
) -> TimelineEntryCreate:
    return TimelineEntryCreate(
        entry_type=entry_type,
        body=body,
        actor_id=actor,
        idempotency_key=key,
        **extra,
    )


def _handover(
    *, actor: int, receiver: int, through_seq: int, key: str, body: str = "shift change"
) -> TimelineEntryCreate:
    return _entry(
        "handover",
        actor=actor,
        key=key,
        body=body,
        handover_to_user_id=receiver,
        confirm_through_seq=through_seq,
    )


def test_incident_creation_starts_timeline_at_seq_one(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    slice = service.timeline(incident_id)
    assert slice.head_seq == 1
    assert slice.owner_id == organizer
    assert slice.status == EmergencyStatus.OPEN
    assert [item.seq for item in slice.items] == [1]
    assert slice.items[0].entry_type == "observation"
    assert slice.items[0].body == "Climber fell on descent"
    incident = session.get(EmergencyIncident, incident_id)
    assert incident.owner_id == organizer
    assert incident.confirmed_handover_seq == 0


def test_incremental_read_returns_entries_after_cursor(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    for index in range(4):
        service.append_timeline_entry(
            incident_id,
            _entry(actor=organizer, key=f"obs-key-{index}", body=f"update {index}"),
        )
    first_page = service.timeline(incident_id, after_seq=0, limit=3)
    assert [item.seq for item in first_page.items] == [1, 2, 3]
    assert first_page.head_seq == 5
    second_page = service.timeline(incident_id, after_seq=3, limit=3)
    assert [item.seq for item in second_page.items] == [4, 5]
    assert service.timeline(incident_id, after_seq=5).items == []


def test_idempotent_replay_does_not_create_second_entry(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    payload = _entry(actor=organizer, key="duplicate-key", body="same request")
    first = service.append_timeline_entry(incident_id, payload)
    second = service.append_timeline_entry(incident_id, payload)
    assert first.id == second.id
    assert service.timeline(incident_id).head_seq == 2
    with pytest.raises(IdempotencyConflictError):
        service.append_timeline_entry(
            incident_id, _entry(actor=organizer, key="duplicate-key", body="changed request")
        )
    assert service.timeline(incident_id).head_seq == 2


def test_handover_ownership_moves_only_after_receiver_confirms(session) -> None:
    organizer, incident_id = _incident(session)
    receiver = create_user(session, email="receiver@example.com", name="Receiver")
    service = SafetyService(session)
    handover = service.append_timeline_entry(
        incident_id,
        _handover(actor=organizer, receiver=receiver, through_seq=1, key="handover-1"),
    )
    assert service.timeline(incident_id).owner_id == organizer
    pending = service.pending_handovers(receiver)
    assert [item.handover_seq for item in pending] == [handover.seq]
    assert pending[0].confirm_through_seq == 1
    assert pending[0].current_owner_id == organizer
    assert pending[0].handover_from_actor_id == organizer
    assert service.pending_handovers(organizer) == []
    confirmed = service.confirm_handover(
        incident_id,
        handover.seq,
        HandoverConfirm(actor_id=receiver, idempotency_key="confirm-1"),
    )
    assert confirmed.owner_id == receiver
    assert confirmed.confirmed_handover_seq == handover.seq
    assert service.pending_handovers(receiver) == []


def test_handover_confirm_rejects_non_receiver(session) -> None:
    organizer, incident_id = _incident(session)
    receiver = create_user(session, email="receiver2@example.com", name="Receiver")
    outsider = create_user(session, email="outsider@example.com", name="Outsider")
    service = SafetyService(session)
    handover = service.append_timeline_entry(
        incident_id,
        _handover(actor=organizer, receiver=receiver, through_seq=1, key="handover-auth"),
    )
    with pytest.raises(UnauthorizedOperationError):
        service.confirm_handover(
            incident_id,
            handover.seq,
            HandoverConfirm(actor_id=outsider, idempotency_key="confirm-outsider"),
        )
    with pytest.raises(UnauthorizedOperationError):
        service.confirm_handover(
            incident_id,
            handover.seq,
            HandoverConfirm(actor_id=organizer, idempotency_key="confirm-owner"),
        )
    incident = session.get(EmergencyIncident, incident_id)
    assert incident.owner_id == organizer
    assert incident.confirmed_handover_seq == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.HANDOVER_CONFIRMED)
        )
        == 0
    )


def test_only_latest_handover_can_be_confirmed(session) -> None:
    organizer, incident_id = _incident(session)
    second = create_user(session, email="second@example.com", name="Second")
    third = create_user(session, email="third@example.com", name="Third")
    service = SafetyService(session)
    first = service.append_timeline_entry(
        incident_id, _handover(actor=organizer, receiver=second, through_seq=1, key="handover-one")
    )
    newer = service.append_timeline_entry(
        incident_id, _handover(actor=organizer, receiver=third, through_seq=2, key="handover-two")
    )
    with pytest.raises(ConflictError, match="latest unconfirmed handover"):
        service.confirm_handover(
            incident_id,
            first.seq,
            HandoverConfirm(actor_id=second, idempotency_key="confirm-stale"),
        )
    confirmed = service.confirm_handover(
        incident_id,
        newer.seq,
        HandoverConfirm(actor_id=third, idempotency_key="confirm-newer"),
    )
    assert confirmed.owner_id == third
    with pytest.raises(ConflictError, match="latest unconfirmed handover"):
        service.confirm_handover(
            incident_id,
            newer.seq,
            HandoverConfirm(actor_id=third, idempotency_key="confirm-again"),
        )
    replay = service.confirm_handover(
        incident_id,
        newer.seq,
        HandoverConfirm(actor_id=third, idempotency_key="confirm-newer"),
    )
    assert replay.owner_id == third


def test_handover_requires_existing_receiver_and_known_seq(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    with pytest.raises(ValidationError, match="confirm_through_seq"):
        service.append_timeline_entry(
            incident_id,
            _handover(actor=organizer, receiver=organizer, through_seq=99, key="bad-seq-key"),
        )
    with pytest.raises(NotFoundError):
        service.append_timeline_entry(
            incident_id,
            _handover(actor=organizer, receiver=99999, through_seq=1, key="bad-user"),
        )
    assert service.timeline(incident_id).head_seq == 1


def test_terminal_transition_requires_final_action_entry(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    with pytest.raises(ValidationError, match="final_action_seq"):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(target_status="resolved", actor_id=organizer),
        )
    with pytest.raises(ValidationError, match="action entry"):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(
                target_status="resolved", actor_id=organizer, final_action_seq=1
            ),
        )
    with pytest.raises(ValidationError, match="existing timeline entry"):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(
                target_status="false_alarm", actor_id=organizer, final_action_seq=99
            ),
        )
    assert session.get(EmergencyIncident, incident_id).status == EmergencyStatus.OPEN
    action = service.append_timeline_entry(
        incident_id,
        _entry("action", actor=organizer, key="real-action", body="Checked vitals, all normal"),
    )
    closed = service.transition_incident_status(
        incident_id,
        IncidentStatusTransition(
            target_status="false_alarm", actor_id=organizer, final_action_seq=action.seq
        ),
    )
    assert closed.status == "false_alarm"
    assert closed.resolution == "Checked vitals, all normal"
    assert closed.resolved_at is not None


def test_status_transitions_follow_explicit_rules(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    with pytest.raises(InvalidStateError):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(target_status="open", actor_id=organizer),
        )
    with pytest.raises(ValidationError, match="only allowed when closing"):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(
                target_status="monitoring", actor_id=organizer, final_action_seq=1
            ),
        )
    monitoring = service.transition_incident_status(
        incident_id,
        IncidentStatusTransition(target_status="monitoring", actor_id=organizer),
    )
    assert monitoring.status == "monitoring"
    with pytest.raises(InvalidStateError):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(target_status="open", actor_id=organizer),
        )


def test_status_suggestions_are_validated_and_linkable(session) -> None:
    organizer, incident_id = _incident(session)
    service = SafetyService(session)
    with pytest.raises(ValidationError, match="not reachable"):
        service.append_timeline_entry(
            incident_id,
            _entry(
                "status_suggestion",
                actor=organizer,
                key="suggest-bad",
                suggested_status="open",
            ),
        )
    suggestion = service.append_timeline_entry(
        incident_id,
        _entry(
            "status_suggestion",
            actor=organizer,
            key="suggest-ok",
            body="Vitals stable",
            suggested_status="monitoring",
        ),
    )
    resolved_suggestion = service.append_timeline_entry(
        incident_id,
        _entry(
            "status_suggestion",
            actor=organizer,
            key="suggest-resolved",
            body="Ready to walk out",
            suggested_status="resolved",
        ),
    )
    with pytest.raises(ValidationError, match="does not match"):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(
                target_status="monitoring",
                actor_id=organizer,
                suggestion_seq=resolved_suggestion.seq,
            ),
        )
    moved = service.transition_incident_status(
        incident_id,
        IncidentStatusTransition(
            target_status="monitoring",
            actor_id=organizer,
            suggestion_seq=suggestion.seq,
        ),
    )
    assert moved.status == "monitoring"


def test_closed_incident_rejects_append_confirm_and_transition(session) -> None:
    organizer, incident_id = _incident(session)
    receiver = create_user(session, email="close-receiver@example.com", name="Receiver")
    service = SafetyService(session)
    handover = service.append_timeline_entry(
        incident_id,
        _handover(actor=organizer, receiver=receiver, through_seq=1, key="ho-close"),
    )
    action = service.append_timeline_entry(
        incident_id,
        _entry("action", actor=organizer, key="final-action", body="Evacuated to trailhead"),
    )
    closed = service.transition_incident_status(
        incident_id,
        IncidentStatusTransition(
            target_status="resolved", actor_id=organizer, final_action_seq=action.seq
        ),
    )
    assert closed.status == "resolved"
    with pytest.raises(InvalidStateError, match="closed"):
        service.append_timeline_entry(
            incident_id, _entry(actor=organizer, key="after-close", body="too late")
        )
    with pytest.raises(InvalidStateError, match="closed"):
        service.confirm_handover(
            incident_id,
            handover.seq,
            HandoverConfirm(actor_id=receiver, idempotency_key="confirm-after-close"),
        )
    with pytest.raises(InvalidStateError):
        service.transition_incident_status(
            incident_id,
            IncidentStatusTransition(target_status="monitoring", actor_id=organizer),
        )
    assert service.pending_handovers(receiver) == []


def test_timeline_entries_cannot_be_modified_or_deleted(database) -> None:
    with database.session() as session:
        _, incident_id = _incident(session)
    with pytest.raises(IntegrityError, match="append-only"), database.session() as session:
        session.execute(
            text("UPDATE incident_timeline_entries SET body = 'tampered' "
                 "WHERE incident_id = :id"),
            {"id": incident_id},
        )
    with pytest.raises(IntegrityError, match="append-only"), database.session() as session:
        session.execute(
            text("DELETE FROM incident_timeline_entries WHERE incident_id = :id"),
            {"id": incident_id},
        )
    with database.session() as session:
        bodies = session.scalars(
            select(IncidentTimelineEntry.body).where(
                IncidentTimelineEntry.incident_id == incident_id
            )
        ).all()
        assert bodies == ["Climber fell on descent"]


def test_rolled_back_append_leaves_no_trace_and_no_seq_gap(database) -> None:
    with database.session() as session:
        organizer, incident_id = _incident(session)
    with pytest.raises(RuntimeError, match="simulated"), database.session() as session:
        SafetyService(session).append_timeline_entry(
            incident_id,
            _entry(actor=organizer, key="rolled-back", body="lost update"),
        )
        raise RuntimeError("simulated failure")
    with database.session() as session:
        service = SafetyService(session)
        slice = service.timeline(incident_id)
        assert slice.head_seq == 1
        assert [item.seq for item in slice.items] == [1]
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.TIMELINE_ENTRY_APPENDED)
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
    with database.session() as session:
        entry = SafetyService(session).append_timeline_entry(
            incident_id,
            _entry(actor=organizer, key="after-rollback", body="committed"),
        )
        assert entry.seq == 2


def test_concurrent_appends_keep_seq_continuous_and_unique(busy_database) -> None:
    with busy_database.session() as session:
        organizer, incident_id = _incident(session)

    def append(index: int) -> int:
        return busy_database.run_write(
            lambda session: SafetyService(session).append_timeline_entry(
                incident_id,
                _entry(actor=organizer, key=f"concurrent-{index}", body=f"note {index}"),
            ).seq
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        seqs = list(pool.map(append, range(20)))
    assert sorted(seqs) == list(range(2, 22))
    with busy_database.session() as session:
        service = SafetyService(session)
        slice = service.timeline(incident_id, limit=100)
        assert [item.seq for item in slice.items] == list(range(1, 22))
        assert slice.head_seq == 21
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.TIMELINE_ENTRY_APPENDED)
            )
            == 20
        )


def test_concurrent_duplicate_key_creates_single_entry(busy_database) -> None:
    with busy_database.session() as session:
        organizer, incident_id = _incident(session)
    payload = _entry(actor=organizer, key="shared-key", body="same request")
    outcomes: list[str] = []

    def append() -> None:
        try:
            busy_database.run_write(
                lambda session: SafetyService(session).append_timeline_entry(
                    incident_id, payload
                )
            )
            outcomes.append("ok")
        except IntegrityError:
            outcomes.append("conflict")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: append(), range(2)))
    assert "ok" in outcomes
    with busy_database.session() as session:
        slice = SafetyService(session).timeline(incident_id)
        assert [item.seq for item in slice.items] == [1, 2]
        assert slice.head_seq == 2


def test_close_and_confirm_race_settles_in_one_order(busy_database) -> None:
    with busy_database.session() as session:
        organizer, incident_id = _incident(session)
        receiver = create_user(session, email="race-receiver@example.com", name="Receiver")
        service = SafetyService(session)
        action = service.append_timeline_entry(
            incident_id,
            _entry("action", actor=organizer, key="race-action", body="Treated and stable"),
        )
        handover = service.append_timeline_entry(
            incident_id,
            _handover(
                actor=organizer,
                receiver=receiver,
                through_seq=action.seq,
                key="race-handover",
            ),
        )
        action_seq = action.seq
        handover_seq = handover.seq

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def close() -> None:
        barrier.wait()
        try:
            busy_database.run_write(
                lambda session: SafetyService(session).transition_incident_status(
                    incident_id,
                    IncidentStatusTransition(
                        target_status="resolved",
                        actor_id=organizer,
                        final_action_seq=action_seq,
                    ),
                )
            )
            outcomes["close"] = "ok"
        except (InvalidStateError, ConflictError):
            outcomes["close"] = "rejected"

    def confirm() -> None:
        barrier.wait()
        try:
            busy_database.run_write(
                lambda session: SafetyService(session).confirm_handover(
                    incident_id,
                    handover_seq,
                    HandoverConfirm(actor_id=receiver, idempotency_key="race-confirm"),
                )
            )
            outcomes["confirm"] = "ok"
        except (InvalidStateError, ConflictError):
            outcomes["confirm"] = "rejected"

    threads = [threading.Thread(target=close), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes["close"] == "ok"
    assert outcomes["confirm"] in {"ok", "rejected"}
    with busy_database.session() as session:
        incident = session.get(EmergencyIncident, incident_id)
        assert incident.status == EmergencyStatus.RESOLVED
        if outcomes["confirm"] == "ok":
            assert incident.owner_id == receiver
            assert incident.confirmed_handover_seq == handover_seq
        else:
            assert incident.owner_id == organizer
            assert incident.confirmed_handover_seq == 0


def test_timeline_continues_after_application_restart(settings) -> None:
    first = Database(settings)
    initialize_database(first)
    with first.session() as session:
        organizer, incident_id = _incident(session)
        service = SafetyService(session)
        service.append_timeline_entry(
            incident_id, _entry(actor=organizer, key="pre-restart-1", body="before restart")
        )
        service.append_timeline_entry(
            incident_id,
            _entry(actor=organizer, key="pre-restart-2", body="also before restart"),
        )
    first.engine.dispose()

    second = Database(settings)
    initialize_database(second)
    with second.session() as session:
        service = SafetyService(session)
        replay = service.append_timeline_entry(
            incident_id,
            _entry(actor=organizer, key="pre-restart-2", body="also before restart"),
        )
        assert replay.seq == 3
        entry = service.append_timeline_entry(
            incident_id, _entry(actor=organizer, key="post-restart", body="after restart")
        )
        assert entry.seq == 4
        slice = service.timeline(incident_id, limit=100)
        assert [item.seq for item in slice.items] == [1, 2, 3, 4]
        assert slice.head_seq == 4
    second.engine.dispose()


def test_audit_trail_records_operator_states_and_seq_refs(session) -> None:
    organizer, incident_id = _incident(session)
    receiver = create_user(session, email="audit-receiver@example.com", name="Receiver")
    service = SafetyService(session)
    action = service.append_timeline_entry(
        incident_id,
        _entry("action", actor=organizer, key="audit-action", body="Splint applied"),
    )
    handover = service.append_timeline_entry(
        incident_id,
        _handover(
            actor=organizer, receiver=receiver, through_seq=action.seq, key="audit-handover"
        ),
    )
    service.confirm_handover(
        incident_id,
        handover.seq,
        HandoverConfirm(actor_id=receiver, idempotency_key="audit-confirm"),
    )
    service.transition_incident_status(
        incident_id,
        IncidentStatusTransition(
            target_status="resolved", actor_id=receiver, final_action_seq=action.seq
        ),
    )

    logs = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "emergency_incident",
            AuditLog.entity_id == incident_id,
        )
        .order_by(AuditLog.id)
    ).all()
    by_action: dict[str, list[AuditLog]] = {}
    for log in logs:
        by_action.setdefault(log.action, []).append(log)

    recorded = by_action[AuditAction.EMERGENCY_RECORDED][0]
    assert recorded.actor_id == organizer
    assert recorded.context["timeline_seq"] == 1

    appends = by_action[AuditAction.TIMELINE_ENTRY_APPENDED]
    assert [log.after_state["seq"] for log in appends] == [action.seq, handover.seq]
    assert appends[0].after_state["entry_type"] == "action"
    assert appends[1].after_state["handover_to_user_id"] == receiver
    assert appends[1].after_state["confirm_through_seq"] == action.seq

    confirmed = by_action[AuditAction.HANDOVER_CONFIRMED][0]
    assert confirmed.actor_id == receiver
    assert confirmed.before_state["owner_id"] == organizer
    assert confirmed.after_state["owner_id"] == receiver
    assert confirmed.context["handover_seq"] == handover.seq
    assert confirmed.context["confirm_through_seq"] == action.seq

    closed = by_action[AuditAction.STATUS_CHANGED][0]
    assert closed.actor_id == receiver
    assert closed.before_state["status"] == "open"
    assert closed.after_state["status"] == "resolved"
    assert closed.context["final_action_seq"] == action.seq


def test_api_incident_timeline_endpoints(client) -> None:
    database = client.app.state.database
    with database.session() as session:
        organizer = create_user(session, email="api-org@example.com", name="Org")
        receiver = create_user(session, email="api-recv@example.com", name="Recv")
        route = create_route(session, actor_id=organizer)
        expedition = create_expedition(session, organizer_id=organizer, route_id=route)

    created = client.post(
        "/api/v1/safety/incidents",
        json={
            "expedition_id": expedition,
            "reported_by": organizer,
            "incident_type": "weather",
            "risk_level": "high",
            "occurred_at": datetime.now(UTC).isoformat(),
            "description": "Storm approaching",
            "idempotency_key": "api-incident-1",
        },
    )
    assert created.status_code == 201, created.text
    incident = created.json()
    assert incident["owner_id"] == organizer
    assert incident["timeline_head_seq"] == 1
    incident_id = incident["id"]

    appended = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline",
        json={
            "entry_type": "observation",
            "body": "Wind increasing",
            "actor_id": organizer,
            "idempotency_key": "api-obs-1",
        },
    )
    assert appended.status_code == 201
    assert appended.json()["seq"] == 2

    handover = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline",
        json={
            "entry_type": "handover",
            "body": "night shift handover",
            "actor_id": organizer,
            "handover_to_user_id": receiver,
            "confirm_through_seq": 2,
            "idempotency_key": "api-ho-1",
        },
    )
    assert handover.status_code == 201, handover.text
    handover_seq = handover.json()["seq"]

    pending = client.get("/api/v1/safety/handovers/pending", params={"user_id": receiver})
    assert pending.status_code == 200
    assert [item["handover_seq"] for item in pending.json()] == [handover_seq]

    forbidden = client.post(
        f"/api/v1/safety/incidents/{incident_id}/handovers/{handover_seq}/confirm",
        json={"actor_id": organizer, "idempotency_key": "api-confirm-no"},
    )
    assert forbidden.status_code == 403

    confirmed = client.post(
        f"/api/v1/safety/incidents/{incident_id}/handovers/{handover_seq}/confirm",
        json={"actor_id": receiver, "idempotency_key": "api-confirm-yes"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["owner_id"] == receiver
    assert (
        client.get("/api/v1/safety/handovers/pending", params={"user_id": receiver}).json()
        == []
    )

    slice_response = client.get(
        f"/api/v1/safety/incidents/{incident_id}/timeline", params={"after_seq": 2}
    )
    assert slice_response.status_code == 200
    assert [item["seq"] for item in slice_response.json()["items"]] == [3]
    assert slice_response.json()["head_seq"] == 3

    unclosed = client.post(
        f"/api/v1/safety/incidents/{incident_id}/status",
        json={"target_status": "resolved", "actor_id": receiver},
    )
    assert unclosed.status_code == 422

    action = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline",
        json={
            "entry_type": "action",
            "body": "Group sheltered in hut",
            "actor_id": receiver,
            "idempotency_key": "api-action-1",
        },
    )
    closed = client.post(
        f"/api/v1/safety/incidents/{incident_id}/status",
        json={
            "target_status": "resolved",
            "actor_id": receiver,
            "final_action_seq": action.json()["seq"],
        },
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "resolved"
    assert closed.json()["resolution"] == "Group sheltered in hut"

    after_close = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline",
        json={
            "entry_type": "observation",
            "body": "too late",
            "actor_id": receiver,
            "idempotency_key": "api-after-close",
        },
    )
    assert after_close.status_code == 409
