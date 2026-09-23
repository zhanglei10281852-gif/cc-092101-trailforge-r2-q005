from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.conftest import create_expedition, create_route, create_user
from trailforge.config import Settings
from trailforge.database.migrations import initialize_database, migration_status
from trailforge.database.session import Database
from trailforge.domain.enums import AuditAction, EmergencyStatus
from trailforge.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedOperationError,
)
from trailforge.models.audit import AuditLog
from trailforge.models.safety import (
    ActiveHandover,
    HandoverConfirmation,
    IncidentTimelineEntry,
)
from trailforge.schemas.safety import (
    EmergencyIncidentCreate,
    HandoverAppend,
    HandoverConfirm,
    IncidentEntryAppend,
    IncidentTransition,
    StatusSuggestionAppend,
)
from trailforge.services.safety import SafetyService

UTC = UTC


def _setup(session) -> tuple[int, int, int, int, int]:
    organizer = create_user(session, email="organizer@example.com", name="Organizer")
    successor = create_user(session, email="relief@example.com", name="Night Relief")
    outsider = create_user(session, email="outsider@example.com", name="Outsider")
    route = create_route(session, actor_id=organizer, name="Night Ridge")
    expedition = create_expedition(session, organizer_id=organizer, route_id=route)
    return organizer, successor, outsider, route, expedition


def _open_incident(
    service: SafetyService,
    expedition: int,
    reporter: int,
    *,
    key: str = "incident-key-1",
    description: str = "Stranded hiker on the north ridge",
):
    return service.record_incident(
        EmergencyIncidentCreate(
            expedition_id=expedition,
            reported_by=reporter,
            incident_type="lost_person",
            risk_level="high",
            occurred_at=datetime.now(UTC),
            description=description,
            idempotency_key=key,
        )
    )


# ---------------------------------------------------------------------------
# Timeline basics: four entry kinds, per-incident contiguous numbering
# ---------------------------------------------------------------------------


def test_timeline_entries_are_numbered_contiguously_per_incident(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)

    observation = service.append_observation(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Voice contact established", idempotency_key="obs-key-1"
        ),
    )
    action = service.append_action(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Headlamp battery replaced", idempotency_key="act-key-1"
        ),
    )
    suggestion = service.append_status_suggestion(
        incident.id,
        StatusSuggestionAppend(
            author_id=organizer,
            content="Situation is stable, downgrade to monitoring",
            suggested_status="monitoring",
            idempotency_key="sug-key-1",
        ),
    )
    second_observation = service.append_observation(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Wind picking up", idempotency_key="obs-key-2"
        ),
    )

    assert [entry.seq for entry in (observation, action, suggestion, second_observation)] == [
        1,
        2,
        3,
        4,
    ]
    assert observation.kind == "observation"
    assert action.kind == "action"
    assert suggestion.kind == "status_suggestion"
    assert suggestion.suggested_status == EmergencyStatus.MONITORING
    # A suggestion never changes incident status by itself.
    assert service.safety.get_incident(incident.id).status == EmergencyStatus.OPEN

    page = service.timeline(incident.id)
    assert [entry.seq for entry in page.entries] == [1, 2, 3, 4]
    assert page.has_more is False
    assert page.next_after_seq == 4


