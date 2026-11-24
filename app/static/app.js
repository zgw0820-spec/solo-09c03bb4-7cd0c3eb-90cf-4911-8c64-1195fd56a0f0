"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  devices: [],          // editor rows: {id, name, power_w, shutdown_min, depends_on[]}
  session: null,        // last drill snapshot from the API
  preview: null,        // last preview payload
  busy: false,
};

const MAX_DEVICES = 6;
const PALETTE = ["#6ea8ff", "#22d3a6", "#ffb020", "#c084fc", "#f472b6", "#38bdf8"];

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    let detail = payload && payload.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((d) => `${d.loc ? d.loc.join(".") : ""} ${d.msg}`).join("；");
    }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

function uniqueId(base) {
  const taken = new Set(state.devices.map((d) => d.id));
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}-${n}`)) n += 1;
  return `${base}-${n}`;
}

function fmt(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(digits).replace(/\.0+$/, "");
}

function deviceName(id) {
  const device =
    state.devices.find((d) => d.id === id) ||
    (state.session && state.session.devices.find((d) => d.id === id));
  return device ? device.name : id;
}

function showError(message) {
  const box = $("setup-error");
  if (message) {
    box.textContent = message;
    box.hidden = false;
  } else {
    box.hidden = true;
    box.textContent = "";
  }
}

// ---------------------------------------------------------------------------
// Device editor
// ---------------------------------------------------------------------------

function emptyDevice() {
  const n = state.devices.length + 1;
  return {
    id: uniqueId(`dev-${n}`),
    name: `设备 ${n}`,
    power_w: 300,
    shutdown_min: 3,
    depends_on: [],
  };
}

function addDevice(device) {
  if (state.devices.length >= MAX_DEVICES) return;
  state.devices.push(device);
  renderEditor();
}

function removeDevice(index) {
  const removed = state.devices[index];
  state.devices.splice(index, 1);
  state.devices.forEach((d) => {
    d.depends_on = d.depends_on.filter((id) => id !== removed.id);
  });
  renderEditor();
}

function renderEditor() {
  const tbody = $("device-rows");
  tbody.innerHTML = "";

  state.devices.forEach((device, index) => {
    const tr = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.className = "cell-name";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = device.name;
    nameInput.maxLength = 60;
    nameInput.addEventListener("input", () => {
      device.name = nameInput.value;
      refreshDepLabels();
    });
    nameInput.addEventListener("change", () => {
      if (!device.name.trim()) {
        device.name = `设备 ${index + 1}`;
        nameInput.value = device.name;
      }
    });
    nameCell.appendChild(nameInput);
    tr.appendChild(nameCell);

    tr.appendChild(numberCell(device, "power_w", 0, 1000000, 10));
    tr.appendChild(numberCell(device, "shutdown_min", 1, 240, 1));

    const depCell = document.createElement("td");
    depCell.className = "dep-cell";
    const list = document.createElement("div");
    list.className = "dep-list";
    state.devices.forEach((other) => {
      if (other.id === device.id) return;
      const label = document.createElement("label");
      label.className = "dep-option";
      label.dataset.deviceId = other.id;
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = device.depends_on.includes(other.id);
      box.addEventListener("change", () => {
        if (box.checked) {
          if (!device.depends_on.includes(other.id)) device.depends_on.push(other.id);
        } else {
          device.depends_on = device.depends_on.filter((id) => id !== other.id);
        }
      });
      label.appendChild(box);
      label.appendChild(document.createTextNode(other.name));
      list.appendChild(label);
    });
    if (!list.childElementCount) {
      const empty = document.createElement("span");
      empty.className = "tag waiting";
      empty.textContent = "无其他设备";
      list.appendChild(empty);
    }
    depCell.appendChild(list);
    tr.appendChild(depCell);

    const actionCell = document.createElement("td");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn ghost danger";
    remove.textContent = "移除";
    remove.disabled = state.devices.length <= 1;
    remove.addEventListener("click", () => removeDevice(index));
    actionCell.appendChild(remove);
    tr.appendChild(actionCell);

    tbody.appendChild(tr);
  });

  $("btn-add-device").disabled = state.devices.length >= MAX_DEVICES;
}

function refreshDepLabels() {
  document.querySelectorAll("#device-rows tr").forEach((row, index) => {
    const device = state.devices[index];
    if (!device) return;
    row.querySelectorAll(".dep-option").forEach((label) => {
      const otherId = label.dataset.deviceId;
      const other = state.devices.find((d) => d.id === otherId);
      if (other) label.lastChild.textContent = other.name;
    });
  });
}

function numberCell(device, key, min, max, step) {
  const td = document.createElement("td");
  const input = document.createElement("input");
  input.type = "number";
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = device[key];
  input.addEventListener("input", () => {
    const parsed = Number(input.value);
    if (!Number.isNaN(parsed)) device[key] = parsed;
  });
  input.addEventListener("blur", () => {
    let value = Math.round(Number(input.value));
    if (Number.isNaN(value)) value = device[key];
    value = Math.min(max, Math.max(min, value));
    device[key] = value;
    input.value = value;
  });
  td.appendChild(input);
  return td;
}

function payloadFromEditor() {
  return {
    residual_wh: Number($("residual-input").value),
    devices: state.devices.map((d) => ({
      id: d.id,
      name: d.name.trim(),
      power_w: Math.round(Number(d.power_w)),
      shutdown_min: Math.round(Number(d.shutdown_min)),
      depends_on: d.depends_on.slice(),
    })),
  };
}

// ---------------------------------------------------------------------------
// Setup actions
// ---------------------------------------------------------------------------

async function loadSample() {
  showError("");
  try {
    const sample = await api("/api/sample");
    state.devices = sample.devices.map((d) => ({
      id: d.id,
      name: d.name,
      power_w: d.power_w,
      shutdown_min: d.shutdown_min,
      depends_on: d.depends_on.slice(),
    }));
    $("residual-input").value = sample.residual_wh;
    renderEditor();
    await runPreview();
  } catch (err) {
    showError(err.message);
  }
}

async function runPreview() {
  showError("");
  try {
    const payload = payloadFromEditor();
    const result = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.preview = result;
    renderPlan(result);
    $("plan-panel").hidden = false;
    $("plan-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showError(err.message);
    $("plan-panel").hidden = true;
  }
}

async function startDrill(event) {
  event.preventDefault();
  if (state.busy) return;
  state.busy = true;
  showError("");
  $("btn-start").disabled = true;
  try {
    const payload = payloadFromEditor();
    const session = await api("/api/drills", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.session = session;
    renderSession();
    $("drill-panel").hidden = false;
    $("drill-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showError(err.message);
  } finally {
    state.busy = false;
    $("btn-start").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Plan rendering (preview + current session)
// ---------------------------------------------------------------------------

function renderPlan(result, { live = false } = {}) {
  const plan = result.plan;
  const budget = live ? result.residual_wh : plan.budget_wh;
  // Live sessions know the energy still needed from the current minute; the
  // plan's own figure is measured from the last planning instant.
  const needed = live && result.required_wh != null ? result.required_wh : plan.energy_needed_wh;
  const gap = budget - needed;

  const badge = $("plan-badge");
  badge.hidden = false;
  if (plan.feasible) {
    badge.className = "badge ok";
    badge.textContent = "余电够用";
  } else {
    badge.className = "badge bad";
    badge.textContent = "余电不足";
  }

  const makespan = plan.makespan;
  const lastEnd = plan.actions.reduce((m, a) => Math.max(m, a.end), 0);
  $("plan-metrics").innerHTML = `
    <div class="metric"><span>${live ? "预计全部停妥于" : "全部停妥用时"}</span>
      <strong>${makespan} 分钟</strong></div>
    <div class="metric"><span>${live ? "后续还需电量" : "需要电量"}</span>
      <strong>${fmt(needed)} Wh</strong></div>
    <div class="metric"><span>${live ? "当前余电" : "可用余电"}</span>
      <strong>${fmt(budget)} Wh</strong></div>
    <div class="metric"><span>${plan.feasible ? (gap >= 0 ? "停妥后剩余" : "余量") : "最小缺口"}</span>
      <strong style="color:${plan.feasible ? "var(--accent-2)" : "var(--danger)"}">
        ${fmt(plan.feasible ? gap : plan.shortfall_wh)} Wh
      </strong></div>`;

  const verdict = $("plan-verdict");
  verdict.classList.toggle("bad", !plan.feasible);
  if (plan.feasible) {
    verdict.innerHTML =
      `余电够用：按此时序 <strong>${makespan} 分钟</strong>内全部设备停妥，` +
      `共耗电 <strong>${fmt(needed)} Wh</strong>，` +
      `停妥后还剩 <strong>${fmt(gap)} Wh</strong>。` +
      `停机顺序：${plan.order.map(deviceName).join(" → ")}。`;
  } else {
    verdict.innerHTML =
      `余电不足：最快方案也需 <strong>${fmt(plan.min_energy_wh)} Wh</strong>，` +
      `当前余电 <strong>${fmt(budget)} Wh</strong>，` +
      `最小缺口 <strong>${fmt(plan.shortfall_wh)} Wh</strong>。` +
      `补录新余电后才能推进演练。`;
  }

  renderGantt(plan, lastEnd);
  renderPlanRows(result.device_states, plan);
}

function renderGantt(plan, lastEnd) {
  const container = $("gantt");
  container.innerHTML = "";
  const span = Math.max(lastEnd, 1);

  const byId = new Map(plan.actions.map((a) => [a.device_id, a]));
  const rows = [...byId.values()].sort((a, b) => a.start - b.start || a.end - b.end);

  rows.forEach((action) => {
    const row = document.createElement("div");
    row.className = "gantt-row";

    const label = document.createElement("div");
    label.className = "gantt-label";
    label.textContent = deviceName(action.device_id);
    label.title = action.device_id;
    row.appendChild(label);

    const track = document.createElement("div");
    track.className = "gantt-track";

    if (action.start > 0) {
      const wait = document.createElement("div");
      wait.className = "gantt-bar wait-zone";
      wait.style.left = "0%";
      wait.style.width = `${(action.start / span) * 100}%`;
      wait.title = `等待 ${action.start} 分钟（仍按功率耗电）`;
      track.appendChild(wait);
    }

    const bar = document.createElement("div");
    bar.className = "gantt-bar" + (action.frozen ? " frozen" : "");
    bar.style.left = `${(action.start / span) * 100}%`;
    bar.style.width = `${((action.end - action.start) / span) * 100}%`;
    bar.textContent = `${action.start}→${action.end}`;
    bar.title = `${deviceName(action.device_id)}: ${action.start} 分钟开始，${action.end} 分钟停妥`;
    if (action.frozen) {
      bar.style.background = "linear-gradient(135deg, #7ff0d2, #22d3a6)";
    } else {
      const index = state.devices.findIndex((d) => d.id === action.device_id);
      const color = PALETTE[(index + PALETTE.length) % PALETTE.length];
      bar.style.background = `linear-gradient(135deg, ${color}, ${color}cc)`;
    }
    track.appendChild(bar);
    row.appendChild(track);
    container.appendChild(row);
  });

  const axis = document.createElement("div");
  axis.className = "gantt-axis";
  const ticks = span <= 16 ? span : 8;
  for (let i = 0; i <= ticks; i += 1) {
    const minute = Math.round((span / ticks) * i);
    const tick = document.createElement("span");
    tick.className = "gantt-tick";
    tick.style.left = `${(minute / span) * 100}%`;
    tick.textContent = String(minute);
    axis.appendChild(tick);
  }
  container.appendChild(axis);
}

function renderPlanRows(states, plan) {
  const tbody = $("plan-rows");
  tbody.innerHTML = "";
  states.forEach((s) => {
    const tr = document.createElement("tr");
    const deps = s.depends_on.length
      ? s.depends_on.map(deviceName).join("、")
      : "—";
    const statusMap = {
      waiting: ["waiting", "未开始"],
      starting: ["starting", "即将开始"],
      stopping: ["stopping", "停机中"],
      stopped: ["stopped", "已停妥"],
    };
    const [cls, text] = statusMap[s.status] || ["waiting", s.status];
    tr.innerHTML = `
      <td>${escapeHtml(s.name)}</td>
      <td class="mono">${fmt(s.power_w, 0)} W</td>
      <td class="mono">${s.shutdown_min} 分钟</td>
      <td class="mono">${s.start}</td>
      <td class="mono">${s.end}</td>
      <td>${escapeHtml(deps)}</td>
      <td><span class="tag ${cls}">${text}${s.frozen ? " · 已下达" : ""}</span></td>`;
    tbody.appendChild(tr);
  });
}

// ---------------------------------------------------------------------------
// Session / playback
// ---------------------------------------------------------------------------

function renderSession() {
  const session = state.session;
  if (!session) return;

  $("clock").textContent = String(session.now);
  $("residual-now").textContent = fmt(session.residual_wh);
  $("required-wh").textContent = fmt(session.required_wh);
  $("draw-now").textContent = fmt(session.plan.feasible
    ? session.device_states
        .filter((d) => d.status !== "stopped")
        .reduce((sum, d) => sum + d.power_w, 0)
    : 0, 0);

  const budget = session.plan.budget_wh || 1;
  const pct = Math.max(0, Math.min(100, (session.residual_wh / budget) * 100));
  const fill = $("energy-fill");
  fill.style.width = `${pct}%`;
  fill.className = "energy-fill" + (pct <= 5 ? " empty" : pct <= 25 ? " low" : "");

  renderPlan(
    {
      plan: session.plan,
      device_states: session.device_states,
      residual_wh: session.residual_wh,
      required_wh: session.required_wh,
    },
    { live: true },
  );
  renderLiveDevices(session);
  renderChart(session);
  renderHistory(session);
  renderEvents(session);
  renderShortage(session);
  renderRevision(session);

  const finished = session.status === "complete";
  $("btn-step").disabled = !session.can_advance || state.busy;
  $("btn-next").disabled = !session.can_advance || state.busy;
  $("btn-finish").disabled = !session.can_advance || state.busy || finished;
  $("btn-revise").disabled = state.busy;
  $("revise-input").value = fmt(session.residual_wh, 1);

  if (finished) {
    $("clock").textContent = `${session.now}（已全部停妥）`;
  }
}

function renderLiveDevices(session) {
  const grid = $("live-devices");
  grid.innerHTML = "";
  session.device_states.forEach((s) => {
    const card = document.createElement("div");
    card.className = `device-card ${s.status}`;
    const total = Math.max(1, s.end - s.start);
    const elapsed = Math.max(0, Math.min(total, session.now - s.start));
    const progress = s.status === "stopped" ? 100 : (elapsed / total) * 100;
    const statusText = {
      waiting: "未开始",
      starting: "即将开始",
      stopping: "停机中",
      stopped: "已停妥",
    }[s.status];
    card.innerHTML = `
      ${s.frozen ? '<span class="frozen-flag">命令已下达</span>' : ""}
      <h4>${escapeHtml(s.name)}</h4>
      <div class="meta">
        ${fmt(s.power_w, 0)} W · 停机 ${s.shutdown_min} 分钟<br />
        ${s.start} → ${s.end} 分钟 · ${statusText}
      </div>
      <div class="progress"><div style="width:${progress}%"></div></div>`;
    grid.appendChild(card);
  });
}

function renderChart(session) {
  const svg = $("chart");
  const W = 720;
  const H = 260;
  const pad = { top: 18, right: 16, bottom: 28, left: 48 };

  const history = session.history || [];
  const projection = session.plan.feasible ? session.projection || [] : [];
  const points = history.concat(
    projection.filter((p) => p.minute > (history.length ? history[history.length - 1].minute : -1)),
  );

  svg.innerHTML = "";
  if (!points.length) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" fill="#93a1c4" font-size="13"
      text-anchor="middle">余电不足，无法生成走势；请补录后重排</text>`;
    return;
  }

  const xMax = Math.max(1, points[points.length - 1].minute);
  const yMax = Math.max(1, Math.max(...points.map((p) => p.residual_wh), session.plan.budget_wh));

  const x = (m) => pad.left + (m / xMax) * (W - pad.left - pad.right);
  const y = (v) => H - pad.bottom - (v / yMax) * (H - pad.top - pad.bottom);

  const gridLines = [0, 0.25, 0.5, 0.75, 1];
  gridLines.forEach((frac) => {
    const value = yMax * frac;
    const yy = y(value);
    svg.insertAdjacentHTML(
      "beforeend",
      `<line x1="${pad.left}" y1="${yy}" x2="${W - pad.right}" y2="${yy}"
         stroke="#1f2a45" stroke-width="1" />
       <text x="${pad.left - 8}" y="${yy + 3}" fill="#7c8db5" font-size="10"
         text-anchor="end">${fmt(value, 0)}</text>`,
    );
  });

  const xTicks = 6;
  for (let i = 0; i <= xTicks; i += 1) {
    const minute = Math.round((xMax / xTicks) * i);
    svg.insertAdjacentHTML(
      "beforeend",
      `<text x="${x(minute)}" y="${H - 8}" fill="#7c8db5" font-size="10"
         text-anchor="middle">${minute}m</text>`,
    );
  }

  if (projection.length) {
    const projPath = projection
      .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.minute).toFixed(1)},${y(p.residual_wh).toFixed(1)}`)
      .join(" ");
    svg.insertAdjacentHTML(
      "beforeend",
      `<path d="${projPath}" fill="none" stroke="#4f8cff" stroke-width="2"
         stroke-dasharray="5 5" opacity="0.75" />`,
    );
  }

  const solidPoints = history.filter((p) => p.minute <= session.now);
  if (solidPoints.length) {
    const area = solidPoints
      .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.minute).toFixed(1)},${y(p.residual_wh).toFixed(1)}`)
      .join(" ");
    const areaFill = `${area} L${x(solidPoints[solidPoints.length - 1].minute).toFixed(1)},${y(0).toFixed(1)} L${x(solidPoints[0].minute).toFixed(1)},${y(0).toFixed(1)} Z`;
    svg.insertAdjacentHTML(
      "beforeend",
      `<path d="${areaFill}" fill="rgba(34,211,166,0.12)" />
       <path d="${area}" fill="none" stroke="#22d3a6" stroke-width="2.4" />`,
    );

    const last = solidPoints[solidPoints.length - 1];
    svg.insertAdjacentHTML(
      "beforeend",
      `<circle cx="${x(last.minute)}" cy="${y(last.residual_wh)}" r="4.5"
         fill="#22d3a6" stroke="#0d1528" stroke-width="2" />
       <line x1="${x(session.now)}" y1="${pad.top}" x2="${x(session.now)}"
         y2="${H - pad.bottom}" stroke="#4f8cff" stroke-width="1" stroke-dasharray="3 3" opacity="0.7" />`,
    );
  }

  svg.insertAdjacentHTML(
    "beforeend",
    `<text x="${pad.left}" y="12" fill="#93a1c4" font-size="10">Wh</text>`,
  );
}

