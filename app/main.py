"""FastAPI application: UPS-powered safe shutdown drill for shared storage."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .planner import (
    Action,
    Device,
    Plan,
    PlanError,
    compute_plan,
    device_state,
    sample_devices,
    SAMPLE_ENERGY_WH,
    SAMPLE_SHORTFALL_ENERGY_WH,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="UPS 安全停机演练",
    description="机房断电后，UPS 余电能否支撑共享存储安全停机？",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DeviceIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=60)
    power_w: int = Field(..., ge=0, le=1_000_000)
    shutdown_min: int = Field(..., ge=1, le=240)
    depends_on: List[str] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def unique_deps(cls, value: List[str]) -> List[str]:
        if len(set(value)) != len(value):
            raise ValueError("依赖列表有重复项")
        return value

    def to_device(self) -> Device:
        return Device(
            id=self.id,
            name=self.name.strip(),
            power_w=self.power_w,
            shutdown_min=self.shutdown_min,
            depends_on=tuple(self.depends_on),
        )


class DrillCreate(BaseModel):
    devices: List[DeviceIn] = Field(..., min_length=1, max_length=6)
    residual_wh: float = Field(..., ge=0, le=1_000_000)


class DrillRevise(BaseModel):
    residual_wh: float = Field(..., ge=0, le=1_000_000)


class DrillAdvance(BaseModel):
    minutes: int = Field(1, ge=1, le=2000)
    to_next_event: bool = False


class PreviewRequest(BaseModel):
    devices: List[DeviceIn] = Field(..., min_length=1, max_length=6)
    residual_wh: float = Field(..., ge=0, le=1_000_000)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    devices: List[Device]
    plan: Plan
    now: int
    residual_wh: float
    issued: List[Action] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    revision: Optional[dict] = None
    revision_count: int = 0
    created: float = field(default_factory=time.time)
    version: int = 0

    @property
    def feasible(self) -> bool:
        return self.plan.feasible

    @property
    def complete(self) -> bool:
        return self.feasible and self.now >= self.plan.makespan

    @property
    def status(self) -> str:
        if not self.feasible:
            return "shortage"
        if self.complete:
            return "complete"
        if self.now == 0:
            return "ready"
        return "running"

    def draw_at(self, minute: int) -> int:
        """Power still being drawn at ``minute`` (waiting + stopping devices)."""
        return sum(
            d.power_w
            for d in self.devices
            if self.plan.action_for(d.id).end > minute
        )

    def log(self, kind: str, **payload) -> None:
        self.events.append({"at": self.now, "kind": kind, **payload})
        self.events = self.events[-100:]

    def snapshot(self) -> dict:
        return {
            "minute": self.now,
            "residual_wh": round(max(0.0, self.residual_wh), 3),
            "draw_w": self.draw_at(self.now) if self.feasible else 0,
        }


_SESSIONS: "OrderedDict[str, Session]" = OrderedDict()
_LOCK = threading.Lock()
_MAX_SESSIONS = 200


def _store(session: Session) -> None:
    with _LOCK:
        _SESSIONS[session.id] = session
        _SESSIONS.move_to_end(session.id)
        while len(_SESSIONS) > _MAX_SESSIONS:
            _SESSIONS.popitem(last=False)


def _get(session_id: str) -> Session:
    with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="演练会话不存在或已过期")
    return session


def _plan_for(
    devices: List[Device], residual: float, now: int, issued: List[Action]
) -> Plan:
    try:
        return compute_plan(devices, residual, now=now, frozen=issued)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _projection(session: Session) -> List[dict]:
    """Residual-energy curve from the current minute to the last stop."""
    series: List[dict] = []
    if not session.feasible:
        return series
    remaining = session.residual_wh
    for minute in range(session.now, max(session.plan.makespan, session.now) + 1):
        draw = session.draw_at(minute)
        series.append(
            {
                "minute": minute,
                "residual_wh": round(max(0.0, remaining), 3),
                "draw_w": draw,
            }
        )
        remaining -= draw / 60.0
    return series


def _remaining_need(session: Session) -> float:
    """Energy still required from the current minute until the last stop."""
    if not session.feasible:
        return 0.0
    watt_minutes = sum(
        d.power_w * max(0, session.plan.action_for(d.id).end - session.now)
        for d in session.devices
    )
    return watt_minutes / 60.0


def _state(session: Session) -> dict:
    plan = session.plan
    upcoming = sorted(
        {a.start for a in plan.actions if a.start > session.now}
        | {a.end for a in plan.actions if a.end > session.now}
    )
    return {
        "id": session.id,
        "status": session.status,
        "now": session.now,
        "version": session.version,
        "feasible": session.feasible,
        "residual_wh": round(max(0.0, session.residual_wh), 3),
        "shortfall_wh": round(plan.shortfall_wh, 3) if not session.feasible else 0.0,
        "required_wh": round(
            plan.min_energy_wh if not session.feasible else _remaining_need(session), 3
        ),
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "power_w": d.power_w,
                "shutdown_min": d.shutdown_min,
                "depends_on": list(d.depends_on),
            }
            for d in session.devices
        ],
        "plan": plan.as_dict(),
        "device_states": device_state(session.devices, plan, session.now),
        "issued": [a.as_dict() for a in session.issued],
        "next_event_minute": upcoming[0] if upcoming else None,
        "can_advance": session.feasible and not session.complete,
        "history": session.history,
        "projection": _projection(session),
        "revision": session.revision,
        "revision_count": session.revision_count,
        "events": session.events,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    with _LOCK:
        count = len(_SESSIONS)
    return {"ok": True, "sessions": count}


@app.get("/api/sample")
def sample() -> dict:
    devices = sample_devices()
    plan = compute_plan(devices, SAMPLE_ENERGY_WH)
    tight = compute_plan(devices, SAMPLE_SHORTFALL_ENERGY_WH)
    return {
        "devices": [d.as_dict() for d in devices],
        "residual_wh": SAMPLE_ENERGY_WH,
        "tight_residual_wh": SAMPLE_SHORTFALL_ENERGY_WH,
        "note": (
            "断电后，Web/业务应用先停机，随后数据库与备份服务器，最后共享存储。"
            "样例余电足以完成安全停机；把余电改成 250 Wh 可看到缺口演示。"
        ),
        "expected": {
            "makespan": plan.makespan,
            "energy_needed_wh": round(plan.energy_needed_wh, 3),
            "order": plan.order,
            "shortfall_at_tight_wh": round(tight.shortfall_wh, 3),
        },
    }


@app.post("/api/preview")
def preview(payload: PreviewRequest) -> dict:
    devices = [d.to_device() for d in payload.devices]
    try:
        plan = compute_plan(devices, payload.residual_wh, now=0)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "plan": plan.as_dict(),
        "device_states": device_state(devices, plan, 0),
        "can_advance": plan.feasible,
        "shortfall_wh": round(plan.shortfall_wh, 3),
        "required_wh": round(plan.min_energy_wh, 3),
    }


@app.post("/api/drills", status_code=201)
def create_drill(payload: DrillCreate) -> dict:
    devices = [d.to_device() for d in payload.devices]
    try:
        plan = compute_plan(devices, payload.residual_wh, now=0)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = Session(
        id=uuid.uuid4().hex,
        devices=devices,
        plan=plan,
        now=0,
        residual_wh=payload.residual_wh,
    )
    session.history.append(session.snapshot())
    session.log(
        "create",
        feasible=plan.feasible,
        residual_wh=round(payload.residual_wh, 3),
        shortfall_wh=round(plan.shortfall_wh, 3),
    )
    _store(session)
    return _state(session)


@app.get("/api/drills/{session_id}")
def get_drill(session_id: str) -> dict:
    return _state(_get(session_id))


@app.post("/api/drills/{session_id}/advance")
def advance_drill(session_id: str, payload: DrillAdvance) -> dict:
    session = _get(session_id)
    if not session.feasible:
        raise HTTPException(
            status_code=409,
            detail=(
                f"UPS 余电不足 {session.plan.shortfall_wh:.1f} Wh，"
                "请先补录当前余电再推进"
            ),
        )
    if session.complete:
        return _state(session)

    if payload.to_next_event:
        candidates = [a.start for a in session.plan.actions if a.start > session.now] + [
            a.end for a in session.plan.actions if a.end > session.now
        ]
        target = min(candidates) if candidates else session.plan.makespan
    else:
        target = session.now + payload.minutes
    target = min(target, session.plan.makespan)

    step_energy = 0.0
    while session.now < target:
        draw = session.draw_at(session.now)
        step_energy += draw / 60.0
        session.residual_wh = max(0.0, session.residual_wh - draw / 60.0)
        session.now += 1
        session.history.append(session.snapshot())

    # Commands whose start minute has been reached are now immutable; the rest
    # of the plan is intentionally left untouched until the operator revises
    # the residual energy (that is what triggers a re-plan).
    if session.now > 0:
        issued = {
            a.device_id: Action(a.device_id, a.start, a.end, True)
            for a in session.plan.actions
            if a.start <= session.now
        }
        session.issued = sorted(issued.values(), key=lambda a: (a.start, a.device_id))
    session.version += 1
    if session.complete:
        session.log("complete", residual_wh=round(session.residual_wh, 3))
    else:
        session.log("advance", to=session.now, spent_wh=round(step_energy, 3))
    return _state(session)


@app.post("/api/drills/{session_id}/revise")
def revise_drill(session_id: str, payload: DrillRevise) -> dict:
    session = _get(session_id)
    old_plan = session.plan
    issued_ids = {a.device_id for a in session.issued}
    new_plan = _plan_for(
        session.devices, payload.residual_wh, session.now, session.issued
    )

    preserved = []
    rescheduled = []
    for action in new_plan.actions:
        old = old_plan.action_for(action.device_id)
        entry = {
            "device_id": action.device_id,
            "name": next(
                d.name for d in session.devices if d.id == action.device_id
            ),
            "start": action.start,
            "end": action.end,
            "old_start": old.start,
            "old_end": old.end,
            "status": "stopped"
            if action.end <= session.now
            else ("running" if action.start < session.now else "waiting"),
        }
        if action.device_id in issued_ids:
            preserved.append(entry)
        elif (old.start, old.end) != (action.start, action.end):
            rescheduled.append(entry)

    session.residual_wh = payload.residual_wh
    session.plan = new_plan
    session.revision_count += 1
    session.version += 1
    session.revision = {
        "count": session.revision_count,
        "at_minute": session.now,
        "reported_residual_wh": round(payload.residual_wh, 3),
        "preserved": preserved,
        "rescheduled": rescheduled,
        "feasible": new_plan.feasible,
        "shortfall_wh": round(new_plan.shortfall_wh, 3),
        "required_wh": round(
            new_plan.energy_needed_wh if new_plan.feasible else new_plan.min_energy_wh,
            3,
        ),
        "preserved_finish_times": all(
            new_plan.action_for(a.device_id).end == a.end for a in session.issued
        ),
        "all_frozen": len(issued_ids) > 0
        and all(a.frozen for a in new_plan.actions if a.device_id in issued_ids),
    }
    session.history.append(session.snapshot())
    session.log(
        "revise",
        residual_wh=round(payload.residual_wh, 3),
        preserved=len(preserved),
        rescheduled=len(rescheduled),
        feasible=new_plan.feasible,
        shortfall_wh=round(new_plan.shortfall_wh, 3),
    )
    return _state(session)


@app.delete("/api/drills/{session_id}")
def delete_drill(session_id: str) -> dict:
    with _LOCK:
        existed = _SESSIONS.pop(session_id, None) is not None
    if not existed:
        raise HTTPException(status_code=404, detail="演练会话不存在或已过期")
    return {"deleted": True}