def test_sequence_numbering_is_independent_between_incidents(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    first = _open_incident(service, expedition, organizer, key="incident-a")
    second = _open_incident(service, expedition, organizer, key="incident-b")
    entry_a = service.append_observation(
        first.id,
        IncidentEntryAppend(author_id=organizer, content="A", idempotency_key="obs-alpha-1"),
    )
    entry_b = service.append_observation(
        second.id,
        IncidentEntryAppend(author_id=organizer, content="B", idempotency_key="obs-beta-1"),
    )
    assert entry_a.seq == entry_b.seq == 1


def test_status_suggestion_must_differ_from_current_status(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    with pytest.raises(Exception, match="differ from the current status"):
        service.append_status_suggestion(
            incident.id,
            StatusSuggestionAppend(
                author_id=organizer,
                content="Suggest open while open",
                suggested_status="open",
                idempotency_key="same-status",
            ),
        )


def test_closed_incident_rejects_new_timeline_entries(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    now = datetime.now(UTC)
    incident = _open_incident(service, expedition, organizer)
    final_action = service.append_action(
        incident.id,
        IncidentEntryAppend(author_id=organizer, content="Evacuated", idempotency_key="act-close"),
    )
    service.transition_incident(
        incident.id,
        IncidentTransition(
            actor_id=organizer,
            target_status="resolved",
            final_action_entry_id=final_action.id,
            resolved_at=now + timedelta(hours=1),
            idempotency_key="close-incident",
        ),
    )
    with pytest.raises(InvalidStateError, match="closed incidents"):
        service.append_observation(
            incident.id,
            IncidentEntryAppend(
                author_id=organizer, content="too late", idempotency_key="late-obs"
            ),
        )


# ---------------------------------------------------------------------------
# Append-only guarantees at the database level
# ---------------------------------------------------------------------------


def test_timeline_rows_cannot_be_updated_or_deleted(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    entry = service.append_observation(
        incident.id,
        IncidentEntryAppend(author_id=organizer, content="Immutable", idempotency_key="imm-key-1"),
    )
    # Commit so the row survives the rollbacks forced by the rejected statements.
    session.commit()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            IncidentTimelineEntry.__table__.update()
            .where(IncidentTimelineEntry.id == entry.id)
            .values(content="rewritten")
        )
    session.rollback()
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            IncidentTimelineEntry.__table__.delete().where(
                IncidentTimelineEntry.id == entry.id
            )
        )
    session.rollback()

    assert service.safety.get_timeline_entry(entry.id).content == "Immutable"


def test_handover_confirmations_cannot_be_updated_or_deleted(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    handover = service.append_handover(
        incident.id,
        HandoverAppend(
            author_id=organizer,
            content="Shift change",
            successor_id=successor,
            idempotency_key="handover-key-1",
        ),
    )
    confirmation = service.confirm_handover(
        incident.id,
        handover.id,
        HandoverConfirm(
            successor_id=successor,
            confirm_through_seq=handover.seq,
            idempotency_key="confirm-key-1",
        ),
    )
    session.commit()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            HandoverConfirmation.__table__.update()
            .where(HandoverConfirmation.id == confirmation.id)
            .values(confirmed_through_seq=99)
        )
    session.rollback()
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            HandoverConfirmation.__table__.delete().where(
                HandoverConfirmation.id == confirmation.id
            )
        )
    session.rollback()


# ---------------------------------------------------------------------------
# Handover lifecycle and ownership rules
# ---------------------------------------------------------------------------


def test_handover_owner_stays_effective_until_successor_confirms(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)

    handover = service.append_handover(
        incident.id,
        HandoverAppend(
            author_id=organizer,
            content="Relief arriving at 02:00",
            successor_id=successor,
            idempotency_key="handover-x",
        ),
    )
    assert handover.successor_id == successor
    assert handover.required_through_seq == handover.seq == 1
    assert handover.handover_confirmation is None

    # The successor sees the pending todo; the reported owner still derives to the
    # original reporter while confirmation is outstanding.
    todos = service.handover_todos(successor)
    assert len(todos) == 1
    assert todos[0].from_owner_id == organizer
    assert todos[0].required_through_seq == 1
    assert todos[0].current_head_seq == 1

    # The outgoing owner can still record actions while awaiting confirmation.
    service.append_action(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Still in charge, documenting progress",
            idempotency_key="act-pending",
        ),
    )

    # A second handover cannot be opened while one is pending.
    with pytest.raises(ConflictError, match="already awaiting confirmation"):
        service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=organizer,
                content="Try again",
                successor_id=successor,
                idempotency_key="handover-dup",
            ),
        )

    # Head has moved to seq 2: confirming the old handover seq is rejected.
    with pytest.raises(ConflictError, match="latest timeline sequence"):
        service.confirm_handover(
            incident.id,
            handover.id,
            HandoverConfirm(
                successor_id=successor,
                confirm_through_seq=handover.seq,
                idempotency_key="confirm-stale",
            ),
        )

    confirmation = service.confirm_handover(
        incident.id,
        handover.id,
        HandoverConfirm(
            successor_id=successor,
            confirm_through_seq=2,
            idempotency_key="confirm-ok",
        ),
    )
    assert confirmation.confirmed_through_seq == 2
    assert service.handover_todos(successor) == []

    # Ownership has transferred: the outgoing owner can no longer hand over.
    with pytest.raises(UnauthorizedOperationError, match="current responsible owner"):
        service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=organizer,
                content="Former owner tries another handover",
                successor_id=organizer,
                idempotency_key="handover-after",
            ),
        )

    # The new owner can hand the incident back.
    next_handover = service.append_handover(
        incident.id,
        HandoverAppend(
            author_id=successor,
            content="Situation contained, handing back",
            successor_id=organizer,
            idempotency_key="handover-back",
        ),
    )
    assert next_handover.seq == 3


