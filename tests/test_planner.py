"""Planner unit tests: constraints, optimality and mid-drill revision semantics."""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.planner import (  # noqa: E402
    Action,
    Device,
    PlanError,
    compute_plan,
    sample_devices,
)


def make(**kwargs):
    return Device(**kwargs)


def device_map(plan):
    return {a.device_id: (a.start, a.end) for a in plan.actions}


def assert_valid(devices, plan, now=0):
    """Brute-force validation of the stated constraints."""
    actions = device_map(plan)
    assert len(actions) == len(devices)

    frozen_ids = {a.device_id for a in plan.actions if a.frozen}
    for d in devices:
        start, end = actions[d.id]
        assert end - start == d.shutdown_min
        if d.id not in frozen_ids:
            assert start >= now
        for dep in d.depends_on:
            # A provider starts only after every consumer (`d`) is stopped.
            assert end <= actions[dep][0], f"{d.id} still running when {dep} started"

    # Parallelism: sweep every integer minute and every interval boundary.
    horizon = max(end for _, end in actions.values()) + 1
    for minute in range(0, horizon + 1):
        busy = sum(1 for start, end in actions.values() if start <= minute < end)
        assert busy <= 2, f"{busy} devices shutting down at minute {minute}"


def energy_of(devices, plan, now=0):
    return sum(d.power_w * (plan.action_for(d.id).end - now) for d in devices) / 60.0


def brute_force(devices, now=0):
    """Enumerate every assignment of start times on a coarse grid for n<=4."""
    ids = [d.id for d in devices]
    durations = {d.id: d.shutdown_min for d in devices}
    horizon = now + sum(durations.values()) + 1
    best = None
    ranges = [range(now, horizon) for _ in ids]
    for combo in itertools.product(*ranges):
        starts = dict(zip(ids, combo))
        actions = {
            i: Action(i, starts[i], starts[i] + durations[i]) for i in ids
        }
        ok = True
        for d in devices:
            for dep in d.depends_on:
                if actions[d.id].end > actions[dep].start:
                    ok = False
        if not ok:
            continue
        for minute in range(now, horizon + 1):
            if sum(1 for a in actions.values() if a.start <= minute < a.end) > 2:
                ok = False
                break
        if not ok:
            continue
        makespan = max(a.end for a in actions.values())
        energy = sum(d.power_w * (actions[d.id].end - now) for d in devices) / 60.0
        key = (makespan, energy)
        if best is None or key < best[0]:
            best = (key, dict(starts))
    return best


# ---------------------------------------------------------------------------
# Sample scenario
# ---------------------------------------------------------------------------


def test_sample_plan_is_earliest_and_fits():
    devices = sample_devices()
    plan = compute_plan(devices, 320.0)
    assert plan.feasible
    assert plan.makespan == 12
    assert plan.energy_needed_wh == pytest.approx(298.5)
    assert_valid(devices, plan)
    # storage must stop last, only after db and backup are fully stopped.
    m = device_map(plan)
    assert m["storage"][0] >= max(m["db"][1], m["backup"][1])
    assert m["db"][0] >= max(m["web"][1], m["app"][1])


def test_sample_shortfall():
    devices = sample_devices()
    plan = compute_plan(devices, 250.0)
    assert not plan.feasible
    assert plan.shortfall_wh == pytest.approx(48.5)
    assert plan.min_energy_wh == pytest.approx(298.5)
    # Even infeasible, the reference schedule is valid.
    assert_valid(devices, plan)


def test_required_energy_is_monotone_under_larger_budget():
    devices = sample_devices()
    loose = compute_plan(devices, 10000.0)
    assert loose.feasible
    assert loose.makespan == 12


# ---------------------------------------------------------------------------
# Constraint validation
# ---------------------------------------------------------------------------


def test_cycle_rejected():
    devices = [
        Device("a", "A", 100, 2, ("b",)),
        Device("b", "B", 100, 2, ("a",)),
    ]
    with pytest.raises(PlanError):
        compute_plan(devices, 100)


def test_unknown_dependency_rejected():
    with pytest.raises(PlanError):
        compute_plan([Device("a", "A", 100, 2, ("ghost",))], 100)


def test_too_many_devices_rejected():
    devices = [Device(f"d{i}", f"D{i}", 100, 2) for i in range(7)]
    with pytest.raises(PlanError):
        compute_plan(devices, 1000)


def test_parallel_limit_respected_for_many_independent_devices():
    devices = [Device(f"d{i}", f"D{i}", 100 + i, 5) for i in range(6)]
    plan = compute_plan(devices, 100000)
    assert_valid(devices, plan)
    assert plan.makespan == 15  # two lanes of 3 x 5 minutes


