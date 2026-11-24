"""Deterministic, exact shut-down planner for a battery-backed equipment rack.

Modelling rules (mirroring the drill brief)
------------------------------------------
* Up to 6 devices.  Each has a power draw (W) and a whole-minute shutdown
  duration.
* A device draws its full power until it is *completely stopped* - whether it
  is still waiting for its turn or is actively shutting down.
* At most two devices may be actively shutting down at the same instant.
* ``device.depends_on`` lists the devices it *consumes* (its providers).
  A provider may only begin shutting down after **all of its consumers** have
  completely stopped.  Example: web servers depend on the database, which
  depends on shared storage, so storage stops last.
* The UPS holds a given amount of usable energy.  The drill is feasible when
  the total energy needed by a schedule (from the planning instant onward)
  fits in the currently reported residual energy.

Objective
---------
Among schedules that fit the reported residual energy, return one with the
earliest global finish ("all devices fully stopped as early as possible"),
then the lowest energy use, then deterministic order.  If no schedule fits,
return the minimum achievable energy schedule and the minimum shortfall.

Exactness
---------
n <= 6, two machines, whole-minute durations and no release dates.  All
work-conserving *and* voluntary-idle schedules are enumerated, so the result
is the true optimum for the stated objective, not a list-scheduling
heuristic.

Mid-drill revisions
-------------------
Already issued commands (``frozen`` actions) keep their start and completion
times.  Only devices without an issued command are scheduled.  The search
starts at the current drill minute; in-flight frozen actions keep occupying
their slot and drawing power until their recorded completion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Optional, Sequence, Tuple

MAX_PARALLEL = 2
EPS = 1e-7


class PlanError(ValueError):
    """The requested device set or drill cannot be represented."""


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    power_w: int
    shutdown_min: int
    depends_on: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "power_w": self.power_w,
            "shutdown_min": self.shutdown_min,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class Action:
    device_id: str
    start: int
    end: int
    frozen: bool = False

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "start": self.start,
            "end": self.end,
            "frozen": self.frozen,
        }


@dataclass
class Plan:
    actions: List[Action]
    makespan: int
    energy_needed_wh: float
    budget_wh: float
    feasible: bool
    shortfall_wh: float
    min_energy_wh: float
    planning_from: int
    frozen: List[Action] = field(default_factory=list)

    @property
    def order(self) -> List[str]:
        # Actions are stored sorted by (start, input order).
        return [a.device_id for a in self.actions]

    def action_for(self, device_id: str) -> Action:
        for action in self.actions:
            if action.device_id == device_id:
                return action
        raise KeyError(device_id)

    def as_dict(self) -> dict:
        return {
            "actions": [a.as_dict() for a in self.actions],
            "makespan": self.makespan,
            "energy_needed_wh": round(self.energy_needed_wh, 3),
            "budget_wh": round(self.budget_wh, 3),
            "feasible": self.feasible,
            "shortfall_wh": round(self.shortfall_wh, 3),
            "min_energy_wh": round(self.min_energy_wh, 3),
            "planning_from": self.planning_from,
            "order": self.order,
            "frozen": [a.as_dict() for a in self.frozen],
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def normalise_devices(devices: Sequence[Device]) -> List[Device]:
    devices = list(devices)
    if not 1 <= len(devices) <= 6:
        raise PlanError("设备数量必须在 1 到 6 之间")
    ids = [d.id for d in devices]
    if any(not i for i in ids):
        raise PlanError("设备 ID 不能为空")
    if len(ids) != len(set(ids)):
        raise PlanError("设备 ID 重复")
    known = set(ids)
    for d in devices:
        if not d.name.strip():
            raise PlanError("设备名称不能为空")
        if d.power_w < 0:
            raise PlanError(f"{d.name} 的功率不能为负")
        if d.power_w > 1_000_000:
            raise PlanError(f"{d.name} 的功率超出合理范围")
        if d.shutdown_min < 1:
            raise PlanError(f"{d.name} 的停机耗时至少为 1 整分钟")
        if d.shutdown_min > 240:
            raise PlanError(f"{d.name} 的停机耗时超出合理范围")
        if d.id in d.depends_on:
            raise PlanError(f"{d.name} 不能依赖自身")
        if len(set(d.depends_on)) != len(d.depends_on):
            raise PlanError(f"{d.name} 的依赖列表有重复项")
        for dep in d.depends_on:
            if dep not in known:
                raise PlanError(f"{d.name} 依赖了不存在的设备 {dep}")
    if not _acyclic(devices):
        raise PlanError("依赖关系存在环，无法安排停机顺序")
    return devices


def _acyclic(devices: Sequence[Device]) -> bool:
    by_id = {d.id: d for d in devices}
    state: Dict[str, int] = {}

    def visit(node: str) -> bool:
        mark = state.get(node, 0)
        if mark == 1:
            return False
        if mark == 2:
            return True
        state[node] = 1
        for dep in by_id[node].depends_on:
            if not visit(dep):
                return False
        state[node] = 2
        return True

    return all(visit(node) for node in by_id)


def _consumers(devices: Sequence[Device]) -> Dict[str, List[str]]:
    """For every provider, the devices that consume it (must stop first)."""
    result: Dict[str, List[str]] = {d.id: [] for d in devices}
    for d in devices:
        for provider in d.depends_on:
            result[provider].append(d.id)
    return result


def _validate_frozen(
    devices: Sequence[Device],
    frozen: Sequence[Action],
    now: int,
    consumers: Dict[str, List[str]],
) -> Dict[str, Action]:
    by_id = {d.id: d for d in devices}
    frozen_by_id: Dict[str, Action] = {}
    for action in frozen:
        d = by_id.get(action.device_id)
        if d is None:
            raise PlanError(f"已下达命令引用了不存在的设备 {action.device_id}")
        if action.device_id in frozen_by_id:
            raise PlanError(f"{d.name} 有多条已下达命令")
        if action.start < 0:
            raise PlanError("已下达命令的起始时刻不能为负")
        if action.end != action.start + d.shutdown_min:
            raise PlanError(f"{d.name} 的已下达命令时长与停机耗时不一致")
        if action.start > now:
            raise PlanError(f"{d.name} 的已下达命令尚未开始，不能冻结")
        frozen_by_id[d.id] = Action(d.id, action.start, action.end, True)

    # A provider command is only legal once all its consumers have stopped.
    for device_id, action in frozen_by_id.items():
        for consumer_id in consumers[device_id]:
            consumer_action = frozen_by_id.get(consumer_id)
            if consumer_action is None or consumer_action.end > action.start:
                cname = by_id[consumer_id].name
                raise PlanError(
                    f"{by_id[device_id].name} 已下达命令，但使用方 {cname} 尚未停妥"
                )

    # Parallelism of the frozen history: sweep the start/end boundaries and
    # check that no more than MAX_PARALLEL commands are ever in flight.
    boundaries = sorted({a.start for a in frozen_by_id.values()} | {a.end for a in frozen_by_id.values()})
    for moment in boundaries:
        active = sum(
            1
            for a in frozen_by_id.values()
            if a.start <= moment < a.end
        )
        if active > MAX_PARALLEL:
            raise PlanError(f"已下达命令中同时停机的设备超过 {MAX_PARALLEL} 台")
    return frozen_by_id


# ---------------------------------------------------------------------------
# Exact schedule enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    actions: Tuple[Action, ...]
    makespan: int
    energy_wh: float
    signature: Tuple[Tuple[int, int, int], ...]


def _measure(
    devices: Sequence[Device], actions: Dict[str, Action], now: int, order: Dict[str, int]
) -> Tuple[int, float, Tuple[Tuple[int, int, int], ...]]:
    makespan = max(action.end for action in actions.values())
    watt_minutes = sum(
        d.power_w * max(0, actions[d.id].end - now) for d in devices
    )
    energy = watt_minutes / 60.0
    signature = tuple(
        sorted(
            (a.start, order[a.device_id], a.end) for a in actions.values()
        )
    )
    return makespan, energy, signature


def _enumerate(
    devices: Sequence[Device], now: int, frozen: Dict[str, Action]
) -> List[_Candidate]:
    """Enumerate every legal schedule from ``now`` with frozen commands fixed."""

    order = {d.id: index for index, d in enumerate(devices)}
    consumers = _consumers(devices)
    results: Dict[Tuple[Tuple[int, int, int], ...], _Candidate] = {}

    def emit(actions: Dict[str, Action]) -> None:
        makespan, energy, signature = _measure(devices, actions, now, order)
        if signature not in results:
            results[signature] = _Candidate(
                tuple(sorted(actions.values(), key=lambda a: (a.start, order[a.device_id]))),
                makespan,
                energy,
                signature,
            )

    def dfs(t: int, actions: Dict[str, Action]) -> None:
        remaining = [d for d in devices if d.id not in actions]
        if not remaining:
            emit(actions)
            return

        busy = [a for a in actions.values() if a.start <= t < a.end]
        free = MAX_PARALLEL - len(busy)
        ready = [
            d
            for d in remaining
            if all(
                consumer_id in actions and actions[consumer_id].end <= t
                for consumer_id in consumers[d.id]
            )
        ]
        ready.sort(key=lambda d: order[d.id])

        next_end: Optional[int] = None
        for action in actions.values():
            if action.end > t and (next_end is None or action.end < next_end):
                next_end = action.end

        # Option 1: issue one or more ready stop commands right now.
        if free > 0 and ready:
            for count in range(1, min(free, len(ready)) + 1):
                for combo in combinations(ready, count):
                    extended = dict(actions)
                    for device in combo:
                        extended[device.id] = Action(
                            device.id, t, t + device.shutdown_min, False
                        )
                    following = min(
                        a.end for a in extended.values() if a.end > t
                    )
                    dfs(following, extended)

        # Option 2: deliberately leave slots idle until the next completion.
        # (Kept for exhaustive optimality; it is usually energy-inferior.)
        if next_end is not None:
            dfs(next_end, actions)

    dfs(now, dict(frozen))
    return list(results.values())


# ---------------------------------------------------------------------------
# Public planning entry point
# ---------------------------------------------------------------------------


def compute_plan(
    devices: Sequence[Device],
    residual_wh: float,
    *,
    now: int = 0,
    frozen: Optional[Sequence[Action]] = None,
) -> Plan:
    """Compute the optimal drill plan from ``now`` given current residual energy."""

    devices = normalise_devices(devices)
    if residual_wh < 0:
        raise PlanError("UPS 余电不能为负")
    if now < 0:
        raise PlanError("演练时刻不能为负")

    consumers = _consumers(devices)
    frozen_by_id = _validate_frozen(devices, frozen or [], now, consumers)
    candidates = _enumerate(devices, now, frozen_by_id)
    if not candidates:
        raise PlanError("无法生成停机方案")

    candidates.sort(key=lambda c: (c.makespan, c.energy_wh, c.signature))
    feasible = [c for c in candidates if c.energy_wh <= residual_wh + EPS]
    min_energy = min(c.energy_wh for c in candidates)

    if feasible:
        chosen = feasible[0]
    else:
        # The least-bad reference plan: minimum energy, then earliest finish.
        chosen = min(candidates, key=lambda c: (c.energy_wh, c.makespan, c.signature))

    shortfall = max(0.0, min_energy - residual_wh)
    actions = list(chosen.actions)
    return Plan(
        actions=actions,
        makespan=chosen.makespan,
        energy_needed_wh=chosen.energy_wh,
        budget_wh=residual_wh,
        feasible=chosen.energy_wh <= residual_wh + EPS,
        shortfall_wh=shortfall,
        min_energy_wh=min_energy,
        planning_from=now,
        frozen=[a for a in actions if a.frozen],
    )


# ---------------------------------------------------------------------------
# Energy accounting for the drill clock
# ---------------------------------------------------------------------------


def consumption_between(
    devices: Sequence[Device], plan: Plan, start_minute: int, end_minute: int
) -> float:
    """Energy drawn in [start_minute, end_minute).

    A device draws its power until it is *stopped* (``action.end``), including
    the time it spends waiting for its turn.  Its start time does not matter
    for the power total.
    """
    if end_minute <= start_minute:
        return 0.0
    watt_minutes = 0.0
    for device in devices:
        action = plan.action_for(device.id)
        active = max(0, min(end_minute, action.end) - start_minute)
        watt_minutes += device.power_w * active
    return watt_minutes / 60.0


def residual_at(
    devices: Sequence[Device],
    plan: Plan,
    anchors: Sequence[dict],
    minute: int,
) -> float:
    """Residual energy at ``minute`` using the latest recorded measurement."""
    usable = [a for a in anchors if a["minute"] <= minute]
    if not usable:
        raise PlanError("缺少余电基准")
    anchor = max(usable, key=lambda a: a["minute"])
    consumed = consumption_between(devices, plan, anchor["minute"], minute)
    return max(0.0, anchor["residual_wh"] - consumed)


def timeline(
    devices: Sequence[Device],
    plan: Plan,
    anchors: Sequence[dict],
    *,
    horizon: int,
) -> List[dict]:
    """Minute-by-minute residual energy / draw for charts and drill playback."""
    points: List[dict] = []
    for minute in range(0, max(horizon, 1) + 1):
        residual = residual_at(devices, plan, anchors, minute)
        draw = sum(
            d.power_w
            for d in devices
            if plan.action_for(d.id).start <= minute < plan.action_for(d.id).end
        )
        points.append(
            {
                "minute": minute,
                "draw_w": draw,
                "residual_wh": round(residual, 3),
                "active": [
                    d.id
                    for d in devices
                    if plan.action_for(d.id).start <= minute < plan.action_for(d.id).end
                ],
            }
        )
    return points


def device_state(
    devices: Sequence[Device], plan: Plan, minute: int
) -> List[dict]:
    """State of each device at the boundary ``minute``.

    A device with an action ending at or before ``minute`` is fully stopped;
    one whose action started strictly before ``minute`` is mid-shutdown; one
    that starts exactly at ``minute`` is about to start (``starting``).
    """
    states: List[dict] = []
    for d in devices:
        action = plan.action_for(d.id)
        if minute >= action.end:
            status = "stopped"
        elif minute > action.start:
            status = "stopping"
        elif minute == action.start:
            status = "starting"
        else:
            status = "waiting"
        states.append(
            {
                "id": d.id,
                "name": d.name,
                "power_w": d.power_w,
                "shutdown_min": d.shutdown_min,
                "depends_on": list(d.depends_on),
                "start": action.start,
                "end": action.end,
                "frozen": action.frozen,
                "status": status,
            }
        )
    return states


# ---------------------------------------------------------------------------
# Built-in sample
# ---------------------------------------------------------------------------


def sample_devices() -> List[Device]:
    return [
        Device("web", "Web 应用服务器", 450, 3, ("db",)),
        Device("app", "业务应用服务器", 420, 3, ("db",)),
        Device("db", "数据库服务器", 600, 4, ("storage",)),
        Device("backup", "备份服务器", 300, 2, ("storage",)),
        Device("storage", "共享存储", 800, 5),
    ]


SAMPLE_ENERGY_WH = 320.0
SAMPLE_SHORTFALL_ENERGY_WH = 250.0