def test_handover_requires_current_owner(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    with pytest.raises(UnauthorizedOperationError, match="current responsible owner"):
        service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=successor,
                content="Not the owner yet",
                successor_id=successor,
                idempotency_key="handover-takeover",
            ),
        )


def test_handover_successor_validation(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    with pytest.raises(Exception, match="differ from the current owner"):
        service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=organizer,
                content="Handing to myself",
                successor_id=organizer,
                idempotency_key="handover-self",
            ),
        )


def test_confirmation_authorization_and_stale_sequence_checks(session) -> None:
    organizer, successor, outsider, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    handover = service.append_handover(
        incident.id,
        HandoverAppend(
            author_id=organizer,
            content="Relief brief",
            successor_id=successor,
            idempotency_key="handover-auth",
        ),
    )

    # Only the designated successor can confirm.
    with pytest.raises(UnauthorizedOperationError, match="designated successor"):
        service.confirm_handover(
            incident.id,
            handover.id,
            HandoverConfirm(
                successor_id=outsider,
                confirm_through_seq=handover.seq,
                idempotency_key="confirm-outsider",
            ),
        )

    # A newer entry moves the head forward; confirming the old head seq is stale.
    service.append_observation(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer,
            content="Update after handover",
            idempotency_key="post-handover-obs",
        ),
    )
    with pytest.raises(ConflictError, match="latest timeline sequence"):
        service.confirm_handover(
            incident.id,
            handover.id,
            HandoverConfirm(
                successor_id=successor,
                confirm_through_seq=handover.seq,
                idempotency_key="confirm-stale-seq",
            ),
        )

    service.confirm_handover(
        incident.id,
        handover.id,
        HandoverConfirm(
            successor_id=successor,
            confirm_through_seq=handover.seq + 1,
            idempotency_key="confirm-good",
        ),
    )

    # Double confirmation is rejected.
    with pytest.raises(ConflictError, match="already been confirmed"):
        service.confirm_handover(
            incident.id,
            handover.id,
            HandoverConfirm(
                successor_id=successor,
                confirm_through_seq=handover.seq,
                idempotency_key="confirm-again",
            ),
        )


def test_confirmation_unknown_or_cross_incident_entry_is_not_found(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    other = _open_incident(service, expedition, organizer, key="incident-other")
    handover = service.append_handover(
        other.id,
        HandoverAppend(
            author_id=organizer,
            content="Belongs to the other incident",
            successor_id=successor,
            idempotency_key="handover-cross",
        ),
    )
    with pytest.raises(NotFoundError):
        service.confirm_handover(
            incident.id,
            handover.id,
            HandoverConfirm(
                successor_id=successor,
                confirm_through_seq=handover.seq,
                idempotency_key="confirm-cross",
            ),
        )


# ---------------------------------------------------------------------------
# Explicit status transition rules
# ---------------------------------------------------------------------------


def test_explicit_status_transitions_are_enforced(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)

    monitoring = service.transition_incident(
        incident.id,
        IncidentTransition(
            actor_id=organizer,
            target_status="monitoring",
            idempotency_key="to-monitoring",
        ),
    )
    assert monitoring.status == EmergencyStatus.MONITORING

    reopened = service.transition_incident(
        incident.id,
        IncidentTransition(actor_id=organizer, target_status="open", idempotency_key="reopen-open")
    )
    assert reopened.status == EmergencyStatus.OPEN

    final_action = service.append_action(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Located and safe", idempotency_key="final-act"
        ),
    )
    service.transition_incident(
        incident.id,
        IncidentTransition(
            actor_id=organizer,
            target_status="resolved",
            final_action_entry_id=final_action.id,
            resolved_at=datetime.now(UTC),
            idempotency_key="to-resolved",
        ),
    )

    # Terminal statuses cannot move again.
    with pytest.raises(InvalidStateError, match="cannot change"):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=organizer, target_status="open", idempotency_key="reopen-denied"
            ),
        )


def test_closing_requires_a_final_action_entry(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    observation = service.append_observation(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Just an observation", idempotency_key="obs-only"
        ),
    )

    # No linked entry at all.
    with pytest.raises(Exception, match="final action timeline entry"):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=organizer,
                target_status="false_alarm",
                resolved_at=datetime.now(UTC),
                idempotency_key="false-no-entry",
            ),
        )

    # Linking an observation instead of an action is rejected.
    with pytest.raises(Exception, match="must be an action"):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=organizer,
                target_status="false_alarm",
                final_action_entry_id=observation.id,
                resolved_at=datetime.now(UTC),
                idempotency_key="false-wrong-kind",
            ),
        )

    # resolved_at is mandatory when closing.
    action = service.append_action(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Confirmed sensor glitch", idempotency_key="act-false"
        ),
    )
    with pytest.raises(Exception, match="resolved_at"):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=organizer,
                target_status="false_alarm",
                final_action_entry_id=action.id,
                idempotency_key="false-no-time",
            ),
        )

    result = service.transition_incident(
        incident.id,
        IncidentTransition(
            actor_id=organizer,
            target_status="false_alarm",
            final_action_entry_id=action.id,
            resolved_at=datetime.now(UTC),
            idempotency_key="false-ok",
        ),
    )
    assert result.status == EmergencyStatus.FALSE_ALARM


