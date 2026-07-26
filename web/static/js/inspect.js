(() => {
  const $ = (id) => document.getElementById(id);
  let traces = [];
  let current = null;
  let tab = "raw";

  function qsId() {
    const u = new URL(location.href);
    return u.searchParams.get("id");
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      const t = await res.text();
      throw new Error(t || res.statusText);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** @returns {"qwen"|"local"|"pddl"} */
  function providerBucket(t) {
    const kind = String(t.kind || "").toLowerCase();
    if (kind === "pddl_plan" || kind === "plan") return "pddl";
    const p = String(t.provider || (t.meta && t.meta.provider) || "").toLowerCase();
    if (p === "pddl" || p === "planner") return "pddl";
    if (p === "local_gbnf" || p === "local" || p === "llama" || p === "llamacpp") {
      return "local";
    }
    if (t.fallback_used || (t.meta && t.meta.fallback_used)) {
      return "local";
    }
    return "qwen";
  }

  function kindLabel(kind) {
    if (kind === "goal_decision") return "决策";
    if (kind === "pddl_plan") return "规划";
    if (kind === "dialogue") return "对话";
    return kind || "?";
  }

  function listTitle(t) {
    const m = t.meta || {};
    if (t.kind === "pddl_plan") {
      const who = m.character || (m.phase === "solve" ? "solver" : "?");
      const goal = m.goal || (m.phase === "solve" ? "solve" : "plan");
      const tgt = m.target_entity ? `→${m.target_entity}` : "";
      const eng = m.engine ? ` · ${m.engine}` : "";
      return `${who} · ${goal}${tgt}${eng}`;
    }
    if (t.kind === "goal_decision") {
      const who = m.character || "?";
      const step = m.step ? `/${m.step}` : "";
      return `${who}${step}`;
    }
    return m.speaker_b || m.npc || m.character || t.kind || "?";
  }

  function splitTraces() {
    const qwen = [];
    const local = [];
    const pddl = [];
    for (const t of traces) {
      const b = providerBucket(t);
      if (b === "local") local.push(t);
      else if (b === "pddl") pddl.push(t);
      else qwen.push(t);
    }
    return { qwen, local, pddl };
  }

  function itemHtml(t) {
    const active = current && current.id === t.id ? " active" : "";
    const badge = t.ok
      ? `<span class="badge ok">ok</span>`
      : `<span class="badge fail">fail</span>`;
    const who = listTitle(t);
    const ms =
      typeof t.elapsed_ms === "number" ? `${Math.round(t.elapsed_ms)}ms` : "";
    const attempt =
      t.meta && t.meta.attempt != null ? ` · #${t.meta.attempt}` : "";
    const kind = kindLabel(t.kind);
    return `<li class="${active}" data-id="${t.id}">
      <div class="row1"><span>${escapeHtml(kind)} · ${escapeHtml(who)}${escapeHtml(
      attempt
    )} · ${escapeHtml(t.ts || "")}</span>${badge}</div>
      <div class="preview">${escapeHtml(t.error || t.raw_preview || "")} ${ms}</div>
    </li>`;
  }

  function fillList(ul, items, emptyText) {
    if (!items.length) {
      ul.innerHTML = `<li class="empty-item">${escapeHtml(emptyText)}</li>`;
      return;
    }
    ul.innerHTML = items.map(itemHtml).join("");
    ul.querySelectorAll("li[data-id]").forEach((li) => {
      li.addEventListener("click", () => loadDetail(li.dataset.id));
    });
  }

  function renderList() {
    const { qwen, local, pddl } = splitTraces();
    $("qwen-count").textContent = String(qwen.length);
    $("local-count").textContent = String(local.length);
    const pddlCount = $("pddl-count");
    if (pddlCount) pddlCount.textContent = String(pddl.length);
    fillList($("trace-list-qwen"), qwen, "暂无 Qwen 记录");
    fillList($("trace-list-local"), local, "暂无本地 GBNF 记录");
    const pddlList = $("trace-list-pddl");
    if (pddlList) fillList(pddlList, pddl, "暂无 PDDL/规划记录");
  }

  function showPanel() {
    if (!current) return;
    let text = "";
    if (tab === "raw") text = current.raw || "(empty)";
    else if (tab === "prompt") text = current.prompt || "(empty)";
    else if (tab === "parsed")
      text = JSON.stringify(current.parsed ?? null, null, 2);
    else if (tab === "gbnf")
      text = JSON.stringify(current.gbnf ?? {}, null, 2);
    $("panel").textContent = text;
  }

  function renderDetail() {
    if (!current) {
      $("empty").classList.remove("hidden");
      $("detail").classList.add("hidden");
      renderList();
      return;
    }
    $("empty").classList.add("hidden");
    $("detail").classList.remove("hidden");
    const m = current.meta || {};
    const g = current.gbnf || {};
    const provider =
      m.provider ||
      g.provider ||
      providerBucket({
        kind: current.kind,
        provider: m.provider,
        meta: m,
        fallback_used: m.fallback_used,
      });
    const err = current.error
      ? `<span class="err">error: ${escapeHtml(current.error)}</span>`
      : "";
    const whoBits =
      current.kind === "pddl_plan" || current.kind === "goal_decision"
        ? `<span><strong>character</strong> ${escapeHtml(m.character || "")}</span>
           <span><strong>goal/step</strong> ${escapeHtml(
             m.goal || m.step || ""
           )}${m.target_entity ? " → " + escapeHtml(m.target_entity) : ""}</span>
           <span><strong>engine</strong> ${escapeHtml(m.engine || "")}</span>`
        : `<span><strong>a/b</strong> ${escapeHtml(m.speaker_a || "")} → ${escapeHtml(
            m.speaker_b || ""
          )}</span>
           <span><strong>msg</strong> ${escapeHtml(m.player_message || "")}</span>`;
    $("meta-row").innerHTML = `
      <span><strong>id</strong> ${escapeHtml(current.id)}</span>
      <span><strong>provider</strong> ${escapeHtml(String(provider))}</span>
      <span><strong>fallback</strong> ${!!(m.fallback_used || g.fallback_used || g.used)}</span>
      <span><strong>ok</strong> ${current.ok}</span>
      <span><strong>kind</strong> ${escapeHtml(current.kind)}</span>
      <span><strong>elapsed</strong> ${
        current.elapsed_ms != null ? Math.round(current.elapsed_ms) + "ms" : "?"
      }</span>
      <span><strong>raw_len</strong> ${current.raw_len ?? 0}</span>
      ${whoBits}
      ${err}
    `;
    showPanel();
    renderList();
  }

  async function loadDetail(id) {
    current = await api(`/api/llm/traces/${encodeURIComponent(id)}`);
    const u = new URL(location.href);
    u.searchParams.set("id", id);
    history.replaceState(null, "", u);
    renderDetail();
  }

  async function refresh() {
    const data = await api("/api/llm/traces?limit=160");
    traces = data.traces || [];
    renderList();
    const want = qsId() || (current && current.id);
    if (want && traces.some((t) => t.id === want)) {
      await loadDetail(want);
    } else if (traces[0]) {
      await loadDetail(traces[0].id);
    } else {
      current = null;
      renderDetail();
    }
  }

  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      tab = btn.dataset.tab;
      document
        .querySelectorAll(".tabs button")
        .forEach((b) => b.classList.toggle("active", b === btn));
      showPanel();
    });
  });

  $("btn-refresh").addEventListener("click", () =>
    refresh().catch((e) => alert(e.message))
  );
  $("btn-clear").addEventListener("click", async () => {
    if (!confirm("清空全部追踪（对话 / 决策 / PDDL）？")) return;
    await api("/api/llm/traces", { method: "DELETE" });
    current = null;
    await refresh();
  });

  refresh().catch((e) => {
    $("empty").textContent = e.message;
  });
})();
