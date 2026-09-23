from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from trailforge.models.audit import AuditLog
from trailforge.models.users import User

UTC = UTC


def test_health_reports_sqlite_configuration(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"
    assert body["foreign_keys"] == 1
    assert body["journal_mode"] == "wal"


def test_validation_error_has_structured_response(client) -> None:
    response = client.post(
        "/api/v1/users",
        json={"email": "broken", "display_name": ""},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "request_validation_error"
    assert detail["message"] == "request validation failed"
    assert len(detail["errors"]) >= 2


def test_api_complete_business_chain_and_database_side_effects(client) -> None:
    user_response = client.post(
        "/api/v1/users",
        json={
            "email": "api-hiker@example.com",
            "display_name": "API Hiker",
            "timezone": "Asia/Shanghai",
        },
    )
    assert user_response.status_code == 201
    user = user_response.json()
    user_id = user["id"]
    profile_response = client.put(
        f"/api/v1/users/{user_id}/sport-profile",
        params={"actor_id": user_id},
        json={
            "height_cm": 170,
            "weight_kg": 64,
            "fitness_level": "intermediate",
            "outdoor_experience": "Local hiking",
            "weekly_training_minutes": 180,
        },
    )
    assert profile_response.status_code == 200
    route_response = client.post(
        "/api/v1/routes",
        params={"actor_id": user_id},
        json={
            "name": "API Ridge",
            "region": "API Mountains",
            "description": "Stored locally",
            "distance_km": 8,
            "elevation_gain_m": 400,
            "elevation_loss_m": 400,
            "min_altitude_m": 100,
            "max_altitude_m": 500,
            "estimated_duration_minutes": 180,
            "difficulty": "moderate",
            "is_loop": True,
            "is_published": True,
            "segments": [
                {
                    "sequence": 1,
                    "name": "Loop",
                    "distance_km": 8,
                    "elevation_gain_m": 400,
                    "estimated_duration_minutes": 180,
                    "difficulty": "moderate",
                    "start_latitude": 30,
                    "start_longitude": 120,
                    "end_latitude": 30,
                    "end_longitude": 120,
                }
            ],
            "points": [],
            "risk_tag_ids": [],
        },
    )
    assert route_response.status_code == 201, route_response.text
    route = route_response.json()
    start = datetime.now(UTC) + timedelta(days=10)
    expedition_response = client.post(
        "/api/v1/expeditions",
        json={
            "organizer_id": user_id,
            "route_id": route["id"],
            "name": "API Expedition",
            "meeting_location": "Trailhead",
            "meeting_at": (start - timedelta(hours=1)).isoformat(),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=6)).isoformat(),
            "registration_deadline": (start - timedelta(days=1)).isoformat(),
            "capacity": 5,
            "minimum_fitness_level": 1,
            "risk_level": "moderate",
        },
    )
    assert expedition_response.status_code == 201, expedition_response.text
    expedition = expedition_response.json()
    open_response = client.post(
        f"/api/v1/expeditions/{expedition['id']}/status",
        json={"target_status": "open", "actor_id": user_id, "reason": "Ready"},
    )
    assert open_response.status_code == 200
    roster_response = client.get(f"/api/v1/expeditions/{expedition['id']}/roster")
    assert roster_response.status_code == 200
    roster = roster_response.json()
    assert roster["confirmed_count"] == 1
    assert roster["members"][0]["role"] == "leader"
    dashboard = client.get("/api/v1/statistics/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["active_users"] == 1
    assert dashboard.json()["published_routes"] == 1
    database = client.app.state.database
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLog)) >= 5