def test_final_action_entry_must_belong_to_same_incident(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer, key="inc-main")
    other = _open_incident(service, expedition, organizer, key="inc-side")
    foreign_action = service.append_action(
        other.id,
        IncidentEntryAppend(
            author_id=organizer, content="Other incident action", idempotency_key="foreign-act"
        ),
    )
    with pytest.raises(Exception, match="belong to this incident"):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=organizer,
                target_status="resolved",
                final_action_entry_id=foreign_action.id,
                resolved_at=datetime.now(UTC),
                idempotency_key="close-foreign",
            ),
        )


def test_status_transition_requires_owner_or_organizer(session) -> None:
    organizer, successor, outsider, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    with pytest.raises(UnauthorizedOperationError):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=outsider, target_status="monitoring", idempotency_key="outsider-move"
            ),
        )
    # A designated successor who has not confirmed cannot move status either.
    with pytest.raises(UnauthorizedOperationError):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=successor, target_status="monitoring", idempotency_key="relief-move"
            ),
        )


def test_expected_version_conflict_blocks_transition(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    with pytest.raises(ConflictError, match="modified by another operation"):
        service.transition_incident(
            incident.id,
            IncidentTransition(
                actor_id=organizer,
                target_status="monitoring",
                expected_version=99,
                idempotency_key="stale-version",
            ),
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_append_retries_do_not_duplicate(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    request = IncidentEntryAppend(
        author_id=organizer, content="Exactly once", idempotency_key="once-key"
    )
    first = service.append_observation(incident.id, request)
    second = service.append_observation(incident.id, request)
    assert first.id == second.id
    assert first.seq == second.seq == 1
    assert len(service.timeline(incident.id).entries) == 1
    audit_count = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.correlation_id == "once-key")
    )
    assert audit_count == 1


def test_idempotency_key_rejects_different_payload(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    service.append_observation(
        incident.id,
        IncidentEntryAppend(author_id=organizer, content="First", idempotency_key="reused-key"),
    )
    with pytest.raises(IdempotencyConflictError):
        service.append_observation(
            incident.id,
            IncidentEntryAppend(
                author_id=organizer, content="Second", idempotency_key="reused-key"
            ),
        )


def test_idempotent_transition_and_confirmation_retries(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    handover = service.append_handover(
        incident.id,
        HandoverAppend(
            author_id=organizer,
            content="Handing over",
            successor_id=successor,
            idempotency_key="handover-idem",
        ),
    )
    confirm_request = HandoverConfirm(
        successor_id=successor,
        confirm_through_seq=handover.seq,
        idempotency_key="confirm-idem",
    )
    first = service.confirm_handover(incident.id, handover.id, confirm_request)
    second = service.confirm_handover(incident.id, handover.id, confirm_request)
    assert first.id == second.id
    assert (
        session.scalar(
            select(func.count()).select_from(HandoverConfirmation)
        )
        == 1
    )

    final_action = service.append_action(
        incident.id,
        IncidentEntryAppend(
            author_id=successor, content="All clear", idempotency_key="final-idem"
        ),
    )
    transition = IncidentTransition(
        actor_id=successor,
        target_status="resolved",
        final_action_entry_id=final_action.id,
        resolved_at=datetime.now(UTC),
        idempotency_key="transition-idem",
    )
    resolved_once = service.transition_incident(incident.id, transition)
    resolved_twice = service.transition_incident(incident.id, transition)
    assert resolved_once.version == resolved_twice.version
    assert service.safety.get_incident(incident.id).status == EmergencyStatus.RESOLVED


# ---------------------------------------------------------------------------
# Incremental reads and handover todos
# ---------------------------------------------------------------------------


def test_timeline_supports_incremental_reads(session) -> None:
    organizer, _, _, _, expedition = _setup(session)
    service = SafetyService(session)
    incident = _open_incident(service, expedition, organizer)
    for index in range(3):
        service.append_observation(
            incident.id,
            IncidentEntryAppend(
                author_id=organizer,
                content=f"Note {index}",
                idempotency_key=f"page-obs-{index}",
            ),
        )

    first_page = service.timeline(incident.id, after_seq=0, limit=2)
    assert [entry.seq for entry in first_page.entries] == [1, 2]
    assert first_page.has_more is True
    assert first_page.next_after_seq == 2

    second_page = service.timeline(incident.id, after_seq=2, limit=2)
    assert [entry.seq for entry in second_page.entries] == [3]
    assert second_page.has_more is False
    assert second_page.next_after_seq == 3

    empty = service.timeline(incident.id, after_seq=3, limit=2)
    assert empty.entries == []
    assert empty.has_more is False
    assert empty.next_after_seq == 3


def test_handover_todos_list_open_incidents_only(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    first = _open_incident(service, expedition, organizer, key="todo-incident-a")
    second = _open_incident(service, expedition, organizer, key="todo-incident-b")
    handover_a = service.append_handover(
        first.id,
        HandoverAppend(
            author_id=organizer,
            content="A handover",
            successor_id=successor,
            idempotency_key="todo-hand-a",
        ),
    )
    handover_b = service.append_handover(
        second.id,
        HandoverAppend(
            author_id=organizer,
            content="B handover",
            successor_id=successor,
            idempotency_key="todo-hand-b",
        ),
    )

    todos = service.handover_todos(successor)
    assert {item.incident_id for item in todos} == {first.id, second.id}
    assert todos[0].entry_id == handover_a.id

    # Confirming the first handover removes only that todo.
    service.confirm_handover(
        first.id,
        handover_a.id,
        HandoverConfirm(
            successor_id=successor,
            confirm_through_seq=handover_a.seq,
            idempotency_key="todo-confirm-a",
        ),
    )
    remaining = service.handover_todos(successor)
    assert [item.incident_id for item in remaining] == [second.id]

    # Closing the second incident drops its pending handover from the queue.
    final_action = service.append_action(
        second.id,
        IncidentEntryAppend(
            author_id=organizer, content="Resolved independently",
            idempotency_key="todo-final-b",
        ),
    )
    service.transition_incident(
        second.id,
        IncidentTransition(
            actor_id=organizer,
            target_status="resolved",
            final_action_entry_id=final_action.id,
            resolved_at=datetime.now(UTC),
            idempotency_key="todo-close-b",
        ),
    )
    assert service.handover_todos(successor) == []

    # A confirmation arriving after closure is still refused.
    with pytest.raises(InvalidStateError, match="closed incidents"):
        service.confirm_handover(
            second.id,
            handover_b.id,
            HandoverConfirm(
                successor_id=successor,
                confirm_through_seq=2,
                idempotency_key="todo-confirm-late",
            ),
        )


# ---------------------------------------------------------------------------
# Structured audit trail
# ---------------------------------------------------------------------------


def test_audit_records_actor_states_and_related_sequences(session) -> None:
    organizer, successor, _, _, expedition = _setup(session)
    service = SafetyService(session)
    now = datetime.now(UTC)
    incident = _open_incident(service, expedition, organizer)
    service.append_observation(
        incident.id,
        IncidentEntryAppend(
            author_id=organizer, content="Audited note", idempotency_key="audit-obs"
        ),
    )
    handover = service.append_handover(
        incident.id,
        HandoverAppend(
            author_id=organizer,
            content="Audited handover",
            successor_id=successor,
            idempotency_key="audit-hand",
        ),
    )
    service.confirm_handover(
        incident.id,
        handover.id,
        HandoverConfirm(
            successor_id=successor,
            confirm_through_seq=handover.seq,
            idempotency_key="audit-confirm",
        ),
    )
    service.transition_incident(
        incident.id,
        IncidentTransition(
            actor_id=successor,
            target_status="monitoring",
            idempotency_key="audit-monitor",
        ),
    )
    final_action = service.append_action(
        incident.id,
        IncidentEntryAppend(
            author_id=successor,
            content="Final evacuation complete",
            idempotency_key="audit-final-act",
        ),
    )
    service.transition_incident(
        incident.id,
        IncidentTransition(
            actor_id=successor,
            target_status="resolved",
            final_action_entry_id=final_action.id,
            resolved_at=now + timedelta(hours=1),
            idempotency_key="audit-resolve",
        ),
    )

    append_log = session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.TIMELINE_APPENDED.value)
        .order_by(AuditLog.id)
    )
    assert append_log.actor_id == organizer
    assert append_log.context["incident_id"] == incident.id
    assert append_log.context["seq"] == 1
    assert append_log.context["kind"] == "observation"
    assert append_log.after_state["content"] == "Audited note"

    confirm_log = session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.HANDOVER_CONFIRMED.value)
    )
    assert confirm_log.actor_id == successor
    assert confirm_log.before_state == {"owner_id": organizer}
    assert confirm_log.after_state == {"owner_id": successor}
    assert confirm_log.context["handover_seq"] == handover.seq
    assert confirm_log.context["confirmed_through_seq"] == handover.seq

    status_logs = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "emergency_incident",
            AuditLog.action == AuditAction.STATUS_CHANGED.value,
        )
        .order_by(AuditLog.id)
    ).all()
    assert [log.before_state["status"] for log in status_logs] == ["open", "monitoring"]
    assert [log.after_state["status"] for log in status_logs] == [
        "monitoring",
        "resolved",
    ]
    closing_log = status_logs[-1]
    assert closing_log.context["final_action_entry_id"] == final_action.id
    assert closing_log.context["final_action_seq"] == final_action.seq