def test_zero_power_devices_are_free():
    devices = [Device(f"d{i}", f"D{i}", 0, 3) for i in range(4)]
    plan = compute_plan(devices, 0)
    assert plan.feasible
    assert plan.energy_needed_wh == 0


# ---------------------------------------------------------------------------
# Optimality against brute force
# ---------------------------------------------------------------------------


def test_matches_brute_force_on_random_small_cases():
    random.seed(7)
    for _ in range(25):
        n = random.randint(1, 4)
        base = [
            Device(f"d{i}", f"D{i}", random.choice([0, 120, 300, 500]), random.randint(1, 4))
            for i in range(n)
        ]
        devices = []
        for i, d in enumerate(base):
            deps = tuple(p.id for p in base[:i] if random.random() < 0.35)
            devices.append(Device(d.id, d.name, d.power_w, d.shutdown_min, deps))
        plan = compute_plan(devices, 1_000_000)
        best = brute_force(devices)
        assert plan.makespan == best[0][0], (devices, plan.makespan, best)
        assert plan.energy_needed_wh == pytest.approx(best[0][1])


def test_energy_tension_can_widen_makespan():
    """Earliest finish wins only while the energy budget allows it."""
    devices = [
        Device("low", "LOW", 1, 6),
        Device("h1", "H1", 1000, 4),
        Device("h2", "H2", 1000, 4),
    ]
    # Plenty of energy: finish in 8 minutes by starting the low-power job
    # alongside one high-power job.
    rich = compute_plan(devices, 1000.0)
    assert rich.feasible and rich.makespan == 8
    assert rich.energy_needed_wh == pytest.approx(200.1)

    # A tight budget forces the 10-minute low-energy order.
    tight = compute_plan(devices, 150.0)
    assert tight.feasible and tight.makespan == 10
    assert tight.energy_needed_wh == pytest.approx(133.5)

    # Below the minimum energy nothing fits; the gap is exact.
    starved = compute_plan(devices, 100.0)
    assert not starved.feasible
    assert starved.shortfall_wh == pytest.approx(33.5)


# ---------------------------------------------------------------------------
# Mid-drill revision
# ---------------------------------------------------------------------------


def test_frozen_actions_keep_completion_times():
    devices = sample_devices()
    first = compute_plan(devices, 400.0)
    issued = [Action(a.device_id, a.start, a.end, True) for a in first.actions if a.start <= 4]
    revised = compute_plan(devices, 120.0, now=4, frozen=issued)
    before = device_map(first)
    after = device_map(revised)
    for action in issued:
        assert after[action.device_id] == (action.start, action.end)
        assert after[action.device_id] == before[action.device_id]
    # Unstarted work may move, but must still respect the frozen horizon.
    for device in devices:
        if device.id not in {a.device_id for a in issued}:
            assert after[device.id][0] >= 4
    assert_valid(devices, revised, now=4)


def test_revision_can_reorder_unstarted_devices():
    devices = [
        Device("x", "X", 500, 4),
        Device("y", "Y", 500, 4),
        Device("z", "Z", 800, 4),
    ]
    first = compute_plan(devices, 100000)
    # Freeze both minute-0 jobs, then shrink the budget drastically.
    issued = [Action(a.device_id, a.start, a.end, True) for a in first.actions if a.start == 0]
    revised = compute_plan(devices, 30.0, now=1, frozen=issued)
    for action in issued:
        assert revised.action_for(action.device_id).end == action.end


def test_frozen_provider_before_consumer_finishes_is_rejected():
    devices = [
        Device("web", "Web", 100, 3, ("db",)),
        Device("db", "DB", 100, 3),
    ]
    with pytest.raises(PlanError):
        compute_plan(
            devices,
            1000,
            now=5,
            frozen=[
                Action("web", 0, 3, True),
                Action("db", 1, 4, True),
            ],
        )


def test_frozen_overlapping_beyond_limit_is_rejected():
    devices = [Device(f"d{i}", f"D{i}", 100, 5) for i in range(3)]
    with pytest.raises(PlanError):
        compute_plan(
            devices,
            1000,
            now=5,
            frozen=[
                Action("d0", 0, 5, True),
                Action("d1", 0, 5, True),
                Action("d2", 0, 5, True),
            ],
        )


def test_revision_energy_equals_frozen_plus_remaining():
    devices = sample_devices()
    first = compute_plan(devices, 400.0)
    issued = [Action(a.device_id, a.start, a.end, True) for a in first.actions if a.start <= 2]
    revised = compute_plan(devices, 400.0, now=2, frozen=issued)
    # Energy counted from the planning instant only.
    expected = sum(
        d.power_w * (revised.action_for(d.id).end - 2) for d in devices
    ) / 60.0
    assert revised.energy_needed_wh == pytest.approx(expected)