function renderHistory(session) {
  const tbody = $("history-rows");
  tbody.innerHTML = "";
  const rows = session.history.slice().reverse().slice(0, 80);
  rows.forEach((h) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${h.minute}</td><td>${fmt(h.residual_wh)}</td><td>${fmt(h.draw_w, 0)}</td>`;
    tbody.appendChild(tr);
  });
}

function renderEvents(session) {
  const list = $("event-list");
  list.innerHTML = "";
  const events = session.events.slice().reverse();
  events.forEach((e) => {
    const li = document.createElement("li");
    let text = "";
    switch (e.kind) {
      case "create":
        text = e.feasible
          ? `开始演练，余电 ${fmt(e.residual_wh)} Wh，余电够用`
          : `开始演练，余电 ${fmt(e.residual_wh)} Wh，缺口 ${fmt(e.shortfall_wh)} Wh`;
        break;
      case "advance":
        text = `推进到第 ${e.to} 分钟`;
        break;
      case "revise":
        text = `补录余电 ${fmt(e.residual_wh)} Wh：保留 ${e.preserved} 条已下达命令，重排 ${e.rescheduled} 条` +
          (e.feasible ? "，余电够用" : `，仍缺 ${fmt(e.shortfall_wh)} Wh`);
        break;
      case "complete":
        text = `全部设备停妥，剩余 ${fmt(e.residual_wh)} Wh`;
        break;
      default:
        text = e.kind;
    }
    li.innerHTML = `<span class="t">${e.at}m</span>${escapeHtml(text)}`;
    list.appendChild(li);
  });
}

function renderShortage(session) {
  const box = $("shortage-box");
  if (session.feasible) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = `
    <strong>余电不足，最小缺口 ${fmt(session.shortfall_wh)} Wh。</strong><br />
    完成全部安全停机至少需要 <strong>${fmt(session.required_wh)} Wh</strong>，
    当前记录 <strong>${fmt(session.residual_wh)} Wh</strong>。<br />
    演练已暂停；请核对 UPS 读数后在下方补录，系统会保留已下达命令的完成时刻并重排其余动作。`;
}

function renderRevision(session) {
  const box = $("revision-box");
  const rev = session.revision;
  if (!rev) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const preserved = rev.preserved
    .map(
      (p) =>
        `${escapeHtml(p.name)}：${p.start}–${p.end} 分钟${
          p.old_start !== p.start || p.old_end !== p.end ? "（已调整！）" : ""
        }`,
    )
    .join("<br />");
  const rescheduled = rev.rescheduled.length
    ? rev.rescheduled
        .map(
          (r) =>
            `${escapeHtml(r.name)}：${r.old_start}–${r.old_end} → ${r.start}–${r.end} 分钟`,
        )
        .join("<br />")
    : "无（所有未开始设备仍按原计划）";
  const preservedOk = rev.preserved_finish_times;
  box.innerHTML = `
    <strong>第 ${rev.count} 次补录</strong>（第 ${rev.at_minute} 分钟，记录 ${fmt(rev.reported_residual_wh)} Wh）
    <div style="margin-top:6px">已下达命令（完成时刻保留）：
      <span class="${preservedOk ? "ok" : "bad"}">${preservedOk ? "✓ 全部保留" : "✗ 有变更"}</span>
    </div>
    <div>${preserved || "无"}</div>
    <div style="margin-top:6px">尚未开始、被重排的动作：</div>
    <div>${rescheduled}</div>
    <div style="margin-top:6px">
      重排后：${rev.feasible
        ? `<span class="ok">余电够用，需要 ${fmt(rev.required_wh)} Wh</span>`
        : `<span class="bad">仍缺 ${fmt(rev.shortfall_wh)} Wh（至少需要 ${fmt(rev.required_wh)} Wh）</span>`}
    </div>`;
}

// ---------------------------------------------------------------------------
// Drill actions
// ---------------------------------------------------------------------------

async function advance(body) {
  if (!state.session || state.busy) return;
  state.busy = true;
  try {
    const session = await api(`/api/drills/${state.session.id}/advance`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.session = session;
    renderSession();
  } catch (err) {
    showError(err.message);
  } finally {
    state.busy = false;
    renderSession();
  }
}

async function revise() {
  if (!state.session || state.busy) return;
  const value = Number($("revise-input").value);
  if (Number.isNaN(value) || value < 0) {
    showError("请输入有效的余电读数（Wh，≥ 0）");
    return;
  }
  state.busy = true;
  showError("");
  $("btn-revise").disabled = true;
  try {
    const session = await api(`/api/drills/${state.session.id}/revise`, {
      method: "POST",
      body: JSON.stringify({ residual_wh: value }),
    });
    state.session = session;
    renderSession();
  } catch (err) {
    showError(err.message);
  } finally {
    state.busy = false;
    renderSession();
  }
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

function bind() {
  $("btn-sample").addEventListener("click", loadSample);
  $("btn-add-device").addEventListener("click", () => addDevice(emptyDevice()));
  $("btn-preview").addEventListener("click", runPreview);
  $("setup-form").addEventListener("submit", startDrill);

  $("btn-step").addEventListener("click", () => advance({ minutes: 1 }));
  $("btn-next").addEventListener("click", () => advance({ to_next_event: true }));
  $("btn-finish").addEventListener("click", () => {
    const remaining = state.session.plan.makespan - state.session.now;
    advance({ minutes: Math.max(1, remaining) });
  });
  $("btn-revise").addEventListener("click", revise);
}

async function init() {
  bind();
  state.devices = [];
  renderEditor();
  try {
    await loadSample();
  } catch {
    /* offline / first run: the editor remains manually usable */
  }
}

document.addEventListener("DOMContentLoaded", init);