# ---------------------------------------------------------------------------
# Concurrency: sequence allocation, duplicate confirmation, close race
# ---------------------------------------------------------------------------


def test_concurrent_appends_keep_sequences_contiguous(database: Database) -> None:
    with database.session() as setup_session:
        organizer = create_user(setup_session)
        route = create_route(setup_session, actor_id=organizer)
        expedition = create_expedition(setup_session, organizer_id=organizer, route_id=route)
        incident = SafetyService(setup_session).record_incident(
            EmergencyIncidentCreate(
                expedition_id=expedition,
                reported_by=organizer,
                incident_type="weather",
                risk_level="critical",
                occurred_at=datetime.now(UTC),
                description="Concurrent storm updates",
                idempotency_key="concurrent-incident",
            )
        )
        incident_id = incident.id

    worker_count = 8
    barrier = Barrier(worker_count)

    def append(worker: int) -> int:
        barrier.wait()

        def operation(session) -> int:
            return SafetyService(session).append_observation(
                incident_id,
                IncidentEntryAppend(
                    author_id=organizer,
                    content=f"Worker {worker} update",
                    idempotency_key=f"concurrent-key-{worker}",
                ),
            ).seq

        return database.run_write(operation)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        sequences = list(pool.map(append, range(worker_count)))

    assert sorted(sequences) == list(range(1, worker_count + 1))
    with database.session() as session:
        rows = session.scalars(
            select(IncidentTimelineEntry.seq)
            .where(IncidentTimelineEntry.incident_id == incident_id)
            .order_by(IncidentTimelineEntry.seq)
        ).all()
        assert rows == list(range(1, worker_count + 1))