def test_api_duplicate_email_returns_conflict_and_rolls_back(client) -> None:
    payload = {"email": "duplicate@example.com", "display_name": "First"}
    assert client.post("/api/v1/users", json=payload).status_code == 201
    response = client.post(
        "/api/v1/users",
        json={"email": "DUPLICATE@example.com", "display_name": "Second"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"
    with client.app.state.database.session() as session:
        users = session.query(User).filter_by(email="duplicate@example.com").all()
        assert len(users) == 1
        assert users[0].display_name == "First"


def test_api_route_pagination_and_sorting(client) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "sorter@example.com", "display_name": "Sorter"},
    ).json()
    for index, distance in enumerate((4, 7, 10)):
        response = client.post(
            "/api/v1/routes",
            params={"actor_id": user["id"]},
            json={
                "name": f"Route {index}",
                "region": "Sorted",
                "distance_km": distance,
                "elevation_gain_m": 100,
                "estimated_duration_minutes": 120,
                "difficulty": "easy",
                "segments": [
                    {
                        "sequence": 1,
                        "name": "Segment",
                        "distance_km": distance,
                        "estimated_duration_minutes": 120,
                        "difficulty": "easy",
                        "start_latitude": 0,
                        "start_longitude": 0,
                        "end_latitude": 0.1,
                        "end_longitude": 0.1,
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
    response = client.get(
        "/api/v1/routes",
        params={
            "region": "Sorted",
            "page": 1,
            "page_size": 2,
            "sort": "distance_km",
            "direction": "desc",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"] == {"page": 1, "page_size": 2, "total": 3, "pages": 2}
    assert [item["distance_km"] for item in body["items"]] == [10, 7]


def test_api_unknown_sort_field_is_clear_422(client) -> None:
    response = client.get("/api/v1/routes", params={"sort": "drop_table"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "domain_validation_error"
    assert "allowed" in detail["context"]


def _api_expedition(client, email: str) -> tuple[int, int]:
    user = client.post(
        "/api/v1/users",
        json={"email": email, "display_name": "Night Lead"},
    ).json()
    route = client.post(
        "/api/v1/routes",
        params={"actor_id": user["id"]},
        json={
            "name": f"Night Route {email}",
            "region": "Night Mountains",
            "distance_km": 6,
            "elevation_gain_m": 300,
            "elevation_loss_m": 300,
            "min_altitude_m": 100,
            "max_altitude_m": 500,
            "estimated_duration_minutes": 200,
            "difficulty": "moderate",
            "is_loop": False,
            "is_published": True,
            "segments": [
                {
                    "sequence": 1,
                    "name": "Ridge",
                    "distance_km": 6,
                    "elevation_gain_m": 300,
                    "estimated_duration_minutes": 200,
                    "difficulty": "moderate",
                    "start_latitude": 30,
                    "start_longitude": 120,
                    "end_latitude": 30.01,
                    "end_longitude": 120.01,
                }
            ],
        },
    ).json()
    start = datetime.now(UTC) + timedelta(days=10)
    expedition = client.post(
        "/api/v1/expeditions",
        json={
            "organizer_id": user["id"],
            "route_id": route["id"],
            "name": f"Night Expedition {email}",
            "meeting_location": "Trailhead",
            "meeting_at": (start - timedelta(hours=1)).isoformat(),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=6)).isoformat(),
            "registration_deadline": (start - timedelta(days=1)).isoformat(),
            "capacity": 5,
            "minimum_fitness_level": 1,
            "risk_level": "moderate",
        },
    ).json()
    return user["id"], expedition["id"]


def test_api_incident_timeline_handover_and_close_flow(client) -> None:
    lead, expedition_id = _api_expedition(client, "night-lead@example.com")
    relief = client.post(
        "/api/v1/users",
        json={"email": "night-relief@example.com", "display_name": "Night Relief"},
    ).json()["id"]
    now = datetime.now(UTC)

    incident = client.post(
        "/api/v1/safety/incidents",
        json={
            "expedition_id": expedition_id,
            "reported_by": lead,
            "incident_type": "lost_person",
            "risk_level": "high",
            "occurred_at": now.isoformat(),
            "description": "Hiker overdue at waypoint three",
            "idempotency_key": "api-incident-1",
        },
    )
    assert incident.status_code == 201, incident.text
    incident_id = incident.json()["id"]

    observation = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline/observations",
        json={
            "author_id": lead,
            "content": "Voice contact established",
            "idempotency_key": "api-observation-1",
        },
    )
    assert observation.status_code == 201, observation.text
    assert observation.json()["seq"] == 1

    action = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline/actions",
        json={
            "author_id": lead,
            "content": "Search team dispatched with warm layers",
            "idempotency_key": "api-action-1",
        },
    )
    assert action.json()["seq"] == 2

    # Idempotent replay returns the same entry instead of creating a second one.
    replay = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline/actions",
        json={
            "author_id": lead,
            "content": "Search team dispatched with warm layers",
            "idempotency_key": "api-action-1",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == action.json()["id"]

    handover = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline/handovers",
        json={
            "author_id": lead,
            "content": "Relief takes over; confirm after reading everything",
            "successor_id": relief,
            "idempotency_key": "api-handover-1",
        },
    )
    assert handover.status_code == 201, handover.text
    handover_body = handover.json()
    assert handover_body["seq"] == 3
    assert handover_body["required_through_seq"] == 3

    pending = client.get("/api/v1/safety/handovers/pending", params={"user_id": relief})
    assert pending.status_code == 200
    assert [item["entry_id"] for item in pending.json()] == [handover_body["id"]]

    # The designated successor must not confirm a stale sequence.
    stale = client.post(
        f"/api/v1/safety/incidents/{incident_id}/handovers/{handover_body['id']}/confirm",
        json={
            "successor_id": relief,
            "confirm_through_seq": 1,
            "idempotency_key": "api-confirm-stale",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["context"]["required_through_seq"] == 3

    confirmation = client.post(
        f"/api/v1/safety/incidents/{incident_id}/handovers/{handover_body['id']}/confirm",
        json={
            "successor_id": relief,
            "confirm_through_seq": 3,
            "idempotency_key": "api-confirm-ok",
        },
    )
    assert confirmation.status_code == 201, confirmation.text

    # Incremental timeline read: only entries after the handover cursor.
    page = client.get(
        f"/api/v1/safety/incidents/{incident_id}/timeline",
        params={"after_seq": 2, "limit": 10},
    )
    assert page.status_code == 200
    body = page.json()
    assert [entry["seq"] for entry in body["entries"]] == [3]
    assert body["entries"][0]["handover_confirmation"]["confirmed_by"] == relief

    # Closing requires a final action entry.
    missing = client.post(
        f"/api/v1/safety/incidents/{incident_id}/transitions",
        json={
            "actor_id": relief,
            "target_status": "resolved",
            "resolved_at": (now + timedelta(hours=2)).isoformat(),
            "idempotency_key": "api-close-missing",
        },
    )
    assert missing.status_code == 422
    assert "final action" in missing.json()["detail"]["message"]

    closed = client.post(
        f"/api/v1/safety/incidents/{incident_id}/transitions",
        json={
            "actor_id": relief,
            "target_status": "resolved",
            "final_action_entry_id": action.json()["id"],
            "resolved_at": (now + timedelta(hours=2)).isoformat(),
            "idempotency_key": "api-close-ok",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "resolved"

    # Closed incidents reject both further appends and handover confirmations.
    after_close = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline/observations",
        json={
            "author_id": relief,
            "content": "Too late",
            "idempotency_key": "api-late-entry",
        },
    )
    assert after_close.status_code == 409
    assert after_close.json()["detail"]["code"] == "invalid_state_transition"


def test_api_handover_requires_designated_successor(client) -> None:
    lead, expedition_id = _api_expedition(client, "sole-lead@example.com")
    relief = client.post(
        "/api/v1/users",
        json={"email": "other-relief@example.com", "display_name": "Other Relief"},
    ).json()["id"]
    now = datetime.now(UTC)
    incident_id = client.post(
        "/api/v1/safety/incidents",
        json={
            "expedition_id": expedition_id,
            "reported_by": lead,
            "incident_type": "weather",
            "risk_level": "critical",
            "occurred_at": now.isoformat(),
            "description": "Thunderstorm closing the pass",
            "idempotency_key": "api-incident-auth",
        },
    ).json()["id"]
    handover = client.post(
        f"/api/v1/safety/incidents/{incident_id}/timeline/handovers",
        json={
            "author_id": lead,
            "content": "Storm watch handover",
            "successor_id": relief,
            "idempotency_key": "api-handover-auth",
        },
    ).json()

    # A different user cannot confirm someone else's handover.
    response = client.post(
        f"/api/v1/safety/incidents/{incident_id}/handovers/{handover['id']}/confirm",
        json={
            "successor_id": lead,
            "confirm_through_seq": handover["seq"],
            "idempotency_key": "api-confirm-wrong",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "operation_not_allowed"
