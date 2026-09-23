from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
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
from trailforge.services.safety import SafetyService

router = APIRouter(prefix="/safety", tags=["safety"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.post(
    "/expeditions/{expedition_id}/check-ins",
    response_model=CheckInResponse,
    status_code=status.HTTP_201_CREATED,
)
def schedule_check_in(
    expedition_id: int,
    data: CheckInScheduleCreate,
    session: SessionDep,
    actor_id: int = Query(gt=0),
) -> CheckInResponse:
    return SafetyService(session).schedule_check_in(expedition_id, data, actor_id=actor_id)


@router.post("/check-ins/{check_in_id}/submit", response_model=CheckInResponse)
def submit_check_in(
    check_in_id: int,
    data: CheckInSubmit,
    session: SessionDep,
) -> CheckInResponse:
    return SafetyService(session).submit_check_in(check_in_id, data)


@router.get("/check-ins/overdue", response_model=list[OverdueCheckIn])
def overdue_check_ins(
    session: SessionDep,
    now: datetime | None = None,
) -> list[OverdueCheckIn]:
    return SafetyService(session).overdue(now=now)


@router.post(
    "/incidents",
    response_model=EmergencyIncidentResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_incident(
    data: EmergencyIncidentCreate,
    session: SessionDep,
) -> EmergencyIncidentResponse:
    return SafetyService(session).record_incident(data)


@router.post(
    "/incidents/{incident_id}/timeline/observations",
    response_model=IncidentTimelineEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def append_observation(
    incident_id: int,
    data: IncidentEntryAppend,
    session: SessionDep,
) -> IncidentTimelineEntryResponse:
    return SafetyService(session).append_observation(incident_id, data)


@router.post(
    "/incidents/{incident_id}/timeline/actions",
    response_model=IncidentTimelineEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def append_action(
    incident_id: int,
    data: IncidentEntryAppend,
    session: SessionDep,
) -> IncidentTimelineEntryResponse:
    return SafetyService(session).append_action(incident_id, data)


@router.post(
    "/incidents/{incident_id}/timeline/status-suggestions",
    response_model=IncidentTimelineEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def append_status_suggestion(
    incident_id: int,
    data: StatusSuggestionAppend,
    session: SessionDep,
) -> IncidentTimelineEntryResponse:
    return SafetyService(session).append_status_suggestion(incident_id, data)


@router.post(
    "/incidents/{incident_id}/timeline/handovers",
    response_model=IncidentTimelineEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def append_handover(
    incident_id: int,
    data: HandoverAppend,
    session: SessionDep,
) -> IncidentTimelineEntryResponse:
    return SafetyService(session).append_handover(incident_id, data)


@router.get("/incidents/{incident_id}/timeline", response_model=IncidentTimelinePage)
def incident_timeline(
    incident_id: int,
    session: SessionDep,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> IncidentTimelinePage:
    return SafetyService(session).timeline(incident_id, after_seq=after_seq, limit=limit)


@router.post(
    "/incidents/{incident_id}/handovers/{entry_id}/confirm",
    response_model=HandoverConfirmationResponse,
    status_code=status.HTTP_201_CREATED,
)
def confirm_handover(
    incident_id: int,
    entry_id: int,
    data: HandoverConfirm,
    session: SessionDep,
) -> HandoverConfirmationResponse:
    return SafetyService(session).confirm_handover(incident_id, entry_id, data)


@router.get("/handovers/pending", response_model=list[HandoverTodoItem])
def pending_handovers(
    session: SessionDep,
    user_id: int = Query(gt=0),
) -> list[HandoverTodoItem]:
    return SafetyService(session).handover_todos(user_id)


@router.post("/incidents/{incident_id}/transitions", response_model=EmergencyIncidentResponse)
def transition_incident(
    incident_id: int,
    data: IncidentTransition,
    session: SessionDep,
) -> EmergencyIncidentResponse:
    return SafetyService(session).transition_incident(incident_id, data)


@router.post(
    "/risk-assessments",
    response_model=RiskAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def assess_risk(
    data: RiskAssessmentCreate,
    session: SessionDep,
) -> RiskAssessmentResponse:
    return SafetyService(session).assess_risk(data)


@router.post(
    "/weather-snapshots",
    response_model=WeatherSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_weather_snapshot(
    data: WeatherSnapshotCreate,
    session: SessionDep,
) -> WeatherSnapshotResponse:
    return SafetyService(session).add_weather_snapshot(data)


@router.get("/expeditions/{expedition_id}/summary", response_model=SafetySummary)
def safety_summary(
    expedition_id: int,
    session: SessionDep,
    now: datetime | None = None,
) -> SafetySummary:
    return SafetyService(session).summary(expedition_id, now=now)