def test_concurrent_confirmation_only_one_wins(database: Database) -> None:
    with database.session() as setup_session:
        organizer, successor, _, _, expedition = _setup(setup_session)
        service = SafetyService(setup_session)
        incident = _open_incident(service, expedition, organizer, key="race-confirm")
        handover = service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=organizer,
                content="Race to confirm",
                successor_id=successor,
                idempotency_key="race-hand",
            ),
        )
        handover_id = handover.id
        head_seq = handover.seq

    attempts = 2
    barrier = Barrier(attempts)

    def confirm(worker: int) -> str:
        barrier.wait()

        def operation(session) -> str:
            try:
                SafetyService(session).confirm_handover(
                    incident.id,
                    handover_id,
                    HandoverConfirm(
                        successor_id=successor,
                        confirm_through_seq=head_seq,
                        idempotency_key=f"race-confirm-{worker}",
                    ),
                )
                return "confirmed"
            except ConflictError:
                return "lost"
            except OperationalError:
                return "busy"

        return database.run_write(operation)

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(confirm, range(attempts)))
    assert outcomes.count("confirmed") == 1
    with database.session() as session:
        assert session.query(HandoverConfirmation).count() == 1


def test_concurrent_close_and_confirm_race_is_serialized(database: Database) -> None:
    with database.session() as setup_session:
        organizer, successor, _, _, expedition = _setup(setup_session)
        service = SafetyService(setup_session)
        incident = _open_incident(service, expedition, organizer, key="race-close")
        final_action = service.append_action(
            incident.id,
            IncidentEntryAppend(
                author_id=organizer, content="Final handling", idempotency_key="race-final"
            ),
        )
        handover = service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=organizer,
                content="Race against closure",
                successor_id=successor,
                idempotency_key="race-hand-close",
            ),
        )
        handover_id = handover.id
        action_id = final_action.id

    barrier = Barrier(2)

    def close(*_: object) -> str:
        barrier.wait()

        def operation(session) -> str:
            try:
                SafetyService(session).transition_incident(
                    incident.id,
                    IncidentTransition(
                        actor_id=organizer,
                        target_status="resolved",
                        final_action_entry_id=action_id,
                        resolved_at=datetime.now(UTC),
                        idempotency_key="race-close-commit",
                    ),
                )
                return "closed"
            except (ConflictError, InvalidStateError):
                return "lost"

        return database.run_write(operation)

    def confirm(*_: object) -> str:
        barrier.wait()

        def operation(session) -> str:
            try:
                SafetyService(session).confirm_handover(
                    incident.id,
                    handover_id,
                    HandoverConfirm(
                        successor_id=successor,
                        confirm_through_seq=handover.seq,
                        idempotency_key="race-confirm-commit",
                    ),
                )
                return "confirmed"
            except (ConflictError, InvalidStateError):
                return "lost"

        return database.run_write(operation)

    with ThreadPoolExecutor(max_workers=2) as pool:
        close_future = pool.submit(close)
        confirm_future = pool.submit(confirm)
        close_result = close_future.result()
        confirm_result = confirm_future.result()

    with database.session() as session:
        final_status = SafetyService(session).safety.get_incident(incident.id).status
        confirmation_count = session.query(HandoverConfirmation).count()

    assert close_result == "closed"
    assert final_status == EmergencyStatus.RESOLVED
    if confirm_result == "confirmed":
        # Confirmation landed before closure; it must acknowledge the required seq.
        assert confirmation_count == 1
    else:
        # Closure landed first: no confirmation may exist, and a later confirmation
        # attempt against the closed incident is refused.
        assert confirmation_count == 0
        with (
            database.session() as session,
            pytest.raises(InvalidStateError, match="closed incidents"),
        ):
            SafetyService(session).confirm_handover(
                incident.id,
                handover_id,
                HandoverConfirm(
                    successor_id=successor,
                    confirm_through_seq=handover.seq,
                    idempotency_key="race-confirm-after",
                ),
            )


