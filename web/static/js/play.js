/**
 * AITown3 Play client — room map, player actions, graph toggle, director.
 */
(function () {
  "use strict";

  const API = "";

  let playState = null;
  let selected = null; // { type: 'person'|'item', name }
  let busy = false;
  let graphMode = false;
  let network = null;
  let nodesDS = null;
  let edgesDS = null;
  let directorBlueprint = null;
  let townLayout = {}; // name -> {x,y,w,h,left,top,...}
  let talkTarget = null; // NPC name pending talk
  let simPollTimer = null;
  let simRunning = false;
  let simMissionLogged = false;
  let simFullRefreshAt = 0;
  let focusedNpc = null;
  let dossierCache = null;
  let dossierTab = "info";
  let dossierRefreshAt = 0;
  let sideTab = "action";

  const ACTION_LABELS = {
    move: "Move",
    talk: "Talk",
    attack: "Attack",
    pick_up: "Pick up",
    drop: "Drop",
    give: "Give",
    use_item: "Use",
    use_on: "Use on target",
    wait: "Wait",
    noop: "Idle",
  };

  function actionLabel(name) {
    if (!name) return "";
    return ACTION_LABELS[name] || name;
  }

  // —— DOM ——
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStatus(msg, isError) {
    const el = $("status");
    el.textContent = msg || "";
    el.className = isError ? "error" : "";
  }

  async function api(path, opts = {}) {
    const res = await fetch(API + path, opts);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function logEvent(text, kind = "ok", extraHtml = "") {
    const box = $("event-log");
    const time = new Date().toLocaleTimeString();
    const div = document.createElement("div");
    div.className = `log-entry ${kind}`;
    div.innerHTML = `<span class="time">${escapeHtml(time)}</span>${escapeHtml(text)}${extraHtml}`;
    box.prepend(div);
  }

  // —— Play state ——
  async function loadPlayState() {
    const data = await api("/api/play/state");
    playState = data;
    if (!data.found) {
      $("gate").classList.add("visible");
      $("player-chip").style.visibility = "hidden";
      renderEmptyMap(data.map);
      return data;
    }
    $("gate").classList.remove("visible");
    $("player-chip").style.visibility = "visible";
    renderHud(data);
    renderRoom(data);
    renderTownMap(data.map, data.current_location?.name, data.adjacent || []);
    renderActions(data);
    renderQuests(data.quests || []);
    renderClues(data.known_clues || []);
    renderSimulation(data.simulation || {});
    renderNpcRoster(data.characters || []);
    fillSimCharacterSelect(data.characters || []);
    if (focusedNpc) {
      refreshDossier(focusedNpc, { quiet: true }).catch(() => {});
    }
    return data;
  }

  function setSideTab(tab) {
    sideTab = tab || "action";
    document.querySelectorAll(".side-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.sideTab === sideTab);
    });
    ["action", "sim", "log"].forEach((id) => {
      const panel = $(`side-panel-${id}`);
      if (panel) panel.classList.toggle("active", id === sideTab);
    });
  }

  function renderSimulation(sim) {
    const meta = $("sim-meta");
    const box = $("npc-activity");
    if (!meta || !box) return;
    const t = typeof sim.sim_time === "number" ? sim.sim_time.toFixed(1) : "0.0";
    const tick = sim.tick || 0;
    const whoSel = $("sel-sim-character");
    const who = (whoSel && whoSel.value) || "";
    const scope = who ? `${who} only` : "All";
    meta.textContent = simRunning
      ? `Simulation running (${scope}) · sim ${t}s · tick ${tick}`
      : `Simulation stopped · sim ${t}s · tick ${tick}`;
    const list = sim.npc_activity || [];
    if (!list.length) {
      box.innerHTML = `<span class="empty">No activity yet (check "Auto-plan player" to include the protagonist)</span>`;
      return;
    }
    box.innerHTML = list
      .map((n) => {
        const goal = n.goal
          ? `${escapeHtml(n.goal)}${n.target ? " → " + escapeHtml(n.target) : ""}`
          : "—";
        const actBusy = n.mission_complete
          ? "Quest complete"
          : n.pending_action
          ? `${escapeHtml(actionLabel(n.pending_action))}${n.pending_label ? " " + escapeHtml(n.pending_label) : ""} (${Number(n.busy_remaining || 0).toFixed(1)}s)`
          : escapeHtml(n.status || "idle");
        const reason = n.reason
          ? `<span class="sub">Reason: ${escapeHtml(n.reason)}</span>`
          : "";
        return `<button type="button" class="npc-row" data-npc="${escapeHtml(n.name)}">
          <strong>${escapeHtml(n.name)}</strong>
          <span class="sub">${actBusy}</span>
          <span class="sub">${goal}</span>
          ${reason}
        </button>`;
      })
      .join("");
    box.querySelectorAll("[data-npc]").forEach((el) => {
      el.addEventListener("click", () => selectNpc(el.dataset.npc));
    });
  }

  async function refreshSimStatus() {
    try {
      const st = await api("/api/simulation/status");
      const wasRunning = simRunning;
      simRunning = !!st.running;
      const snap = {
        sim_time: st.sim_time,
        tick: st.tick,
        npc_activity: st.npc_activity || [],
      };
      if (playState) playState.simulation = snap;
      renderSimulation(snap);
      renderNpcRoster(playState?.characters || []);
      if (playState?.found) {
        renderTownMap(
          playState.map || { locations: [], edges: [] },
          playState.current_location?.name,
          playState.adjacent || []
        );
      }
      if (
        !simRunning &&
        wasRunning &&
        (st.stopped_reason === "missions_complete" || st.missions_complete) &&
        !simMissionLogged
      ) {
        simMissionLogged = true;
        logEvent(st.message || "Long-term goal achieved; simulation stopped", "ok");
        stopSimPoll();
        await loadPlayState();
      }
      const now = Date.now();
      if (focusedNpc && now - dossierRefreshAt > 1200) {
        dossierRefreshAt = now;
        refreshDossier(focusedNpc, { quiet: true }).catch(() => {});
      }
      if (simRunning && now - simFullRefreshAt > 800) {
        simFullRefreshAt = now;
        await loadPlayState();
      }
    } catch (_) {
      /* ignore poll errors */
    }
  }

  function startSimPoll() {
    stopSimPoll();
    simPollTimer = setInterval(() => {
      refreshSimStatus().catch(() => {});
    }, 800);
  }

  function stopSimPoll() {
    if (simPollTimer) {
      clearInterval(simPollTimer);
      simPollTimer = null;
    }
  }

  async function startSimulation() {
    const useLlm = !!$("chk-sim-llm")?.checked;
    const includePlayer = !!$("chk-sim-player")?.checked;
    const sel = $("sel-sim-character");
    const who = (sel && sel.value) || "";
    const body = {
      interval_seconds: 0.25,
      sim_dt: 0.25,
      use_llm: useLlm,
      force_mock: !useLlm,
      include_player: includePlayer,
    };
    if (who) {
      body.characters = [who];
      body.include_player = true;
    }
    const res = await api("/api/simulation/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    simRunning = !!res.running;
    simMissionLogged = false;
    const mode = useLlm ? "LLM" : "Rules";
    const scope = who
      ? `${who} only`
      : includePlayer
        ? "All characters (incl. player)"
        : "All NPCs";
    logEvent(`Simulation started: ${scope} (${mode} decisions)`, "ok");
    setSideTab("sim");
    startSimPoll();
    await refreshSimStatus();
  }

  async function stopSimulation() {
    await api("/api/simulation/stop", { method: "POST" });
    simRunning = false;
    stopSimPoll();
    logEvent("Simulation stopped", "ok");
    setSideTab("sim");
    await refreshSimStatus();
  }

  function fillSimCharacterSelect(characters) {
    const sel = $("sel-sim-character");
    if (!sel) return;
    const prev = sel.value;
    const list = Array.isArray(characters) ? characters.slice() : [];
    list.sort((a, b) => {
      if (!!a.is_player !== !!b.is_player) return a.is_player ? -1 : 1;
      return String(a.name).localeCompare(String(b.name));
    });
    const opts = [`<option value="">All NPCs</option>`].concat(
      list.map((c) => {
        const tag = c.is_player ? " (player)" : "";
        return `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}${tag}</option>`;
      })
    );
    sel.innerHTML = opts.join("");
    if (prev && [...sel.options].some((o) => o.value === prev)) {
      sel.value = prev;
    }
  }

  function renderNpcRoster(characters) {
    const box = $("npc-roster");
    if (!box) return;
    const list = Array.isArray(characters) ? characters.slice() : [];
    list.sort((a, b) => {
      if (!!a.is_player !== !!b.is_player) return a.is_player ? -1 : 1;
      return String(a.name).localeCompare(String(b.name));
    });
    if (!list.length) {
      box.innerHTML = `<span class="empty">No characters yet</span>`;
      return;
    }
    const activity = (playState?.simulation?.npc_activity || []).reduce((acc, n) => {
      if (n?.name) acc[n.name] = n;
      return acc;
    }, {});
    box.innerHTML = list
      .map((c) => {
        const name = c.name;
        const act = activity[name];
        const status = act?.pending_action || act?.status || (c.alive === false ? "Down" : "");
        const active = focusedNpc === name ? " active" : "";
        const playerCls = c.is_player ? " player" : "";
        return `<button type="button" class="npc-chip${active}${playerCls}" data-npc="${escapeHtml(name)}">
          <span class="chip-name">${escapeHtml(name)}</span>
          <span class="chip-sub">@ ${escapeHtml(c.location || "?")}${status ? " · " + escapeHtml(status) : ""}</span>
        </button>`;
      })
      .join("");
    box.querySelectorAll(".npc-chip").forEach((el) => {
      el.addEventListener("click", () => selectNpc(el.dataset.npc));
    });
  }

  async function selectNpc(name) {
    if (!name) return;
    focusedNpc = name;
    renderNpcRoster(playState?.characters || []);
    // Also set as action target when in same room for quick talk/give
    if (playState?.here?.persons?.some((p) => p.name === name)) {
      selectTarget("person", name);
    }
    await refreshDossier(name);
  }

  async function refreshDossier(name, { quiet = false } = {}) {
    if (!name) return;
    try {
      const data = await api(
        `/api/characters/${encodeURIComponent(name)}/dossier?memory_limit=40`
      );
      dossierCache = data;
      renderDossier(data);
    } catch (e) {
      if (!quiet) setStatus(e.message || "Failed to load character", true);
    }
  }

  function setDossierTab(tab) {
    dossierTab = tab || "info";
    document.querySelectorAll(".dossier-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === dossierTab);
    });
    ["info", "plan", "log"].forEach((id) => {
      const panel = $(`dossier-panel-${id}`);
      if (panel) panel.classList.toggle("active", id === dossierTab);
    });
  }

  function renderDossier(data) {
    const empty = $("dossier-empty");
    const body = $("dossier-body");
    if (!data || !data.found) {
      if (empty) empty.classList.remove("hidden");
      if (body) body.classList.add("hidden");
      return;
    }
    if (empty) empty.classList.add("hidden");
    if (body) body.classList.remove("hidden");

    const st = data.state || {};
    $("dossier-name").textContent = data.name || "—";
    const bits = [
      `@ ${st.location || "?"}`,
      `HP ${st.hp ?? "?"}`,
      st.alive === false ? "Down" : null,
      st.role || null,
      st.mood ? `Mood ${st.mood}` : null,
    ].filter(Boolean);
    $("dossier-meta").textContent = bits.join(" · ");

    // info
    const info = $("dossier-panel-info");
    const fields = (data.profile?.fields || []).slice();
    const held = (st.held_items || []).join(", ") || "(empty)";
    let infoHtml = `<div class="dossier-field"><span class="label">Inventory</span><span class="value">${escapeHtml(held)}</span></div>`;
    const liveGoal =
      data.state?.goal ||
      data.profile?.goal ||
      (data.plan?.goal?.goal
        ? `${data.plan.goal.goal}${data.plan.goal.target_entity ? " → " + data.plan.goal.target_entity : ""}`
        : "");
    infoHtml += `<div class="dossier-field"><span class="label">Current goal</span><span class="value">${escapeHtml(liveGoal || "(none yet)")}</span></div>`;
    if (data.activity) {
      const a = data.activity;
      const actLine = a.pending_action
        ? `${a.pending_action}(${Number(a.busy_remaining || 0).toFixed(1)}s left)`
        : a.status || "idle";
      infoHtml += `<div class="dossier-field"><span class="label">Current activity</span><span class="value">${escapeHtml(actLine)}</span></div>`;
      if (a.replan_reason) {
        infoHtml += `<div class="dossier-field"><span class="label">Replan reason</span><span class="value">${escapeHtml(a.replan_reason)}</span></div>`;
      }
    }
    fields.forEach((f) => {
      if (f.key === "goal") return; // Already shown above as Current goal
      infoHtml += `<div class="dossier-field"><span class="label">${escapeHtml(f.label)}</span><span class="value">${escapeHtml(f.value)}</span></div>`;
    });
    if (!fields.filter((f) => f.key !== "goal").length) {
      infoHtml += `<div class="plan-empty">No more profile fields</div>`;
    }
    info.innerHTML = infoHtml;

    // plan
    const planPanel = $("dossier-panel-plan");
    const plan = data.plan || {};
    const goal = plan.goal || {};
    const steps = plan.plan || [];
    let planHtml = "";
    if (goal && goal.goal) {
      planHtml += `<div class="plan-goal">
        <strong>${escapeHtml(goal.goal)}</strong>${goal.target_entity ? " → " + escapeHtml(goal.target_entity) : ""}
        <div class="muted">${plan.engine ? escapeHtml(plan.engine) : ""}${plan.status ? " · " + escapeHtml(plan.status) : ""}${plan.decision_mode ? " · " + escapeHtml(plan.decision_mode) : ""}</div>
      </div>`;
    } else {
      planHtml += `<div class="plan-empty">No current goal</div>`;
    }

    // Decision reason (explicit display)
    const reasonBits = [];
    if (goal.reason) reasonBits.push(String(goal.reason));
    if (plan.replan_reason) reasonBits.push(`Replan: ${plan.replan_reason}`);
    const proc = plan.decision_process || {};
    if (proc.planner) reasonBits.push(`Planner: ${proc.planner}`);
    if (proc.note) reasonBits.push(String(proc.note));
    if (proc.settings?.goal) reasonBits.push(`Current goal: ${proc.settings.goal}`);
    if (proc.settings?.parent_goal) reasonBits.push(`Character note: ${proc.settings.parent_goal}`);
    if (proc.settings?.personality) reasonBits.push(`Personality: ${proc.settings.personality}`);
    const factors = goal.metadata?.contributing_factors || proc.contributing_factors;
    if (factors && typeof factors === "object") {
      const parts = Object.entries(factors)
        .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(2) : v}`)
        .join(", ");
      if (parts) reasonBits.push(`Factors: ${parts}`);
    }
    planHtml += `<div class="dossier-field plan-reason">
      <span class="label">Decision reason</span>
      <span class="value">${reasonBits.length ? escapeHtml(reasonBits.join("\n")) : "No explanation yet (idle or not decided)"}</span>
    </div>`;

    if (plan.error) {
      planHtml += `<div class="dossier-field"><span class="label">Error</span><span class="value" style="color:var(--danger)">${escapeHtml(plan.error)}</span></div>`;
    }
    if (plan.pending_action) {
      const p = plan.pending_action;
      planHtml += `<div class="dossier-field"><span class="label">In progress</span><span class="value">${escapeHtml(actionLabel(p.name))} · ${escapeHtml(p.name)}(${escapeHtml((p.args || []).join(", "))}) · ${Number(plan.busy_remaining || 0).toFixed(1)}s left</span></div>`;
    }
    if (steps.length) {
      planHtml += `<div class="section-label" style="margin-top:8px">Plan steps</div><ul class="plan-steps">${steps
        .map((s) => {
          const cls = s.status || "pending";
          const act = actionLabel(s.action);
          const label = s.label || `${s.action}(${(s.args || []).join(", ")})`;
          return `<li class="${escapeHtml(cls)}"><strong>${escapeHtml(act)}</strong> · ${escapeHtml(label)}</li>`;
        })
        .join("")}</ul>`;
    } else if (goal && goal.goal) {
      planHtml += `<div class="plan-empty">No plan steps: goal already satisfied, or PDDL/rules could not generate actions (see error above). Scored candidate goals ≠ executable plan.</div>`;
    }
    const cands = plan.candidate_goals || [];
    if (cands.length) {
      planHtml += `<div class="dossier-field" style="margin-top:12px"><span class="label">Candidate goals & reasons</span><span class="value"><span class="muted" style="display:block;margin-bottom:4px">p=priority · c=confidence (LLM score; needs feasibility + successful planning for Plan steps)</span>${cands
        .map((c) => {
          const head = `${c.goal || "?"}${c.target_entity ? " → " + c.target_entity : ""}`;
          const why = c.reason ? ` — ${c.reason}` : "";
          const score =
            c.priority != null || c.confidence != null
              ? `(priority ${Number(c.priority ?? 0).toFixed(2)}, confidence ${Number(c.confidence ?? 0).toFixed(2)})`
              : "";
          return escapeHtml(head + why + score);
        })
        .join("<br>")}</span></div>`;
    }
    planPanel.innerHTML = planHtml;

    // log / memories
    const logPanel = $("dossier-panel-log");
    const mem = data.memories || {};
    const items = mem.memories || [];
    if (!mem.available) {
      logPanel.innerHTML = `<div class="plan-empty">${escapeHtml(mem.message || "Memory unavailable")}</div>`;
    } else if (!items.length) {
      logPanel.innerHTML = `<div class="plan-empty">No action/dialogue memories yet (total ${mem.total || 0})</div>`;
    } else {
      logPanel.innerHTML = `<div class="mem-list">${items
        .map((m) => {
          const fail = m.success === false;
          const ok = m.success === true;
          const cls = fail ? "fail" : ok ? "ok" : "";
          const top = [m.type || "memory", m.location || "", m.timestamp || ""]
            .filter(Boolean)
            .join(" · ");
          return `<div class="mem-item ${cls}">
            <div class="mem-top"><span>${escapeHtml(top)}</span></div>
            <div>${escapeHtml(m.text || "")}</div>
          </div>`;
        })
        .join("")}</div>`;
    }

    setDossierTab(dossierTab);
  }

  function renderHud(data) {
    const p = data.player;
    $("player-name").textContent = p.name;
    $("player-loc").textContent = p.location || "?";
    $("player-hp-text").textContent = `${p.hp}`;
    const bar = $("player-hp-bar");
    const pct = Math.max(0, Math.min(100, Number(p.hp) || 0));
    bar.style.width = pct + "%";
  }

  function iconPerson() {
    return `<span class="tile-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="16" height="16"><circle cx="12" cy="7" r="3.2" fill="currentColor"/><path d="M5.5 20c1.2-4.2 3.6-6.2 6.5-6.2S17.8 15.8 19 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></span>`;
  }
  function iconThing() {
    return `<span class="tile-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="16" height="16"><rect x="4" y="7" width="16" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 7V6a4 4 0 0 1 8 0v1" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></span>`;
  }

  function renderEmptyMap(map) {
    $("room-title").textContent = "No player";
    $("room-desc").textContent = "Load a template world or use the Director to create an event.";
    $("people-grid").innerHTML = "";
    $("items-grid").innerHTML = "";
    $("exits-row").innerHTML = "";
    renderTownMap(map || { locations: [], edges: [] }, null, []);
  }

  function findHereEntity(data, name) {
    const pools = [
      ...(data.here?.items || []),
      ...(data.here?.interactables || []),
    ];
    return pools.find((x) => (typeof x === "string" ? x : x.name) === name) || null;
  }

  function _hereThings(data) {
    const seen = new Set();
    const out = [];
    for (const it of [...(data.here?.items || []), ...(data.here?.interactables || [])]) {
      const name = typeof it === "string" ? it : it?.name;
      if (!name || seen.has(name)) continue;
      seen.add(name);
      out.push(typeof it === "string" ? { name: it } : it);
    }
    return out;
  }

  function _thingSub(it) {
    const kind = (it.item_kind || "").toLowerCase();
    if (kind === "door") {
      return it.locked ? `Locked · needs ${it.key_item || "?"}` : "Door · open";
    }
    if (it.description) return it.description;
    if (it.portable === false) return "Fixed object";
    if (it.value != null) return `Value ${it.value}`;
    return "Object";
  }

  function renderRoom(data) {
    const loc = data.current_location || {};
    $("room-title").textContent = loc.name || "Unknown location";
    $("room-desc").textContent = loc.description || "(no description)";

    const people = data.here?.persons || [];
    const things = _hereThings(data);
    const adjacent = data.adjacent || [];
    const lockedExits = data.locked_exits || [];

    $("people-grid").innerHTML = people.length
      ? people
          .map((p) => {
            const sel =
              selected && selected.type === "person" && selected.name === p.name
                ? " selected"
                : "";
            const roleClass = p.role === "antagonist" ? " antagonist" : "";
            const trust =
              typeof p.trust_toward_player === "number"
                ? ` · Trust ${p.trust_toward_player}`
                : "";
            return `<button type="button" class="entity-tile person${roleClass}${sel}" data-type="person" data-name="${escapeHtml(p.name)}">
              ${iconPerson()}
              <span class="tile-text">
                <span class="tile-name">${escapeHtml(p.name)}</span>
                <span class="sub">HP ${p.hp}${p.alive ? "" : " · Down"}${trust}</span>
              </span>
            </button>`;
          })
          .join("")
      : `<span class="empty">No one else here</span>`;

    $("items-grid").innerHTML = things.length
      ? things
          .map((it) => {
            const name = it.name;
            const sel =
              selected && selected.type === "item" && selected.name === name
                ? " selected"
                : "";
            const kind = (it.item_kind || "").toLowerCase();
            const fixed = kind === "door" || it.portable === false;
            const cls = fixed ? "thing fixed" : "thing";
            return `<button type="button" class="entity-tile ${cls}${sel}" data-type="item" data-name="${escapeHtml(name)}">
              ${iconThing()}
              <span class="tile-text">
                <span class="tile-name">${escapeHtml(name)}</span>
                <span class="sub">${escapeHtml(_thingSub(it))}</span>
              </span>
            </button>`;
          })
          .join("")
      : `<span class="empty">Nothing here</span>`;

    const openHtml = adjacent.map(
      (name, i) =>
        `<button type="button" class="exit-btn" data-exit="${escapeHtml(name)}" title="Hotkey ${i + 1}">→ ${escapeHtml(name)}</button>`
    );
    const lockedHtml = lockedExits.map((ex) => {
      const to = ex.to || ex.name || "";
      const key = ex.key || "?";
      const door = ex.door || "Door";
      return `<button type="button" class="exit-btn locked" disabled title="Use ${escapeHtml(key)} on ${escapeHtml(door)}">Locked · ${escapeHtml(to)} (use ${escapeHtml(key)} on ${escapeHtml(door)})</button>`;
    });
    const exitBits = openHtml.concat(lockedHtml);
    $("exits-row").innerHTML = exitBits.length
      ? exitBits.join("")
      : `<span class="empty">No adjacent exits</span>`;

    $("inv-text").textContent = (data.inventory || []).join(", ") || "(empty)";

    // bind clicks
    $("people-grid").querySelectorAll(".entity-tile").forEach((el) => {
      el.addEventListener("click", () => {
        selectTarget("person", el.dataset.name);
        selectNpc(el.dataset.name);
      });
    });
    $("items-grid").querySelectorAll(".entity-tile").forEach((el) => {
      el.addEventListener("click", () => selectTarget("item", el.dataset.name));
    });
    $("exits-row").querySelectorAll(".exit-btn:not(.locked)").forEach((el) => {
      el.addEventListener("click", () => doMove(el.dataset.exit));
    });
  }

  function renderQuests(quests) {
    const el = $("quest-text");
    if (!quests.length) {
      el.textContent = "(none)";
      return;
    }
    el.textContent = quests
      .map((q) => `${q.name}: ${q.objective || q.description || q.stage}`)
      .join(" · ");
  }

  function renderClues(clues) {
    const el = $("clue-text");
    if (!el) return;
    if (!clues.length) {
      el.textContent = "(No known facts yet; ask InnkeeperMara at the inn)";
      return;
    }
    el.textContent = clues
      .map((c) => {
        const tag = c.kind === "rumor" ? "Rumor" : "Fact";
        return `[${tag}] ${c.text || c.name}`;
      })
      .join(" · ");
  }

  function selectTarget(type, name) {
    selected = { type, name };
    if (playState?.found) {
      renderRoom(playState);
      renderActions(playState);
    }
  }

  function renderActions(data) {
    const box = $("target-box");
    const btns = $("action-btns");
    if (!selected) {
      box.innerHTML = `<span class="empty">Click a person or item to select a target, or click an exit to move.</span>`;
      btns.innerHTML = inventoryActionButtons(data);
      btns.querySelectorAll("[data-act]").forEach((el) => {
        el.addEventListener("click", () => runAction(el.dataset.act));
      });
      return;
    }

    if (selected.type === "person") {
      const p = (data.here?.persons || []).find((x) => x.name === selected.name);
      const trust =
        p && typeof p.trust_toward_player === "number"
          ? p.trust_toward_player
          : "?";
      const persona = p?.personality || p?.role || "";
      box.innerHTML = `<strong>Person</strong>: ${escapeHtml(selected.name)}
        <div class="sub">
          ${escapeHtml(persona)} · Trust ${escapeHtml(String(trust))} · ${escapeHtml(p?.mood || "")}
        </div>`;
    } else {
      const it = findHereEntity(data, selected.name);
      const kind = typeof it === "object" ? it.item_kind || "" : "";
      const locked = typeof it === "object" && it.locked ? " · Locked" : "";
      const isInteract = kind === "door" || (typeof it === "object" && it.portable === false);
      const sub = isInteract
        ? kind === "door"
          ? `Door${locked}${it.key_item ? ` · needs ${it.key_item}` : ""}`
          : typeof it === "object" && it.description
            ? it.description
            : "Interactable"
        : typeof it === "object" && it.description
          ? it.description
          : "Item on ground";
      box.innerHTML = `<strong>${isInteract ? "Interactable" : "Item"}</strong>: ${escapeHtml(selected.name)}
        <div class="sub">${escapeHtml(sub)}</div>`;
    }

    const actions = [];
    if (selected.type === "person") {
      actions.push(
        { name: "talk", label: "Talk" },
        { name: "give", label: "Give" },
        { name: "attack", label: "Attack" },
        { name: "use_on", label: actionLabel("use_on") }
      );
    } else {
      const it = findHereEntity(data, selected.name);
      const kind = typeof it === "object" ? (it.item_kind || "") : "";
      const isInteract = kind === "door" || (typeof it === "object" && it.portable === false);
      if (!isInteract) {
        actions.push({ name: "pick_up", label: actionLabel("pick_up") });
      }
      actions.push({ name: "use_on", label: actionLabel("use_on") });
    }

    // use_item / drop always from inventory panel
    const html = actions
      .map(
        (a) =>
          `<button type="button" class="btn ghost" data-act="${a.name}">${a.label}</button>`
      )
      .join("");
    btns.innerHTML = html + inventoryActionButtons(data);

    btns.querySelectorAll("[data-act]").forEach((el) => {
      el.addEventListener("click", () => runAction(el.dataset.act));
    });
  }

  function inventoryActionButtons(data) {
    const inv = data.inventory || [];
    if (!inv.length) return "";
    return (
      `<button type="button" class="btn ghost" data-act="drop">Drop</button>` +
      `<button type="button" class="btn ghost" data-act="use_item">Use item</button>`
    );
  }

  function findAction(name) {
    const list = playState?.available_actions || playState?.all_actions || [];
    return list.find((a) => a.name === name && a.available);
  }

  async function doMove(to) {
    if (busy || !playState?.found) return;
    const player = playState.player;
    await executeAction("move", {
      c: player.name,
      from: player.location,
      to,
    });
  }

  async function runAction(actionName) {
    if (busy || !playState?.found) return;
    const player = playState.player;
    const loc = player.location;
    const entry = findAction(actionName);
    // use_on: try with selected target (world interaction table decides match)
    if (!entry && actionName !== "use_on") {
      setStatus(`Cannot execute now: ${actionName}`, true);
      return;
    }

    const params = { ...(entry?.defaults || {}) };

    if (actionName === "talk" || actionName === "attack") {
      if (!selected || selected.type !== "person") {
        setStatus("Select a person at this location first", true);
        return;
      }
      if (actionName === "talk") {
        openTalkModal(selected.name);
        return;
      }
      params.attacker = player.name;
      params.target = selected.name;
      params.l = loc;
    } else if (actionName === "give") {
      if (!selected || selected.type !== "person") {
        setStatus("Select a recipient first", true);
        return;
      }
      const item = params.i || (player.held_items || [])[0];
      if (!item) {
        setStatus("Inventory empty", true);
        return;
      }
      params.giver = player.name;
      params.receiver = selected.name;
      params.i = item;
      params.l = loc;
    } else if (actionName === "pick_up") {
      if (!selected || selected.type !== "item") {
        setStatus("Select an item on the ground first", true);
        return;
      }
      params.c = player.name;
      params.i = selected.name;
      params.l = loc;
    } else if (actionName === "use_on") {
      if (!selected) {
        setStatus("Select a target (person or item) first", true);
        return;
      }
      params.target = selected.name;
      params.c = player.name;
      params.from = loc;
      const held = player.held_items || [];
      if (!held.length) {
        setStatus("Inventory empty; pick up an item first", true);
        return;
      }
      const catalog = (playState && playState.use_on_by_target) || {};
      const slot = catalog[selected.name] || {};
      const accepted = slot.tools || [];
      const candidates = accepted.length
        ? held.filter((x) => accepted.includes(x))
        : held;
      if (accepted.length && !candidates.length) {
        setStatus(
          `${selected.name} only accepts: ${accepted.join(", ")} (not in inventory)`,
          true
        );
        return;
      }
      const item = pickInventoryItem(candidates, selected.name);
      if (!item) return;
      params.i = item;
      params.to = loc;
    } else if (actionName === "drop" || actionName === "use_item") {
      const item = pickInventoryItem(player.held_items || []);
      if (!item) return;
      params.c = player.name;
      params.i = item;
      if (actionName === "drop") params.l = loc;
    }

    await executeAction(actionName, params);
  }

  function pickInventoryItem(items, targetName) {
    if (!items.length) {
      setStatus("Inventory empty", true);
      return null;
    }
    if (items.length === 1) return items[0];
    const hint = targetName ? `Use on ${targetName}, ` : "";
    const choice = window.prompt(
      `${hint}Choose item:\n` + items.join(", "),
      items[0]
    );
    if (!choice || !items.includes(choice)) {
      setStatus("Cancelled", true);
      return null;
    }
    return choice;
  }

  async function executeAction(action, params, message = "") {
    busy = true;
    setStatus(`Executing ${action}...`);
    try {
      const body = { action, params };
      if (message) body.message = message;
      const res = await api("/api/actions/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.success) {
        logEvent(`${action} failed: ${res.message || "unknown"}`, "fail");
        setStatus(res.message || "Failed", true);
      } else {
        let extra = "";
        if (res.dialogue && res.dialogue.source === "error") {
          const inspect =
            res.dialogue.inspect_url ||
            (res.dialogue.trace_id
              ? `/inspect?id=${encodeURIComponent(res.dialogue.trace_id)}`
              : "/inspect");
          logEvent(`Dialogue failed: ${res.dialogue.error || "LLM required"}`, "fail",
            ` · <a href="${inspect}" target="_blank" rel="noopener">View LLM log</a>`);
          setStatus(res.dialogue.error || "Dialogue requires LLM", true);
        } else if (res.dialogue && res.dialogue.lines) {
          const src = res.dialogue.source || "llm";
          const lines = res.dialogue.lines
            .map(
              (ln) =>
                `<div><span class="who">${escapeHtml(ln.speaker)}</span>: ${escapeHtml(ln.text)}</div>`
            )
            .join("");
          let clueHtml = "";
          if (typeof res.dialogue.trust === "number") {
            clueHtml += `<div class="log-note">Their trust: ${res.dialogue.trust}</div>`;
          }
          clueHtml += `<div class="log-note">Source: ${escapeHtml(src)}</div>`;
          extra = `<div class="log-extra">${lines}</div>${clueHtml}`;
          logEvent(`${action} · dialogue`, "dialogue", extra);
          setStatus(res.message || "Done");
        } else {
          logEvent(`${action} succeeded: ${res.message || "ok"}`, "ok");
          setStatus(res.message || "Done");
        }
        if (action === "move" || action === "pick_up") selected = null;
      }
      await loadPlayState();
      if (graphMode) await loadGraph();
    } catch (e) {
      logEvent(String(e.message || e), "fail");
      setStatus(e.message || String(e), true);
    } finally {
      busy = false;
    }
  }

  function openTalkModal(npcName) {
    talkTarget = npcName;
    $("talk-npc-name").textContent = npcName;
    $("talk-hint").textContent = `You are at ${playState?.current_location?.name || "here"}, talking to ${npcName}.`;
    $("talk-input").value = "";
    $("talk-modal").classList.add("visible");
    setTimeout(() => $("talk-input").focus(), 50);
  }

  function closeTalkModal() {
    $("talk-modal").classList.remove("visible");
    talkTarget = null;
  }

  async function sendTalk() {
    if (busy || !playState?.found || !talkTarget) return;
    const text = ($("talk-input").value || "").trim();
    if (!text) {
      setStatus("Enter what you want to say first", true);
      $("talk-input").focus();
      return;
    }
    const player = playState.player;
    const npc = talkTarget;
    closeTalkModal();
    const badge = $("llm-badge");
    const llmReady = badge && badge.classList.contains("on");
    if (llmReady && /ready|file/i.test(badge.textContent || "")) {
      setStatus("First dialogue loading LLM; may take 1–2 minutes…");
    }
    await executeAction(
      "talk",
      { a: player.name, b: npc, l: player.location },
      text
    );
  }

  // —— Town canvas layout ——
  // Locations are room-boxes listing people + things (no fan-out overlap).

  function shortName(name, max = 6) {
    const s = String(name || "");
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }

  function buildActorOverlay() {
    const byName = {};
    const chars = playState?.characters || [];
    for (const c of chars) {
      if (!c?.name) continue;
      byName[c.name] = {
        name: c.name,
        location: c.location,
        isPlayer: !!c.is_player,
        action: null,
        goal: null,
        busy: 0,
      };
    }
    for (const loc of playState?.map?.locations || []) {
      for (const pname of loc.persons || []) {
        if (!byName[pname]) {
          byName[pname] = { name: pname, location: loc.name, isPlayer: false, action: null, goal: null, busy: 0 };
        } else if (!byName[pname].location) {
          byName[pname].location = loc.name;
        }
      }
    }
    const activity = playState?.simulation?.npc_activity || [];
    for (const a of activity) {
      if (!a?.name) continue;
      if (!byName[a.name]) {
        byName[a.name] = { name: a.name, location: a.location, isPlayer: false, action: null, goal: null, busy: 0 };
      }
      const row = byName[a.name];
      if (a.location) row.location = a.location;
      row.action = a.pending_action || a.last_action || null;
      if (a.goal) row.goal = a.target ? `${a.goal}→${a.target}` : a.goal;
      row.busy = Number(a.busy_remaining || 0);
    }
    const player = playState?.player;
    if (player?.name) {
      if (!byName[player.name]) {
        byName[player.name] = {
          name: player.name,
          location: player.location,
          isPlayer: true,
          action: null,
          goal: null,
          busy: 0,
        };
      } else {
        byName[player.name].isPlayer = true;
        byName[player.name].location = player.location || byName[player.name].location;
      }
    }
    return Object.values(byName).filter((a) => a.location);
  }

  function groupActorsByLocation(actors) {
    const groups = {};
    for (const a of actors) {
      if (!groups[a.location]) groups[a.location] = [];
      groups[a.location].push(a);
    }
    for (const list of Object.values(groups)) {
      list.sort((a, b) => {
        if (!!a.isPlayer !== !!b.isPlayer) return a.isPlayer ? -1 : 1;
        return String(a.name).localeCompare(String(b.name));
      });
    }
    return groups;
  }

  function locContents(loc, actorGroups) {
    const people = (actorGroups && actorGroups[loc.name]) || [];
    const peopleNames = people.length
      ? people.map((a) => a.name)
      : loc.persons || [];
    const things = [];
    const seen = new Set();
    for (const it of [...(loc.items || []), ...(loc.interactables || [])]) {
      const name = typeof it === "string" ? it : it?.name;
      if (!name || seen.has(name)) continue;
      seen.add(name);
      things.push(name);
    }
    return { people, peopleNames, things };
  }

  function _fmtLocRow(arr, max = 3) {
    const shown = (arr || []).slice(0, max);
    const extra = (arr || []).length - shown.length;
    if (!shown.length) return "None";
    return shown.map((n) => shortName(n, 6)).join(", ") + (extra > 0 ? ` +${extra}` : "");
  }

  /** Room box size: title + two rows (people / things). */
  function measureLocBox(loc, actorGroups) {
    const padX = 10;
    const titleH = 22;
    const lineH = 16;
    const sectionGap = 4;
    const padY = 7;
    const iconW = 16;
    const { peopleNames, things } = locContents(loc, actorGroups);
    const lines = [
      { kind: "people", text: _fmtLocRow(peopleNames) },
      { kind: "things", text: _fmtLocRow(things) },
    ];
    const titleW = Math.min(180, Math.max(72, String(loc.name).length * 14 + 24));
    const bodyW = Math.max(
      ...lines.map((row) => Math.min(200, iconW + 8 + String(row.text).length * 11))
    );
    const w = Math.max(titleW, bodyW) + padX * 2;
    const h = titleH + padY + lines.length * lineH + (lines.length - 1) * sectionGap + padY;
    return { w, h, titleH, padX, padY, lineH, sectionGap, iconW, lines };
  }

  function drawMapPersonIcon(ctx, x, y, color) {
    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.arc(x + 6, y - 3.5, 2.6, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(x + 1.5, y + 5.5);
    ctx.quadraticCurveTo(x + 6, y - 0.5, x + 10.5, y + 5.5);
    ctx.stroke();
    ctx.restore();
  }

  function drawMapThingIcon(ctx, x, y, color) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeRect(x + 1.5, y - 1.5, 9, 7);
    ctx.beginPath();
    ctx.moveTo(x + 3.5, y - 1.5);
    ctx.quadraticCurveTo(x + 6, y - 5.5, x + 8.5, y - 1.5);
    ctx.stroke();
    ctx.restore();
  }

  function layoutTown(locations, edges, W, H, actorGroups) {
    const names = (locations || []).map((l) => l.name);
    const n = names.length;
    if (!n) return {};

    const sizes = {};
    const byName = {};
    for (const loc of locations || []) byName[loc.name] = loc;
    for (const name of names) {
      sizes[name] = measureLocBox(byName[name] || { name }, actorGroups);
    }

    const cx = W / 2;
    const cy = H / 2 + 10;
    const r = Math.min(W, H) * 0.32;
    const pos = {};
    names.forEach((name, i) => {
      const ang = (Math.PI * 2 * i) / n - Math.PI / 2;
      pos[name] = { x: cx + r * Math.cos(ang), y: cy + r * Math.sin(ang) };
    });

    const ideal = Math.max(170, Math.min(W, H) * 0.28);
    for (let iter = 0; iter < 90; iter++) {
      // spring along edges
      for (const e of edges || []) {
        const a = pos[e.a];
        const b = pos[e.b];
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const sa = sizes[e.a];
        const sb = sizes[e.b];
        const minDist = ideal + (sa.w + sb.w) * 0.15;
        const f = ((dist - minDist) / dist) * 0.05;
        a.x += dx * f;
        a.y += dy * f;
        b.x -= dx * f;
        b.y -= dy * f;
      }
      // box collision separation
      for (let i = 0; i < names.length; i++) {
        for (let j = i + 1; j < names.length; j++) {
          const na = names[i];
          const nb = names[j];
          const a = pos[na];
          const b = pos[nb];
          const sa = sizes[na];
          const sb = sizes[nb];
          const gap = 16;
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const overlapX = (sa.w + sb.w) / 2 + gap - Math.abs(dx);
          const overlapY = (sa.h + sb.h) / 2 + gap - Math.abs(dy);
          if (overlapX > 0 && overlapY > 0) {
            if (overlapX < overlapY) {
              const push = (overlapX / 2) * (dx === 0 ? (i % 2 ? 1 : -1) : Math.sign(dx) || 1);
              a.x -= push;
              b.x += push;
            } else {
              const push = (overlapY / 2) * (dy === 0 ? (i % 2 ? 1 : -1) : Math.sign(dy) || 1);
              a.y -= push;
              b.y += push;
            }
          }
        }
      }
      // keep boxes inside canvas
      for (const name of names) {
        const p = pos[name];
        const s = sizes[name];
        const pad = 8;
        p.x = Math.max(s.w / 2 + pad, Math.min(W - s.w / 2 - pad, p.x));
        p.y = Math.max(s.h / 2 + pad + 28, Math.min(H - s.h / 2 - pad, p.y));
      }
    }

    const out = {};
    for (const name of names) {
      const s = sizes[name];
      const p = pos[name];
      out[name] = {
        x: p.x,
        y: p.y,
        w: s.w,
        h: s.h,
        left: p.x - s.w / 2,
        top: p.y - s.h / 2,
        ...s,
      };
    }
    return out;
  }

  function hitTestTown(cssX, cssY) {
    // topmost (last drawn) wins — iterate reverse of insertion
    const names = Object.keys(townLayout || {});
    for (let i = names.length - 1; i >= 0; i--) {
      const name = names[i];
      const p = townLayout[name];
      if (!p) continue;
      if (cssX >= p.left && cssX <= p.left + p.w && cssY >= p.top && cssY <= p.top + p.h) {
        return name;
      }
    }
    return null;
  }

  function drawLocRoom(ctx, loc, box, people, current, adjSet) {
    const chalk = "#e8dcc8";
    const isCur = loc.name === current;
    const isAdj = adjSet.has(loc.name);
    const { left, top, w, h, titleH, padX, padY, lineH, sectionGap, iconW, lines } = box;

    // room body
    if (isCur) ctx.fillStyle = "#3a241c";
    else if (isAdj) ctx.fillStyle = "#1e2e22";
    else ctx.fillStyle = "#221e18";
    roundRect(ctx, left, top, w, h, 8);
    ctx.fill();
    ctx.lineWidth = isCur ? 3 : 2.5;
    ctx.strokeStyle = isCur ? "#e25a3c" : chalk;
    ctx.stroke();

    if (isCur) {
      ctx.setLineDash([4, 4]);
      roundRect(ctx, left - 4, top - 4, w + 8, h + 8, 10);
      ctx.strokeStyle = "#e25a3c";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // title strip
    ctx.fillStyle = isCur ? "#4a2e24" : "#2a2620";
    roundRect(ctx, left + 2, top + 2, w - 4, titleH - 2, 6);
    ctx.fill();

    ctx.font = "15px ZCOOL KuaiLe, Comic Sans MS, PingFang SC, Microsoft YaHei, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = isCur ? "#f0a070" : chalk;
    ctx.fillText(shortName(loc.name, 10), left + w / 2, top + titleH / 2 + 1);

    // Two rows: people / things (with icons)
    const bodyTop = top + titleH + padY;
    const rowLines =
      lines ||
      (() => {
        const c = locContents(loc, { [loc.name]: people || [] });
        return [
          { kind: "people", text: _fmtLocRow(c.peopleNames) },
          { kind: "things", text: _fmtLocRow(c.things) },
        ];
      })();

    const iw = iconW || 16;
    ctx.textAlign = "left";
    ctx.font = "12px ZCOOL KuaiLe, Comic Sans MS, PingFang SC, Microsoft YaHei, sans-serif";
    rowLines.forEach((row, i) => {
      const y = bodyTop + i * ((lineH || 16) + (sectionGap || 4)) + (lineH || 16) / 2;
      const kind = row.kind || (i === 0 ? "people" : "things");
      const iconColor = kind === "people" ? "#6aa8c8" : "#e0a84a";
      if (kind === "people") drawMapPersonIcon(ctx, left + padX, y, iconColor);
      else drawMapThingIcon(ctx, left + padX, y, iconColor);
      ctx.fillStyle = chalk;
      const maxW = w - padX * 2 - iw - 4;
      let draw = String(row.text || "");
      while (ctx.measureText(draw).width > maxW && draw.length > 1) {
        draw = draw.slice(0, -2) + "…";
      }
      ctx.fillText(draw, left + padX + iw + 2, y);
    });
  }

  function hideLocTooltip() {
    const tip = $("loc-tooltip");
    if (tip) tip.classList.add("hidden");
  }

  function showLocTooltip(locName, clientX, clientY) {
    const tip = $("loc-tooltip");
    const viewport = tip?.parentElement;
    if (!tip || !viewport || !playState) return;
    const locs = playState.map?.locations || [];
    const loc = locs.find((l) => l.name === locName);
    if (!loc) {
      tip.classList.add("hidden");
      return;
    }
    const current = playState.current_location?.name;
    const adjacent = playState.adjacent || [];
    const isCur = loc.name === current;
    const isAdj = adjacent.includes(loc.name);
    let badge = `<span class="tt-badge far">Far</span>`;
    if (isCur) badge = `<span class="tt-badge here">Current location</span>`;
    else if (isAdj) badge = `<span class="tt-badge">Reachable · click to move</span>`;

    const desc = loc.description || "(no description)";
    const tags = loc.tags ? `<div class="tt-row"><span class="tt-label">Tags</span> ${escapeHtml(loc.tags)}</div>` : "";
    const actorsHere = buildActorOverlay().filter((a) => a.location === loc.name);
    const peopleNames = actorsHere.length
      ? actorsHere.map((a) => {
          const act = a.action ? `(${actionLabel(a.action)})` : "";
          return `${a.name}${act}`;
        })
      : loc.persons || [];
    const people = peopleNames.length
      ? `<div class="tt-row"><span class="tt-label">People</span> ${escapeHtml(peopleNames.join(", "))}</div>`
      : `<div class="tt-row"><span class="tt-label">People</span> None</div>`;
    const thingNames = [
      ...(loc.items || []),
      ...((loc.interactables || []).map((x) => {
        if (typeof x === "string") return x;
        const lock = x.locked ? "Lck" : "Opn";
        return x.item_kind === "door" ? `${x.name}(${lock})` : x.name;
      })),
    ];
    const things = thingNames.length
      ? `<div class="tt-row"><span class="tt-label">Things</span> ${escapeHtml(thingNames.join(", "))}</div>`
      : `<div class="tt-row"><span class="tt-label">Things</span> None</div>`;
    const exits = (loc.adjacent || []).length
      ? `<div class="tt-row"><span class="tt-label">Connected</span> ${escapeHtml((loc.adjacent || []).join(", "))}</div>`
      : "";

    tip.innerHTML = `
      <div class="tt-name">${escapeHtml(loc.name)}</div>
      ${badge}
      <div class="tt-desc">${escapeHtml(desc)}</div>
      ${tags}${people}${things}${exits}
    `;
    tip.classList.remove("hidden");

    const rect = viewport.getBoundingClientRect();
    let left = clientX - rect.left + 14;
    let top = clientY - rect.top + 14;
    // keep inside viewport
    tip.style.left = "0px";
    tip.style.top = "0px";
    const tw = tip.offsetWidth || 220;
    const th = tip.offsetHeight || 120;
    if (left + tw > rect.width - 8) left = Math.max(8, rect.width - tw - 8);
    if (top + th > rect.height - 8) top = Math.max(8, clientY - rect.top - th - 12);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    tip.style.transform = "none";
  }

  /** Slightly wobbly line for doodle paths */
  function doodleLine(ctx, x1, y1, x2, y2) {
    const mx = (x1 + x2) / 2 + (y2 - y1) * 0.04;
    const my = (y1 + y2) / 2 - (x2 - x1) * 0.04;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.quadraticCurveTo(mx, my, x2, y2);
  }

  /** Point on box border facing toward (tx, ty). */
  function boxEdgePoint(box, tx, ty) {
    const dx = tx - box.x;
    const dy = ty - box.y;
    if (Math.abs(dx) < 1e-6 && Math.abs(dy) < 1e-6) return { x: box.x, y: box.y };
    const hw = box.w / 2;
    const hh = box.h / 2;
    const sx = Math.abs(dx) > 1e-6 ? hw / Math.abs(dx) : Infinity;
    const sy = Math.abs(dy) > 1e-6 ? hh / Math.abs(dy) : Infinity;
    const t = Math.min(sx, sy);
    return { x: box.x + dx * t, y: box.y + dy * t };
  }

  function renderTownMap(map, current, adjacent) {
    const canvas = $("town-canvas");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(canvas.clientWidth || 640, 320);
    const cssH = Math.max(canvas.clientHeight || 420, 280);
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    // light paper fill under rooms
    ctx.fillStyle = "#f3ebe0";
    ctx.fillRect(0, 0, cssW, cssH);
    // faint notebook dots
    ctx.fillStyle = "rgba(43, 36, 24, 0.08)";
    for (let y = 10; y < cssH; y += 14) {
      for (let x = 10; x < cssW; x += 14) {
        ctx.beginPath();
        ctx.arc(x, y, 0.8, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    const locations = map?.locations || [];
    const edges = map?.edges || [];
    const actorGroups = groupActorsByLocation(buildActorOverlay());
    townLayout = layoutTown(locations, edges, cssW, cssH, actorGroups);

    const adjSet = new Set(adjacent || []);

    // paths between room borders (not through people)
    for (const e of edges) {
      const a = townLayout[e.a];
      const b = townLayout[e.b];
      if (!a || !b) continue;
      const toCur =
        (e.a === current && adjSet.has(e.b)) || (e.b === current && adjSet.has(e.a));
      const p1 = boxEdgePoint(a, b.x, b.y);
      const p2 = boxEdgePoint(b, a.x, a.y);
      doodleLine(ctx, p1.x, p1.y, p2.x, p2.y);
      if (toCur) {
        ctx.strokeStyle = "#2f8f5b";
        ctx.lineWidth = 3;
        ctx.setLineDash([]);
      } else {
        ctx.strokeStyle = "rgba(43, 36, 24, 0.45)";
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 7]);
      }
      ctx.lineCap = "round";
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // rooms with people inside (draw current last so it sits on top)
    const ordered = [...locations].sort((a, b) => {
      if (a.name === current) return 1;
      if (b.name === current) return -1;
      return 0;
    });
    for (const loc of ordered) {
      const box = townLayout[loc.name];
      if (!box) continue;
      drawLocRoom(ctx, loc, box, actorGroups[loc.name] || [], current, adjSet);
    }

    const toLocal = (ev) => {
      const rect = canvas.getBoundingClientRect();
      return {
        x: ((ev.clientX - rect.left) / rect.width) * cssW,
        y: ((ev.clientY - rect.top) / rect.height) * cssH,
        clientX: ev.clientX,
        clientY: ev.clientY,
      };
    };

    canvas.onmousemove = (ev) => {
      const { x, y, clientX, clientY } = toLocal(ev);
      const hit = hitTestTown(x, y);
      if (hit) {
        canvas.style.cursor = (adjacent || []).includes(hit) || hit === current ? "pointer" : "help";
        showLocTooltip(hit, clientX, clientY);
      } else {
        canvas.style.cursor = "crosshair";
        hideLocTooltip();
      }
    };
    canvas.onmouseleave = () => hideLocTooltip();

    canvas.onclick = (ev) => {
      if (!playState?.found || busy) return;
      const { x, y } = toLocal(ev);
      const best = hitTestTown(x, y);
      if (!best) return;
      if (best === playState.current_location?.name) return;
      if ((playState.adjacent || []).includes(best)) doMove(best);
      else setStatus("Can only move to adjacent locations", true);
    };
  }

  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  // —— Graph ——
  function initNetwork() {
    if (network) return;
    nodesDS = new vis.DataSet([]);
    edgesDS = new vis.DataSet([]);
    network = new vis.Network(
      $("graph"),
      { nodes: nodesDS, edges: edgesDS },
      {
        physics: { stabilization: { iterations: 80 } },
        interaction: { hover: true },
        nodes: {
          font: { size: 15, face: "ZCOOL KuaiLe, Comic Sans MS, Microsoft YaHei", color: "#e8dcc8" },
          borderWidth: 2.5,
          shadow: false,
        },
        edges: {
          color: { color: "#cfc3ae" },
          font: { size: 11, face: "ZCOOL KuaiLe, Comic Sans MS, Microsoft YaHei", color: "#9a8e7a", strokeWidth: 0 },
          width: 1.5,
          smooth: { type: "curvedCW", roundness: 0.12 },
        },
      }
    );
  }

  async function loadGraph() {
    initNetwork();
    const data = await api("/api/graph");
    nodesDS.clear();
    edgesDS.clear();
    nodesDS.add(data.nodes || []);
    edgesDS.add(data.edges || []);
    network.fit({ animation: false });
  }

  function setGraphMode(on) {
    graphMode = on;
    $("btn-graph").classList.toggle("on", on);
    document.querySelector(".stage").classList.toggle("graph-mode", on);
    $("play-side").classList.toggle("hidden", on);
    const tabs = $("side-tabs");
    if (tabs) tabs.classList.toggle("hidden", on);
    $("graph-wrap").classList.toggle("visible", on);
    if (on) loadGraph().catch((e) => setStatus(e.message, true));
  }

  // —— Template worlds ——
  function openWorlds() {
    $("worlds-modal").classList.add("visible");
    fillWorldList($("worlds-list"), { confirmLoad: true });
  }
  function closeWorlds() {
    $("worlds-modal").classList.remove("visible");
  }

  async function fillWorldList(box, { confirmLoad } = {}) {
    if (!box) return;
    try {
      const data = await api("/api/worlds");
      const list = data.worlds || [];
      if (!list.length) {
        box.innerHTML = `<p class="hint">No template worlds found. Add .json configs under worlds/.</p>`;
        return;
      }
      box.innerHTML = list
        .map(
          (w) => `<button type="button" class="template-btn" data-id="${escapeHtml(w.id)}">
          <strong>${escapeHtml(w.title || w.id)}</strong>
          <small>${escapeHtml(w.premise || "")}</small>
        </button>`
        )
        .join("");
      box.querySelectorAll(".template-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.getAttribute("data-id");
          loadWorldTemplate(id, { confirm: !!confirmLoad }).catch((e) =>
            setStatus(e.message, true)
          );
        });
      });
    } catch (e) {
      box.innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
    }
  }

  async function loadWorldTemplate(worldId, { confirm } = {}) {
    if (!worldId) return;
    if (confirm && !window.confirm(`Load template world "${worldId}" and clear current world and memory. Continue?`)) {
      return;
    }
    setStatus(`Loading template world ${worldId}…`);
    const res = await api(`/api/worlds/${encodeURIComponent(worldId)}/load`, {
      method: "POST",
    });
    selected = null;
    closeWorlds();
    const memNote = res.memory_cleared ? " · memory cleared" : "";
    logEvent((res.message || `Loaded ${worldId}`) + memNote, "ok");
    await loadPlayState();
    if (graphMode) await loadGraph();
    setStatus(res.message || "Done");
  }

  // —— Director ——
  function openDirector() {
    $("director-modal").classList.add("visible");
  }
  function closeDirector() {
    $("director-modal").classList.remove("visible");
  }

  function setDirectorStatus(msg, isError) {
    const el = $("director-status");
    if (el) {
      el.textContent = msg || "";
      el.className = "director-status" + (isError ? " is-error" : "");
    }
    setStatus(msg, isError);
  }

  function setDirectorBusy(busyFlag) {
    const planBtn = $("btn-director-plan");
    const buildBtn = $("btn-director-build");
    if (planBtn) {
      planBtn.disabled = !!busyFlag;
      planBtn.textContent = busyFlag ? "Expanding…" : "AI expand";
    }
    if (buildBtn) buildBtn.disabled = !!busyFlag;
  }

  function parseOptionalDraftJson() {
    const raw = ($("director-draft-json")?.value || "").trim();
    if (!raw) return null;
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) return obj;
      throw new Error("Draft JSON must be an object");
    } catch (e) {
      throw new Error(`Partial blueprint JSON invalid: ${e.message || e}`);
    }
  }

  function readConfirmedBlueprint() {
    const raw = ($("director-blueprint-json")?.value || "").trim();
    if (!raw) return directorBlueprint;
    try {
      const obj = JSON.parse(raw);
      if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
        throw new Error("Blueprint must be a JSON object");
      }
      return obj;
    } catch (e) {
      throw new Error(`Completed JSON invalid; fix first: ${e.message || e}`);
    }
  }

  function showBlueprint(bp) {
    directorBlueprint = bp || null;
    $("director-preview").textContent = formatBlueprint(bp);
    const ta = $("director-blueprint-json");
    if (ta) {
      ta.value = bp ? JSON.stringify(bp, null, 2) : "";
    }
  }

  function formatBlueprint(bp) {
    if (!bp) return 'Not planned yet — click "AI expand" first';
    const c = bp.counts || {};
    return [
      `Title: ${bp.title || ""}`,
      `Source: ${bp.source || ""}`,
      `Premise: ${bp.premise || ""}`,
      `Locations (${c.locations ?? (bp.locations || []).length}): ${(bp.locations || []).map((x) => x.name).join(", ")}`,
      `Persons (${c.persons ?? (bp.persons || []).length}): ${(bp.persons || []).map((x) => x.name).join(", ")}`,
      `Items (${c.items ?? (bp.items || []).length}): ${(bp.items || []).map((x) => x.name).join(", ")}`,
      `Quests (${c.quests ?? (bp.quests || []).length}): ${(bp.quests || []).map((x) => x.name).join(", ")}`,
    ].join("\n");
  }

  async function directorPlan() {
    const prompt = $("director-prompt").value.trim();
    if (!prompt) {
      setDirectorStatus("Write an event draft first", true);
      return;
    }
    const body = { prompt, force_mock: false };
    const draft = parseOptionalDraftJson();
    if (draft) body.draft = draft;
    setDirectorBusy(true);
    $("director-preview").textContent = "AI step-by-step expansion (skeleton→world→cast→mechanics, up to ~110s)…";
    setDirectorStatus("AI expanding blueprint (skeleton / world / cast / mechanics)…");
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 120000);
    const started = Date.now();
    const tick = setInterval(() => {
      const sec = Math.round((Date.now() - started) / 1000);
      setDirectorStatus(`AI expanding… waited ${sec}s`);
    }, 1000);
    try {
      const res = await api("/api/director/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!res.ok || !res.blueprint) {
        $("director-preview").textContent = "Expansion failed — edit draft and retry, or use a template world";
        setDirectorStatus(res.warning || "AI expansion failed", true);
        return;
      }
      showBlueprint(res.blueprint);
      const src = res.blueprint?.source || "";
      if (res.warning) {
        setDirectorStatus(`${res.warning}(blueprint source: ${src})`, true);
      } else {
        setDirectorStatus(
          `Expanded: ${res.blueprint?.title || ""} (source ${src}) — review JSON below, then confirm write`
        );
      }
    } catch (e) {
      const msg =
        e.name === "AbortError"
          ? "Request timed out (120s). Simplify draft and retry, or use Template Worlds."
          : e.message || String(e);
      $("director-preview").textContent = "Expansion failed — edit draft and retry";
      setDirectorStatus(msg, true);
      throw e;
    } finally {
      clearTimeout(timer);
      clearInterval(tick);
      setDirectorBusy(false);
    }
  }

  async function directorBuild() {
    let bp;
    try {
      bp = readConfirmedBlueprint();
    } catch (e) {
      setDirectorStatus(e.message || String(e), true);
      return;
    }
    if (!bp) {
      setDirectorStatus('Run "AI expand" and review blueprint first, then confirm write', true);
      return;
    }
    if (!confirm("Rebuild world from current blueprint and start new event. Continue?")) return;
    const prompt = $("director-prompt").value.trim();
    const body = { prompt, force_mock: false, clear: true, blueprint: bp };
    setDirectorBusy(true);
    setDirectorStatus("Writing world…");
    try {
      const res = await api("/api/director/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      showBlueprint(res.blueprint || bp);
      closeDirector();
      selected = null;
      logEvent(`New event: ${res.title || res.message}`, "ok");
      await loadPlayState();
      if (graphMode) await loadGraph();
      setStatus(res.message || "Build complete");
    } catch (e) {
      setDirectorStatus(e.message || String(e), true);
      throw e;
    } finally {
      setDirectorBusy(false);
    }
  }

  // —— Boot ——
  async function boot() {
    $("btn-graph").addEventListener("click", () => setGraphMode(!graphMode));
    $("btn-director").addEventListener("click", openDirector);
    $("btn-director-close").addEventListener("click", closeDirector);
    $("btn-worlds")?.addEventListener("click", openWorlds);
    $("btn-worlds-close")?.addEventListener("click", closeWorlds);
    $("btn-talk-cancel").addEventListener("click", closeTalkModal);
    $("btn-talk-send").addEventListener("click", () =>
      sendTalk().catch((e) => setStatus(e.message, true))
    );
    $("talk-input").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        sendTalk().catch((e) => setStatus(e.message, true));
      }
    });
    $("btn-director-plan").addEventListener("click", () =>
      directorPlan().catch((e) => setDirectorStatus(e.message, true))
    );
    $("btn-director-build").addEventListener("click", () =>
      directorBuild().catch((e) => setDirectorStatus(e.message, true))
    );
    $("director-prompt").addEventListener("input", () => {
      directorBlueprint = null;
      const prev = $("director-preview");
      if (prev) prev.textContent = 'Draft changed — click "AI expand" again';
    });
    $("director-blueprint-json")?.addEventListener("input", () => {
      try {
        const raw = ($("director-blueprint-json").value || "").trim();
        if (!raw) {
          directorBlueprint = null;
          return;
        }
        directorBlueprint = JSON.parse(raw);
        $("director-preview").textContent = formatBlueprint(directorBlueprint);
      } catch (_) {
        /* allow temporarily invalid JSON while editing */
      }
    });

    fillWorldList($("gate-worlds"), { confirmLoad: false });
    $("btn-gate-director").addEventListener("click", openDirector);

    $("btn-sim-start")?.addEventListener("click", () =>
      startSimulation().catch((e) => setStatus(e.message, true))
    );
    $("btn-sim-stop")?.addEventListener("click", () =>
      stopSimulation().catch((e) => setStatus(e.message, true))
    );

    document.querySelectorAll(".dossier-tab").forEach((btn) => {
      btn.addEventListener("click", () => setDossierTab(btn.dataset.tab));
    });
    document.querySelectorAll(".side-tab").forEach((btn) => {
      btn.addEventListener("click", () => setSideTab(btn.dataset.sideTab));
    });
    setSideTab("action");

    refreshSimStatus().catch(() => {});

    document.addEventListener("keydown", (ev) => {
      if (busy || !playState?.found) return;
      if (ev.target && /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
      const n = Number(ev.key);
      if (n >= 1 && n <= 9) {
        const adj = playState.adjacent || [];
        if (adj[n - 1]) doMove(adj[n - 1]);
      }
    });

    window.addEventListener("resize", () => {
      if (playState) {
        renderTownMap(
          playState.map || { locations: [], edges: [] },
          playState.current_location?.name,
          playState.adjacent || []
        );
      }
    });

    try {
      setStatus("Connecting...");
      await api("/api/health");
      setStatus("Connected · loading world...");
      // do not block boot on LLM status (avoids hang while loading model)
      api("/api/llm/status")
        .then((llm) => {
          const badge = $("llm-badge");
          if (llm.available) {
            badge.textContent = llm.loaded ? "LLM loaded" : "LLM ready";
            badge.className = "llm-badge on";
            badge.title = llm.message || "";
          } else {
            badge.textContent = "Knowledge replies";
            badge.className = "llm-badge off";
            badge.title = llm.message || "";
          }
        })
        .catch(() => {
          const badge = $("llm-badge");
          badge.textContent = "LLM ?";
          badge.className = "llm-badge off";
        });
      await loadPlayState();
      setStatus("Connected");
    } catch (e) {
      setStatus(e.message || "Connection failed", true);
      $("gate").classList.add("visible");
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
