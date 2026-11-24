"""Randomised drill simulation: invariants that must hold across revisions."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402


def random_devices(rng: random.Random):
    n = rng.randint(1, 6)
    devices = []
    for i in range(n):
        deps = [devices[j]["id"] for j in range(i) if rng.random() < 0.3]
        devices.append(
            {
                "id": f"d{i}",
                "name": f"设备 {i}",
                "power_w": rng.choice([0, 80, 250, 400, 900]),
                "shutdown_min": rng.randint(1, 6),
                "depends_on": deps,
            }
        )
    return devices


def check_invariants(session, issued_history):
    states = {d["id"]: d for d in session["device_states"]}
    actions = {a["device_id"]: a for a in session["plan"]["actions"]}

    for device_id, action in actions.items():
        assert action["end"] - action["start"] == states[device_id]["shutdown_min"]
        assert action["start"] >= 0
        if action["frozen"]:
            assert action["start"] <= session["now"]
            assert action["end"] > action["start"]

    for device in session["devices"]:
        for provider in device["depends_on"]:
            # Every consumer must be fully stopped before its provider starts.
            assert actions[device["id"]]["end"] <= actions[provider]["start"]

    horizon = max(a["end"] for a in actions.values())
    for minute in range(0, horizon + 1):
        busy = sum(1 for a in actions.values() if a["start"] <= minute < a["end"])
        assert busy <= 2

    # Any command issued at an earlier revision keeps its exact finish time.
    for device_id, times in issued_history.items():
        assert (actions[device_id]["start"], actions[device_id]["end"]) == times

    assert session["residual_wh"] >= 0
    for point in session["history"]:
        assert point["residual_wh"] >= 0

    if not session["feasible"]:
        assert session["shortfall_wh"] > 0
        assert session["can_advance"] is False


@pytest.mark.parametrize("seed", range(12))
def test_random_drill_with_revisions(seed):
    rng = random.Random(seed)
    with TestClient(app) as client:
        devices = random_devices(rng)
        residual = rng.uniform(0, 400)
        created = client.post(
            "/api/drills", json={"devices": devices, "residual_wh": residual}
        )
        assert created.status_code == 201, created.text
        session = created.json()
        sid = session["id"]
        issued_history = {}

        for _ in range(14):
            if session["feasible"]:
                issued_history.update(
                    {a["device_id"]: (a["start"], a["end"]) for a in session["issued"]}
                )
            if rng.random() < 0.45:
                new_residual = session["residual_wh"] + rng.uniform(-60, 120)
                new_residual = max(0.0, new_residual)
                response = client.post(
                    f"/api/drills/{sid}/revise", json={"residual_wh": new_residual}
                )
                assert response.status_code == 200, response.text
                session = response.json()
                assert session["revision"]["preserved_finish_times"] is True
            else:
                response = client.post(
                    f"/api/drills/{sid}/advance",
                    json={"minutes": rng.randint(1, 3), "to_next_event": rng.random() < 0.4},
                )
                if response.status_code == 409:
                    session = client.get(f"/api/drills/{sid}").json()
                    assert not session["feasible"]
                    assert session["shortfall_wh"] > 0
                else:
                    assert response.status_code == 200, response.text
                    session = response.json()

            check_invariants(session, issued_history)
            if session["status"] == "complete":
                break

        if session["feasible"]:
            # With a feasible plan we can always march to the end.
            final = client.post(f"/api/drills/{sid}/advance", json={"minutes": 240}).json()
            assert final["status"] == "complete"
            assert final["residual_wh"] >= 0


@pytest.mark.parametrize("seed", range(6))
def test_never_negative_energy_when_feasible(seed):
    rng = random.Random(100 + seed)
    with TestClient(app) as client:
        devices = random_devices(rng)
        created = client.post(
            "/api/drills", json={"devices": devices, "residual_wh": 10_000}
        ).json()
        sid = created["id"]
        session = created
        while session["status"] != "complete":
            session = client.post(
                f"/api/drills/{sid}/advance", json={"to_next_event": True}
            ).json()
        residual = session["residual_wh"]
        expected = 10_000 - session["plan"]["energy_needed_wh"]
        assert residual == pytest.approx(expected, abs=0.01)