# ---------------------------------------------------------------------------
# Transaction rollback
# ---------------------------------------------------------------------------


def test_concurrent_same_idempotency_key_creates_one_entry(database: Database) -> None:
    with database.session() as setup_session:
        organizer = create_user(setup_session)
        route = create_route(setup_session, actor_id=organizer)
        expedition = create_expedition(setup_session, organizer_id=organizer, route_id=route)
        incident = SafetyService(setup_session).record_incident(
            EmergencyIncidentCreate(
                expedition_id=expedition,
                reported_by=organizer,
                incident_type="delay",
                risk_level="moderate",
                occurred_at=datetime.now(UTC),
                description="Retried over a flaky radio link",
                idempotency_key="retry-incident",
            )
        )
        incident_id = incident.id

    worker_count = 6
    barrier = Barrier(worker_count)

    def append(worker: int) -> tuple[int, int] | str:
        barrier.wait()

        def operation(session) -> tuple[int, int] | str:
            try:
                entry = SafetyService(session).append_observation(
                    incident_id,
                    IncidentEntryAppend(
                        author_id=organizer,
                        content="Identical retried report",
                        idempotency_key="same-retried-report",
                    ),
                )
                return entry.id, entry.seq
            except IdempotencyConflictError:
                return "conflict"

        return database.run_write(operation)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(append, range(worker_count)))

    # Every retry, whether it raced in the append loop or reached the pre-check
    # after the first commit, replays the single stored entry.
    assert results == [(1, 1)] * worker_count
    with database.session() as session:
        entries = session.scalars(
            select(IncidentTimelineEntry).where(
                IncidentTimelineEntry.incident_id == incident_id
            )
        ).all()
        assert len(entries) == 1
        assert entries[0].content == "Identical retried report"


def test_concurrent_handovers_only_one_becomes_pending(database: Database) -> None:
    with database.session() as setup_session:
        organizer, successor, _, _, expedition = _setup(setup_session)
        incident = _open_incident(
            SafetyService(setup_session), expedition, organizer, key="race-two-handovers"
        )
        incident_id = incident.id

    attempts = 2
    barrier = Barrier(attempts)

    def handover(worker: int) -> str:
        barrier.wait()

        def operation(session) -> str:
            try:
                SafetyService(session).append_handover(
                    incident_id,
                    HandoverAppend(
                        author_id=organizer,
                        content=f"Competing handover {worker}",
                        successor_id=successor,
                        idempotency_key=f"competing-handover-{worker}",
                    ),
                )
                return "created"
            except ConflictError:
                return "rejected"

        return database.run_write(operation)

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(handover, range(attempts)))
    assert outcomes.count("created") == 1
    assert outcomes.count("rejected") == 1
    with database.session() as session:
        assert session.query(ActiveHandover).filter_by(incident_id=incident_id).count() == 1
        todos = SafetyService(session).handover_todos(successor)
        assert len(todos) == 1
        handover_entries = session.scalars(
            select(IncidentTimelineEntry).where(
                IncidentTimelineEntry.incident_id == incident_id
            )
        ).all()
        # The loser's savepoint was fully rolled back; only one handover exists.
        assert len(handover_entries) == 1
        assert handover_entries[0].seq == 1
        assert todos[0].entry_seq == 1


