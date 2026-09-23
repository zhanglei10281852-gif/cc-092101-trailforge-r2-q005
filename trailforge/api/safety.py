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
    "/incidents/{incident_id}/timeline",
    response_model=TimelineEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def append_timeline_entry(
    incident_id: int,
    data: TimelineEntryCreate,
    session: SessionDep,
) -> TimelineEntryResponse:
    return SafetyService(session).append_timeline_entry(incident_id, data)


@router.get("/incidents/{incident_id}/timeline", response_model=IncidentTimelineSlice)
def read_timeline(
    incident_id: int,
    session: SessionDep,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> IncidentTimelineSlice:
    return SafetyService(session).timeline(incident_id, after_seq=after_seq, limit=limit)


@router.post("/incidents/{incident_id}/status", response_model=EmergencyIncidentResponse)
def transition_incident_status(
    incident_id: int,
    data: IncidentStatusTransition,
    session: SessionDep,
) -> EmergencyIncidentResponse:
    return SafetyService(session).transition_incident_status(incident_id, data)


@router.post(
    "/incidents/{incident_id}/handovers/{handover_seq}/confirm",
    response_model=EmergencyIncidentResponse,
)
def confirm_handover(
    incident_id: int,
    handover_seq: int,
    data: HandoverConfirm,
    session: SessionDep,
) -> EmergencyIncidentResponse:
    return SafetyService(session).confirm_handover(incident_id, handover_seq, data)


@router.get("/handovers/pending", response_model=list[PendingHandover])
def pending_handovers(
    session: SessionDep,
    user_id: int = Query(gt=0),
) -> list[PendingHandover]:
    return SafetyService(session).pending_handovers(user_id)


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
