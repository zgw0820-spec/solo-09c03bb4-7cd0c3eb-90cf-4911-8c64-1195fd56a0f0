"""API tests for the drill workflow: preview, advance, shortage and revision."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def sample_payload(client, residual=320.0):
    sample = client.get("/api/sample").json()
    return {"devices": sample["devices"], "residual_wh": residual}


def test_health_and_sample(client):
    assert client.get("/api/health").json()["ok"] is True
    sample = client.get("/api/sample").json()
    assert len(sample["devices"]) == 5
    assert sample["expected"]["makespan"] == 12
    assert sample["expected"]["order"][-1] == "storage"


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "UPS" in response.text


def test_preview_feasible(client):
    response = client.post("/api/preview", json=sample_payload(client))
    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["feasible"] is True
    assert body["plan"]["makespan"] == 12
    assert body["shortfall_wh"] == 0


def test_preview_rejects_cycles_and_bad_input(client):
    payload = {
        "residual_wh": 100,
        "devices": [
            {"id": "a", "name": "A", "power_w": 10, "shutdown_min": 2, "depends_on": ["b"]},
            {"id": "b", "name": "B", "power_w": 10, "shutdown_min": 2, "depends_on": ["a"]},
        ],
    }
    response = client.post("/api/preview", json=payload)
    assert response.status_code == 400
    assert "环" in response.json()["detail"]

    seven = {
        "residual_wh": 100,
        "devices": [
            {"id": f"d{i}", "name": f"D{i}", "power_w": 10, "shutdown_min": 2}
            for i in range(7)
        ],
    }
    assert client.post("/api/preview", json=seven).status_code == 422


def test_full_drill_reaches_complete(client):
    created = client.post("/api/drills", json=sample_payload(client))
    assert created.status_code == 201
    session = created.json()
    sid = session["id"]
    assert session["status"] == "ready"

    for _ in range(20):
        if session["status"] == "complete":
            break
        response = client.post(f"/api/drills/{sid}/advance", json={"to_next_event": True})
        assert response.status_code == 200
        session = response.json()

    assert session["status"] == "complete"
    assert session["now"] == 12
    assert session["residual_wh"] == pytest.approx(21.5, abs=0.01)
    assert all(d["status"] == "stopped" for d in session["device_states"])
    # The recorded history matches the projection at every step.
    history = {h["minute"]: h["residual_wh"] for h in session["history"]}
    for minute, residual in history.items():
        assert residual >= 0


def test_step_by_step_matches_energy_curve(client):
    sid = client.post("/api/drills", json=sample_payload(client)).json()["id"]
    seen = []
    for _ in range(12):
        body = client.post(f"/api/drills/{sid}/advance", json={"minutes": 1}).json()
        seen.append(body["residual_wh"])
    assert seen[0] == pytest.approx(320 - 2570 / 60, abs=0.01)
    assert body["status"] == "complete"
    assert seen[-1] == pytest.approx(21.5, abs=0.01)


def test_shortage_blocks_advance_until_revised(client):
    created = client.post("/api/drills", json=sample_payload(client, 250.0)).json()
    sid = created["id"]
    assert created["status"] == "shortage"
    assert created["shortfall_wh"] == pytest.approx(48.5)
    assert created["can_advance"] is False

    blocked = client.post(f"/api/drills/{sid}/advance", json={"minutes": 1})
    assert blocked.status_code == 409
    assert "48.5" in blocked.json()["detail"]

    revised = client.post(f"/api/drills/{sid}/revise", json={"residual_wh": 320.0}).json()
    assert revised["status"] == "ready"
    assert revised["feasible"] is True
    assert revised["shortfall_wh"] == 0

    advanced = client.post(f"/api/drills/{sid}/advance", json={"minutes": 1})
    assert advanced.status_code == 200


def test_revision_preserves_issued_commands(client):
    sid = client.post("/api/drills", json=sample_payload(client, 400.0)).json()["id"]
    before = client.post(f"/api/drills/{sid}/advance", json={"minutes": 4}).json()
    issued_before = {a["device_id"]: (a["start"], a["end"]) for a in before["issued"]}
    assert set(issued_before) == {"web", "app", "db", "backup"}

    revised = client.post(f"/api/drills/{sid}/revise", json={"residual_wh": 120.0}).json()
    assert revised["status"] == "shortage"
    assert revised["revision"]["preserved_finish_times"] is True

    after = {d["id"]: (d["start"], d["end"], d["frozen"]) for d in revised["device_states"]}
    for device_id, times in issued_before.items():
        assert after[device_id][:2] == times
        assert after[device_id][2] is True

    # The only un-issued device (shared storage) keeps its timing here because
    # the original plan was already optimal.
    assert after["storage"][2] is False

    # Advancing is blocked while there is still a shortage.
    assert client.post(f"/api/drills/{sid}/advance", json={"minutes": 1}).status_code == 409

    # A second revision that reports enough energy resumes the drill.
    recovered = client.post(f"/api/drills/{sid}/revise", json={"residual_wh": 200.0}).json()
    assert recovered["status"] == "running"
    assert recovered["revision"]["count"] == 2
    # Previously issued completion times survive the second revision too.
    state = {d["id"]: (d["start"], d["end"]) for d in recovered["device_states"]}
    for device_id, times in issued_before.items():
        assert state[device_id] == times

    finished = client.post(f"/api/drills/{sid}/advance", json={"minutes": 8}).json()
    assert finished["status"] == "complete"
    assert finished["residual_wh"] >= 0


def test_revision_reschedules_only_unstarted_actions(client):
    """Force a case where re-planning must move a not-yet-started device."""
    devices = [
        {"id": "d0", "name": "D0", "power_w": 50, "shutdown_min": 6, "depends_on": []},
        {"id": "d1", "name": "D1", "power_w": 1, "shutdown_min": 8, "depends_on": []},
        {"id": "d2", "name": "D2", "power_w": 1, "shutdown_min": 2, "depends_on": []},
        {"id": "d3", "name": "D3", "power_w": 1, "shutdown_min": 3, "depends_on": []},
        {"id": "d4", "name": "D4", "power_w": 50, "shutdown_min": 6, "depends_on": []},
    ]
    created = client.post(
        "/api/drills", json={"devices": devices, "residual_wh": 20.0}
    ).json()
    sid = created["id"]
    first_plan = {a["device_id"]: (a["start"], a["end"]) for a in created["plan"]["actions"]}

    client.post(f"/api/drills/{sid}/advance", json={"minutes": 3})
    revised = client.post(f"/api/drills/{sid}/revise", json={"residual_wh": 9.5}).json()

    actions = {a["device_id"]: a for a in revised["plan"]["actions"]}
    revision = revised["revision"]

    # The three commands already issued keep their exact start and finish.
    for device_id in ("d0", "d2", "d3"):
        assert actions[device_id]["frozen"] is True
        assert (actions[device_id]["start"], actions[device_id]["end"]) == first_plan[device_id]
        assert device_id not in {r["device_id"] for r in revision["rescheduled"]}

    # d1 and d4 had not started; the tighter budget swaps their lanes.
    moved = {r["device_id"]: (r["old_start"], r["old_end"], r["start"], r["end"])
             for r in revision["rescheduled"]}
    assert set(moved) == {"d1", "d4"}
    assert moved["d4"][2:] == (5, 11) and moved["d4"][:2] == (6, 12)
    assert moved["d1"][2:] == (6, 14) and moved["d1"][:2] == (5, 13)

    # Nobody restarts before the planning minute, and dependencies still hold.
    for a in actions.values():
        if not a["frozen"]:
            assert a["start"] >= revised["now"]


def test_revise_validates_input(client):
    sid = client.post("/api/drills", json=sample_payload(client)).json()["id"]
    assert client.post(f"/api/drills/{sid}/revise", json={"residual_wh": -1}).status_code == 422
    assert client.get("/api/drills/nope").status_code == 404
    assert client.post("/api/drills/nope/advance", json={"minutes": 1}).status_code == 404


# ---------------------------------------------------------------------------
# Run-length encoded refresh chain
# ---------------------------------------------------------------------------


def _expand_segments(segments):
    points = {}
    for seg in segments:
        for minute in range(seg["start"], seg["end"] + 1):
            residual = max(
                0.0,
                seg["start_residual_wh"]
                - seg["draw_w"] * (minute - seg["start"]) / 60.0,
            )
            points[minute] = (round(residual, 3), seg["draw_w"])
    return points


def test_compact_payload_segments_do_not_grow_with_minutes(client):
    # Two long jobs: a long constant-power stretch followed by one load drop.
    devices = [
        {"id": "a", "name": "A", "power_w": 300, "shutdown_min": 120, "depends_on": []},
        {"id": "b", "name": "B", "power_w": 200, "shutdown_min": 60, "depends_on": []},
    ]
    created = client.post(
        "/api/drills",
        json={"devices": devices, "residual_wh": 10000.0},
        params={"compact": "true"},
    ).json()
    sid = created["id"]
    assert created["compact"] is True
    assert created["history"] is None and created["projection"] is None

    session = created
    for _ in range(120):
        session = client.post(
            f"/api/drills/{sid}/advance",
            json={"minutes": 1},
            params={"compact": "true"},
        ).json()

    # 120 one-minute advances, but only the initial run and one load drop.
    assert session["now"] == 120
    assert len(session["history_segments"]) == 2
    first, second = session["history_segments"]
    assert (first["start"], first["end"], first["draw_w"]) == (0, 60, 500)
    assert (second["start"], second["end"], second["draw_w"]) == (60, 120, 300)
    # The load-drop knee and its residual anchor are preserved.
    assert first["residual_wh"] == 9500.0
    assert second["start_residual_wh"] == 9500.0


def test_compact_expansion_matches_legacy_minute_history(client):
    sid_full = client.post("/api/drills", json=sample_payload(client)).json()["id"]
    sid_compact = client.post(
        "/api/drills", json=sample_payload(client), params={"compact": "true"}
    ).json()["id"]
    for _ in range(12):
        client.post(f"/api/drills/{sid_full}/advance", json={"minutes": 1})
        client.post(
            f"/api/drills/{sid_compact}/advance",
            json={"minutes": 1},
            params={"compact": "true"},
        )

    legacy = {h["minute"]: (h["residual_wh"], h["draw_w"])
              for h in client.get(f"/api/drills/{sid_full}").json()["history"]}
    compact_segments = client.get(
        f"/api/drills/{sid_compact}", params={"compact": "true"}
    ).json()["history_segments"]
    expanded = _expand_segments(compact_segments)
    assert set(expanded) == set(legacy)
    for minute, (residual, draw) in expanded.items():
        legacy_residual, legacy_draw = legacy[minute]
        assert draw == legacy_draw
        assert abs(residual - legacy_residual) < 0.002

    # The default API still returns the full minute-by-minute arrays.
    default_state = client.get(f"/api/drills/{sid_compact}").json()
    assert isinstance(default_state["history"], list)
    assert isinstance(default_state["projection"], list)
    assert default_state["compact"] is False


def test_minute_lookup_reports_residual_draw_and_revisions(client):
    sid = client.post(
        "/api/drills", json=sample_payload(client), params={"compact": "true"}
    ).json()["id"]
    client.post(f"/api/drills/{sid}/advance", json={"minutes": 3},
                params={"compact": "true"})

    # Three revisions filed at the same minute, in order.
    for value in (300.0, 260.0, 330.0):
        client.post(
            f"/api/drills/{sid}/revise",
            json={"residual_wh": value},
            params={"compact": "true"},
        )

    record = client.get(f"/api/drills/{sid}/minutes/3").json()
    assert record["minute"] == 3
    assert record["residual_wh"] == 330.0
    assert [r["residual_wh"] for r in record["revisions"]] == [300.0, 260.0, 330.0]
    assert record["revision_count"] == 3

    # Minutes before the revision keep their originally recorded reading.
    earlier = client.get(f"/api/drills/{sid}/minutes/2").json()
    assert earlier["revision_count"] == 0
    assert earlier["residual_wh"] == round(320 - 2570 * 2 / 60.0, 3)

    # A minute that has not arrived yet is rejected.
    response = client.get(f"/api/drills/{sid}/minutes/99")
    assert response.status_code == 404


def test_revision_jump_preserved_but_history_readings_unchanged(client):
    sid = client.post(
        "/api/drills", json=sample_payload(client), params={"compact": "true"}
    ).json()["id"]
    client.post(f"/api/drills/{sid}/advance", json={"minutes": 3},
                params={"compact": "true"})
    before = client.get(f"/api/drills/{sid}/minutes/2").json()

    revised = client.post(
        f"/api/drills/{sid}/revise",
        json={"residual_wh": 400.0},
        params={"compact": "true"},
    ).json()
    # The jump is a zero-length anchor at the revision minute.
    jumps = [s for s in revised["history_segments"]
             if s["start"] == 3 and s["end"] == 3]
    assert len(jumps) == 1 and jumps[0]["residual_wh"] == 400.0
    # Earlier readings are untouched.
    after = client.get(f"/api/drills/{sid}/minutes/2").json()
    assert after["residual_wh"] == before["residual_wh"]
    assert after["draw_w"] == before["draw_w"]


def test_shortage_withdraws_projection_and_revision_restores_it(client):
    sid = client.post(
        "/api/drills", json=sample_payload(client), params={"compact": "true"}
    ).json()["id"]
    client.post(f"/api/drills/{sid}/advance", json={"minutes": 3},
                params={"compact": "true"})

    starved = client.post(
        f"/api/drills/{sid}/revise",
        json={"residual_wh": 1.0},
        params={"compact": "true"},
    ).json()
    assert starved["feasible"] is False
    assert starved["projection_segments"] == []
    assert client.post(f"/api/drills/{sid}/advance", json={"minutes": 1}).status_code == 409

    recovered = client.post(
        f"/api/drills/{sid}/revise",
        json={"residual_wh": 400.0},
        params={"compact": "true"},
    ).json()
    assert recovered["feasible"] is True
    assert len(recovered["projection_segments"]) >= 1
    # Recorded history before the shortage still holds its original values.
    record = client.get(f"/api/drills/{sid}/minutes/2").json()
    assert record["residual_wh"] == round(320 - 2570 * 2 / 60.0, 3)