# ---------------------------------------------------------------------------
# Transaction rollback
# ---------------------------------------------------------------------------


def test_failed_append_rolls_back_everything(database: Database) -> None:
    with database.session() as setup_session:
        organizer, _, outsider, _, expedition = _setup(setup_session)
        incident_id = _open_incident(
            SafetyService(setup_session), expedition, organizer
        ).id

    with pytest.raises(UnauthorizedOperationError), database.session() as session:
        SafetyService(session).append_observation(
            incident_id,
            IncidentEntryAppend(
                author_id=outsider,
                content="Should never be stored",
                idempotency_key="rollback-append",
            ),
        )

    with database.session() as session:
        assert session.query(IncidentTimelineEntry).count() == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.correlation_id == "rollback-append")
            )
            == 0
        )


def test_failed_transition_leaves_incident_unchanged(database: Database) -> None:
    with database.session() as setup_session:
        organizer, _, _, _, expedition = _setup(setup_session)
        incident_id = _open_incident(
            SafetyService(setup_session), expedition, organizer
        ).id

    with (
        pytest.raises(Exception, match="final action timeline entry"),
        database.session() as session,
    ):
        SafetyService(session).transition_incident(
            incident_id,
            IncidentTransition(
                actor_id=organizer,
                target_status="resolved",
                resolved_at=datetime.now(UTC),
                idempotency_key="rollback-transition",
            ),
        )

    with database.session() as session:
        reloaded = SafetyService(session).safety.get_incident(incident_id)
        assert reloaded.status == EmergencyStatus.OPEN
        assert reloaded.resolved_at is None
        assert reloaded.version == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.correlation_id == "rollback-transition")
            )
            == 0
        )


# ---------------------------------------------------------------------------
# Continuity after application restart
# ---------------------------------------------------------------------------


def test_timeline_continuity_after_application_restart(settings: Settings) -> None:
    first = Database(settings)
    initialize_database(first)
    with first.session() as session:
        organizer, successor, _, _, expedition = _setup(session)
        service = SafetyService(session)
        incident = _open_incident(service, expedition, organizer, key="restart-inc")
        service.append_observation(
            incident.id,
            IncidentEntryAppend(
                author_id=organizer, content="Before restart", idempotency_key="restart-1"
            ),
        )
        handover = service.append_handover(
            incident.id,
            HandoverAppend(
                author_id=organizer,
                content="Pending across restart",
                successor_id=successor,
                idempotency_key="restart-hand",
            ),
        )
        incident_id = incident.id
        handover_id = handover.id
        successor_id = successor
    first.engine.dispose()

    second = Database(settings)
    initialize_database(second)
    assert migration_status(second) == {
        "initialized": True,
        "applied": ["0001", "0002"],
        "pending": [],
    }
    try:
        with second.session() as session:
            service = SafetyService(session)
            page = service.timeline(incident_id)
            assert [entry.seq for entry in page.entries] == [1, 2]
            todos = service.handover_todos(successor_id)
            assert len(todos) == 1
            assert todos[0].entry_id == handover_id

            # Sequence allocation continues from the persisted head.
            continued = service.append_observation(
                incident_id,
                IncidentEntryAppend(
                    author_id=organizer,
                    content="After restart",
                    idempotency_key="restart-2",
                ),
            )
            assert continued.seq == 3

            # The pending handover can still be confirmed post-restart.
            confirmation = service.confirm_handover(
                incident_id,
                handover_id,
                HandoverConfirm(
                    successor_id=successor_id,
                    confirm_through_seq=3,
                    idempotency_key="restart-confirm",
                ),
            )
            assert confirmation.confirmed_through_seq == 3

        # Append-only guards survive the restart as well.
        with second.session() as session, pytest.raises(IntegrityError, match="append-only"):
            entry = session.scalar(select(IncidentTimelineEntry).limit(1))
            session.execute(
                IncidentTimelineEntry.__table__.update()
                .where(IncidentTimelineEntry.id == entry.id)
                .values(content="tampered after restart")
            )
    finally:
        second.engine.dispose()
