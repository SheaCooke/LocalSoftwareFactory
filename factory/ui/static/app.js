// Software Factory UI: plain JS, no build step.
// All server data is rendered with textContent (never innerHTML) so artifact and
// LLM output cannot inject markup.
"use strict";

const state = {
  view: "runs",
  status: null,
  models: null,
  pipelines: [],
  runs: [],
  runId: null,
  run: null,
  events: [],
  tab: "activity",
  artifact: null,
  diffRef: null,
  agentFilter: "all",
  kinds: new Set(["message", "tool", "denied", "flow"]),
};

// ------------------------------------------------------------------ helpers

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (options.body !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  const type = res.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || data || res.statusText);
  return data;
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 2500);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const fmtTokens = (n) => (n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n));
const fmtCost = (c) => `$${(c || 0).toFixed(c >= 1 ? 2 : 4)}`;
const statusLabel = (s) => (s || "").replace("_", " ");

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function aliasOptions(select, { blank, selected }) {
  select.replaceChildren();
  if (blank) select.append(h("option", { value: "" }, blank));
  for (const a of state.models ? state.models.aliases : []) {
    select.append(h("option", { value: a.alias }, `${a.alias} (${a.provider}/${a.model})`));
  }
  select.value = selected || "";
}

// ------------------------------------------------------------------ banners

function renderBanners() {
  const s = state.status;
  const box = $("banners");
  box.replaceChildren();
  if (!s) return;
  if (s.config_error) {
    box.append(h("div", { class: "banner error" }, h("strong", {}, "Config error, last good config still active:"), h("pre", {}, s.config_error)));
  }
  for (const e of s.agent_errors) {
    box.append(h("div", { class: "banner error" }, h("strong", {}, `Agent "${e.name}" is invalid:`), e.message, h("span", { class: "muted" }, `  (${e.path})`)));
  }
  for (const e of s.pipeline_errors) {
    box.append(h("div", { class: "banner warn" }, h("strong", {}, `Pipeline ${e.file}:`), e.message));
  }
  if (s.sandbox && s.sandbox.reduced) {
    box.append(h("div", { class: "banner warn" }, h("strong", {}, "Reduced isolation:"), s.sandbox.message, ". Commands run in a restricted subprocess, not Docker."));
  }
  const badge = $("approvals-badge");
  badge.hidden = !s.pending_approvals;
  badge.textContent = `${s.pending_approvals} waiting for you`;
}

// ------------------------------------------------------------------- models

function renderModels() {
  const m = state.models;
  if (!m) return;
  const def = $("default-model");
  aliasOptions(def, { selected: m.default });
  def.title = m.default_source === "runtime" ? `Set at runtime (factory.yaml default: ${m.config_default})` : "From factory.yaml";
  aliasOptions($("run-model-select"), { blank: "Each agent's own model", selected: $("run-model-select").value });

  const agents = $("agents-table");
  agents.replaceChildren(
    h("tr", {}, h("th", {}, "Agent"), h("th", {}, "Level"), h("th", {}, "agent.md default"), h("th", {}, "Override"), h("th", {}, "Next call uses")),
  );
  for (const a of m.agents) {
    const select = h("select", { "aria-label": `Model override for ${a.name}` });
    aliasOptions(select, { blank: `None (use ${a.agent_default || "global default"})`, selected: a.override || "" });
    select.addEventListener("change", async () => {
      try {
        state.models = await api(`/api/models/agents/${encodeURIComponent(a.name)}`, { body: { alias: select.value || null } });
        renderModels();
        toast(`${a.name}: ${select.value || "override cleared"}. Applies to the next LLM call`);
      } catch (err) {
        toast(err.message, true);
      }
    });
    agents.append(
      h("tr", {},
        h("td", {}, h("strong", {}, a.name), h("div", { class: "source" }, a.description)),
        h("td", {}, a.isolation_level),
        h("td", {}, a.agent_default || h("span", { class: "muted" }, "-")),
        h("td", {}, select),
        h("td", {}, h("strong", {}, a.effective), h("div", { class: "source" }, statusLabel(a.source))),
      ),
    );
  }
  const aliases = $("aliases-table");
  aliases.replaceChildren(h("tr", {}, h("th", {}, "Alias"), h("th", {}, "Provider"), h("th", {}, "Model"), h("th", {}, "Tools"), h("th", { class: "num" }, "Context"), h("th", {}, "Description")));
  for (const a of m.aliases) {
    aliases.append(
      h("tr", {},
        h("td", {}, h("strong", {}, a.alias), a.alias === m.default ? h("span", { class: "pill", style: "margin-left:6px" }, "default") : null),
        h("td", {}, `${a.provider} (${a.provider_type})`),
        h("td", {}, h("code", {}, a.model)),
        h("td", {}, a.supports_tools ? "native" : "JSON protocol"),
        h("td", { class: "num" }, a.context_window.toLocaleString()),
        h("td", { class: "muted" }, a.description),
      ),
    );
  }
  if (state.run) renderRunHead();
}

$("default-model").addEventListener("change", async (e) => {
  try {
    state.models = await api("/api/models/default", { body: { alias: e.target.value } });
    renderModels();
    const hint = state.run && state.run.model_override ? ` (this run uses its own model: ${state.run.model_override})` : "";
    toast(`Default model: ${e.target.value}. Applies to the next LLM call${hint}`);
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------------------------------------------------------------- pipelines

function renderPipelines() {
  const select = $("pipeline-select");
  const previous = select.value || "default";
  select.replaceChildren(...state.pipelines.filter((p) => p.stages.length).map((p) => h("option", { value: p.name }, p.name)));
  select.value = state.pipelines.some((p) => p.name === previous) ? previous : (state.pipelines[0] || {}).name;
  renderPipelinePreview();
}

function renderPipelinePreview() {
  const p = state.pipelines.find((x) => x.name === $("pipeline-select").value);
  const box = $("pipeline-preview");
  box.replaceChildren();
  if (!p) return;
  if (p.description) box.append(h("div", {}, p.description));
  box.append(h("div", {}, p.stages.map((s) => s.id + (s.gate === "human_approval" ? " ✋" : "")).join(" → ")));
  if (p.error) box.append(h("div", { class: "err" }, p.error));
}
$("pipeline-select").addEventListener("change", renderPipelinePreview);

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const btn = $("start-btn");
  btn.disabled = true;
  try {
    const run = await api("/api/runs", {
      body: { request: form.get("request"), pipeline: form.get("pipeline"), repo: form.get("repo") || null, model: form.get("model") || null },
    });
    const pipeline = form.get("pipeline");
    e.target.reset();
    $("pipeline-select").value = pipeline; // keep the user's pipeline choice
    renderPipelinePreview();
    await refreshRuns();
    selectRun(run.id);
    toast(`Run ${run.id} started`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------------ run list

async function refreshRuns() {
  state.runs = await api("/api/runs");
  renderRunList();
}

function renderRunList() {
  const list = $("run-list");
  list.replaceChildren();
  if (!state.runs.length) list.append(h("li", { class: "muted" }, "No runs yet."));
  for (const r of state.runs) {
    list.append(
      h("li", {},
        h("button", {
          type: "button",
          class: r.id === state.runId ? "selected" : "",
          "aria-label": `${r.request.split("\n")[0]}: ${statusLabel(r.status)}`,
          "aria-current": r.id === state.runId ? "true" : null,
          onclick: () => selectRun(r.id),
        },
          h("span", { class: `dot ${r.status}` }),
          h("span", { class: "req", title: r.request }, r.request.split("\n")[0]),
          h("span", { class: "sub" }, `${r.pipeline} · ${statusLabel(r.status)}${r.current_stage ? " @ " + r.current_stage : ""} · ${fmtAgo(r.created_at)} · ${fmtTokens(r.tokens)} tok`),
        ),
      ),
    );
  }
}

// ------------------------------------------------------------------ run view

async function selectRun(runId) {
  state.runId = runId;
  state.artifact = null;
  state.diffRef = null;
  history.replaceState(null, "", `#run=${encodeURIComponent(runId)}`);
  setView("runs");
  renderRunList();
  try {
    const [run, events] = await Promise.all([api(`/api/runs/${runId}`), api(`/api/runs/${runId}/events?limit=3000`)]);
    if (state.runId !== runId) return;
    state.run = run;
    state.events = events;
    renderRun();
    renderActivity(true);
  } catch (err) {
    toast(err.message, true);
  }
}

const refreshRun = debounce(async () => {
  if (!state.runId) return;
  try {
    state.run = await api(`/api/runs/${state.runId}`);
    renderRun();
  } catch (err) {
    /* run may have been removed */
  }
}, 250);

function renderRun() {
  $("empty").hidden = true;
  $("run-view").hidden = false;
  renderRunHead();
  renderGraph();
  renderApprovals();
  renderAgentFilter();
  if (state.tab === "artifacts") renderArtifacts();
  if (state.tab === "usage") renderUsage();
}

function renderRunHead() {
  const r = state.run;
  if (!r) return;
  $("run-request").textContent = r.request.split("\n")[0];
  $("run-request").title = r.request;
  const meta = $("run-meta");
  meta.replaceChildren(
    ...[
      h("span", { class: `pill ${r.status}` }, statusLabel(r.status)),
      h("span", {}, `pipeline ${r.pipeline}`),
      h("span", {}, r.id),
      h("span", {}, r.repo ? `repo ${r.repo}` : "new project"),
      h("span", {}, `${fmtTokens(r.total.tokens)} tokens · ${fmtCost(r.total.cost_usd)}`),
      r.isolation ? h("span", {}, `sandbox: ${r.isolation.backend}${r.isolation.reduced ? " (reduced)" : ""}`) : null,
      r.network ? h("span", {}, `network: ${r.network}`) : null,
    ].filter(Boolean),
  );
  if (r.error) meta.append(h("span", { style: "color:var(--failed);flex-basis:100%" }, r.error));
  if (r.delivery) meta.append(h("span", { style: "color:var(--passed);flex-basis:100%" }, `Delivered ${r.delivery.commit.slice(0, 10)} as ${r.delivery.branch}. Next: ${r.delivery.next_step}`));
  aliasOptions($("run-override"), { blank: "Agents' own models", selected: r.model_override || "" });
  $("resume-btn").hidden = r.active || !["failed", "running", "pending"].includes(r.status);
  $("resume-btn").textContent = r.status === "failed" ? "Retry failed stage" : "Resume";
}

$("run-override").addEventListener("change", async (e) => {
  try {
    state.run = await api(`/api/runs/${state.runId}/model`, { body: { alias: e.target.value || null } });
    renderRun();
    toast(e.target.value ? `This run now uses ${e.target.value} for every agent (next LLM call)` : "Run override cleared");
  } catch (err) {
    toast(err.message, true);
  }
});

$("resume-btn").addEventListener("click", async () => {
  try {
    state.run = await api(`/api/runs/${state.runId}/resume`, { body: {} });
    renderRun();
  } catch (err) {
    toast(err.message, true);
  }
});

function renderGraph() {
  const graph = $("graph");
  graph.replaceChildren();
  const stages = state.run.stages;
  stages.forEach((s, i) => {
    const badges = [];
    if (s.gate === "human_approval") badges.push(s.gate_before ? "approval first" : "approval");
    if (s.gate === "check") badges.push("check");
    if (s.gate === "artifact_valid") badges.push("schema");
    if (s.pass_when.length) badges.push("verdict");
    if (s.when.length) badges.push("conditional");
    if (s.on_fail && s.on_fail.retry) badges.push(`retry ${s.retries}/${s.on_fail.retry}`);
    if (s.on_fail && s.on_fail.goto) badges.push(`loop ${s.loops}/${s.on_fail.max_loops || 1}`);
    if (s.attempts > 1) badges.push(`${s.attempts} attempts`);
    if (s.tokens) badges.push(`${fmtTokens(s.tokens)} tok`);
    const node = h("div",
      { class: `stage ${s.status}${s.current && !["passed", "failed"].includes(state.run.status) ? " current" : ""}`, title: s.error || "", tabindex: "0" },
      h("div", { class: "name" }, h("span", {}, s.id)),
      h("div", { class: "status" }, statusLabel(s.status)),
      h("div", { class: "agents" }, (s.agents.length ? s.agents : [s.type]).map((a) => h("div", { class: "agent" }, s.type === "parallel" ? `∥ ${a}` : a))),
      badges.length ? h("div", { class: "badges" }, badges.map((b) => h("span", { class: "tag" }, b))) : null,
      s.on_fail && s.on_fail.goto ? h("div", { class: "loopback" }, `↺ on fail: back to ${s.on_fail.goto}`) : null,
      s.on_fail && s.on_fail.then ? h("div", { class: "loopback" }, `↷ on fail: ${s.on_fail.then}`) : null,
    );
    node.addEventListener("click", () => {
      state.agentFilter = s.agents.length === 1 ? s.agents[0] : "all";
      setTab("activity");
      renderAgentFilter();
      renderActivity(true);
    });
    graph.append(h("div", { class: "stage-wrap" }, i ? h("span", { class: "arrow" }) : null, node));
  });
  const current = graph.querySelector(".stage.current");
  if (current) current.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
}

function renderApprovals() {
  const box = $("approvals");
  box.replaceChildren();
  for (const a of state.run.approvals.filter((x) => x.status === "pending")) {
    const comment = h("textarea", { rows: "3", placeholder: a.kind === "network" ? "Optional note" : "Comment (sent to the agent with “Request changes”)" });
    const decide = async (decision) => {
      if (decision === "changes_requested" && !comment.value.trim()) {
        comment.focus();
        toast("Write what should change first", true);
        return;
      }
      try {
        const res = await api(`/api/runs/${state.runId}/decision`, { body: { decision, comment: comment.value || null, approval_id: a.id } });
        state.run = res.run;
        renderRun();
        toast(`${decision.replace("_", " ")}${a.kind === "network" ? "" : ". Run continues"}`);
      } catch (err) {
        toast(err.message, true);
      }
    };
    const artifacts = (a.details && a.details.artifacts) || [];
    box.append(
      h("div", { class: "approval" },
        h("div", { class: "title" }, h("span", { class: "kind" }, `${a.kind} #${a.id}`), a.summary),
        artifacts.length
          ? h("div", { class: "links" }, "Review:", artifacts.map((name) => h("button", { type: "button", onclick: () => openArtifact(name) }, name)))
          : null,
        a.kind === "network" ? h("div", { class: "links" }, "The agent is waiting. Approving allows network access in the Docker sandbox for the rest of this run.") : null,
        comment,
        h("div", { class: "buttons" },
          h("button", { type: "button", class: "approve", onclick: () => decide("approved") }, "Approve"),
          a.kind === "gate" ? h("button", { type: "button", onclick: () => decide("changes_requested") }, "Request changes") : null,
          h("button", { type: "button", class: "reject", onclick: () => decide("rejected") }, "Reject"),
        ),
      ),
    );
  }
}

// ------------------------------------------------------------------ activity

function eventKind(e) {
  switch (e.type) {
    case "agent.message": return "message";
    case "tool.call": return "tool";
    case "llm.call": return "llm";
    case "permission.decision": return e.data.decision === "denied" ? "denied" : "allowed";
    case "artifact.written": return "tool";
    case "agent.started": case "agent.finished": case "stage.transition": case "gate.evaluated":
    case "approval.requested": case "approval.decided": case "budget.exceeded": case "stage.retry":
    case "stage.loop": case "stage.continue": case "stage.skipped": case "isolation.status":
    case "workspace.landed": case "run.status": case "run.delivered": case "run.model": case "run.created":
      return "flow";
    default: return null;
  }
}

function describe(e) {
  const d = e.data || {};
  switch (e.type) {
    case "agent.message": return { text: d.content };
    case "tool.call": {
      const args = Object.entries(d.arguments || {}).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v.length > 80 ? v.slice(0, 80) + "…" : v) : JSON.stringify(v)}`).join(" ");
      return { node: h("span", {}, `${d.tool}(${args}) `, h("span", { class: d.ok ? "ok" : "err" }, d.ok ? "→ ok" : "→ error"), d.ok ? "" : ` ${d.result_preview || ""}`) };
    }
    case "artifact.written": return { text: `wrote artifact ${d.name} (${d.chars} chars)` };
    case "llm.call": return { text: `${d.alias} (${d.provider}/${d.model}, via ${statusLabel(d.source)}) · in ${d.input_tokens} · out ${d.output_tokens} · ${fmtCost(d.cost_usd)} · ${Math.round(d.latency_ms)} ms` };
    case "permission.decision":
      return d.decision === "denied" ? { text: `DENIED ${d.tool} ${d.target}: ${d.reason}` } : { text: `allowed ${d.tool} ${d.target} (${d.reason})` };
    case "agent.started": return { text: `started (attempt ${d.attempt})` };
    case "agent.finished": return { text: `finished: ${d.status}${d.error ? " (" + d.error + ")" : ""} · ${d.iterations} turns · ${fmtTokens(d.input_tokens + d.output_tokens)} tokens`, tone: d.status === "passed" ? "good" : "bad" };
    case "stage.transition": return { text: `stage ${e.stage}: ${statusLabel(d.to)}${d.error ? " (" + d.error.split("\n")[0] + ")" : ""}`, tone: { passed: "good", failed: "bad", waiting_approval: "warn" }[d.to] };
    case "gate.evaluated": return { text: `gate ${d.gate} ${d.passed ? "passed" : "FAILED"}${(d.detail || []).length ? ": " + d.detail.join("; ") : ""}`, tone: d.passed ? "good" : "bad" };
    case "approval.requested": return { text: `waiting for approval #${d.id} (${d.kind}): ${d.summary}`, tone: "warn" };
    case "approval.decided": return { text: `approval #${d.id}: ${statusLabel(d.decision)}${d.comment ? ` ("${d.comment}")` : ""}`, tone: d.decision === "approved" ? "good" : "warn" };
    case "budget.exceeded": return { text: `budget exceeded: ${d.message}`, tone: "warn" };
    case "stage.retry": return { text: `retry ${d.retry}/${d.of} of ${e.stage}`, tone: "warn" };
    case "stage.loop": return { text: `${e.stage} failed → back to ${d.goto} (loop ${d.loop}/${d.max_loops})`, tone: "warn" };
    case "stage.continue": return { text: `${e.stage} failed → continuing at ${d.then}`, tone: "warn" };
    case "stage.skipped": return { text: `skipped: ${(d.why || []).join("; ")}` };
    case "isolation.status": return { text: d.message, tone: d.reduced ? "warn" : "good" };
    case "workspace.landed": return d.merged_to_integration ? { text: `merged ${String(d.commit).slice(0, 10)} into integration` } : null;
    case "run.status": return { text: `run ${statusLabel(d.status)}${d.error ? ": " + d.error : ""}`, tone: { passed: "good", failed: "bad", waiting_approval: "warn" }[d.status] };
    case "run.delivered": return { text: `delivered ${String(d.commit).slice(0, 10)} as ${d.branch}`, tone: "good" };
    case "run.model": return { text: `run model set to ${d.model_override || "agents' own models"}` };
    case "run.created": return { text: `run created (${d.pipeline})` };
    default: return null;
  }
}

function eventNode(e) {
  const kind = eventKind(e);
  if (!kind || !state.kinds.has(kind)) return null;
  if (state.agentFilter !== "all" && e.agent !== state.agentFilter) return null;
  const desc = describe(e);
  if (!desc) return null;
  let body;
  if (desc.node) body = h("div", { class: "body" }, desc.node);
  else {
    const full = desc.text || "";
    const long = full.length > 1500;
    body = h("div", { class: "body" }, long ? full.slice(0, 1500) + "…" : full);
    if (long) {
      const more = h("button", { type: "button", class: "more", onclick: () => { body.textContent = full; } }, "show all");
      body.append(more);
    }
  }
  return h("li", { class: `ev ${kind}${desc.tone ? " " + desc.tone : ""}` },
    h("span", { class: "time" }, fmtTime(e.ts)),
    h("span", { class: "who", title: e.agent || e.stage || "" }, e.agent || e.stage || "factory"),
    body,
  );
}

function renderActivity(scrollToEnd) {
  const list = $("activity");
  list.replaceChildren(...state.events.map(eventNode).filter(Boolean));
  if (scrollToEnd) list.scrollTop = list.scrollHeight;
}

function appendEvent(e) {
  state.events.push(e);
  if (state.events.length > 5000) state.events.splice(0, 1000);
  const list = $("activity");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  const node = eventNode(e);
  if (node) list.append(node);
  if (atBottom) list.scrollTop = list.scrollHeight;
  if (e.agent && !$("agent-filter").querySelector(`option[value="${CSS.escape(e.agent)}"]`)) renderAgentFilter();
}

function renderAgentFilter() {
  const select = $("agent-filter");
  const agents = new Set();
  for (const s of state.run ? state.run.stages : []) s.agents.forEach((a) => agents.add(a));
  for (const e of state.events) if (e.agent) agents.add(e.agent);
  select.replaceChildren(h("option", { value: "all" }, "All agents"), ...[...agents].sort().map((a) => h("option", { value: a }, a)));
  select.value = agents.has(state.agentFilter) ? state.agentFilter : "all";
}
$("agent-filter").addEventListener("change", (e) => {
  state.agentFilter = e.target.value;
  renderActivity(true);
});
for (const box of document.querySelectorAll(".filters input[type=checkbox]")) {
  box.addEventListener("change", () => {
    if (box.checked) state.kinds.add(box.dataset.kind);
    else state.kinds.delete(box.dataset.kind);
    renderActivity(true);
  });
}

// ----------------------------------------------------------------- artifacts

function renderArtifacts() {
  const list = $("artifact-list");
  list.replaceChildren();
  const byStage = new Map();
  for (const a of state.run.artifacts) {
    const key = a.stage || (a.agent === "user" ? "request" : "other");
    if (!byStage.has(key)) byStage.set(key, []);
    byStage.get(key).push(a);
  }
  const order = ["request", ...state.run.stages.map((s) => s.id), "other"];
  for (const key of order) {
    const items = byStage.get(key);
    if (!items) continue;
    list.append(h("li", { class: "group" }, key));
    for (const a of items) {
      list.append(h("li", {},
        h("button", { type: "button", class: a.name === state.artifact ? "selected" : "", onclick: () => openArtifact(a.name) },
          h("span", {}, a.name), h("span", { class: "small" }, `${a.agent || ""} · ${a.size} B`))));
    }
  }
  if (!state.run.artifacts.length) list.append(h("li", { class: "muted" }, "No artifacts yet."));
}

async function openArtifact(name) {
  state.artifact = name;
  setTab("artifacts");
  renderArtifacts();
  $("artifact-title").textContent = name;
  try {
    let text = await api(`/api/runs/${state.runId}/artifacts/${name.split("/").map(encodeURIComponent).join("/")}`);
    if (name.endsWith(".json")) {
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* show as-is */ }
    }
    $("artifact-body").textContent = text;
  } catch (err) {
    $("artifact-body").textContent = err.message;
  }
}

// ------------------------------------------------------------------- changes

async function renderChanges() {
  const box = $("change-refs");
  box.replaceChildren(h("div", { class: "muted" }, "Loading…"));
  try {
    const c = await api(`/api/runs/${state.runId}/changes`);
    box.replaceChildren();
    if (!c.available) {
      box.append(h("div", { class: "muted" }, "No code workspace yet. It is created when the first agent that touches files runs."));
      return;
    }
    const button = (ref, label, small) =>
      h("button", { type: "button", class: ref === state.diffRef ? "selected" : "", onclick: () => openDiff(ref, label) }, h("span", {}, label), small ? h("span", { class: "small" }, small) : null);
    box.append(h("div", { class: "group" }, "Branches"));
    for (const r of c.refs) box.append(button(r.ref, r.label));
    if (c.stages.length) box.append(h("div", { class: "group" }, "By stage"));
    for (const s of c.stages) box.append(button(`stage:${s.stage}`, s.stage, `${s.commits.length} commit${s.commits.length > 1 ? "s" : ""}`));
    box.append(h("div", { class: "group" }, `Files on integration (${c.files.length})`));
    box.append(h("div", { class: "small", style: "white-space:pre-wrap;padding:0 8px" }, c.files.join("\n") || "(none)"));
    if (!state.diffRef) openDiff("integration", c.refs[0].label);
  } catch (err) {
    box.replaceChildren(h("div", { class: "muted" }, err.message));
  }
}

async function openDiff(ref, label) {
  state.diffRef = ref;
  $("diff-title").textContent = label;
  for (const b of $("change-refs").querySelectorAll("button")) b.classList.toggle("selected", b.textContent.startsWith(label));
  const pre = $("diff-body");
  pre.textContent = "Loading…";
  try {
    const text = await api(`/api/runs/${state.runId}/diff?ref=${encodeURIComponent(ref)}`);
    pre.replaceChildren(
      ...text.split("\n").map((line) => {
        let cls = null;
        if (line.startsWith("diff --git")) cls = "file";
        else if (line.startsWith("@@")) cls = "hunk";
        else if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
        else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
        return cls ? h("span", { class: cls }, line) : document.createTextNode(line + "\n");
      }),
    );
  } catch (err) {
    pre.textContent = err.message;
  }
}

// --------------------------------------------------------------------- usage

function renderUsage() {
  const r = state.run;
  const table = $("usage-table");
  table.replaceChildren(h("tr", {}, h("th", {}, "Agent"), h("th", { class: "num" }, "Calls"), h("th", { class: "num" }, "Input tokens"), h("th", { class: "num" }, "Output tokens"), h("th", { class: "num" }, "Cost")));
  for (const u of r.usage) {
    table.append(h("tr", {}, h("td", {}, u.agent || "-"), h("td", { class: "num" }, u.calls), h("td", { class: "num" }, u.input_tokens.toLocaleString()), h("td", { class: "num" }, u.output_tokens.toLocaleString()), h("td", { class: "num" }, fmtCost(u.cost_usd))));
  }
  table.append(h("tr", { class: "total" }, h("td", {}, "Run total"), h("td", { class: "num" }, r.total.calls), h("td", { class: "num" }, r.total.input_tokens.toLocaleString()), h("td", { class: "num" }, r.total.output_tokens.toLocaleString()), h("td", { class: "num" }, fmtCost(r.total.cost_usd))));
  table.append(h("tr", {}, h("th", {}, "Stage"), h("th", { class: "num" }, "Attempts"), h("th", { class: "num" }, "Tokens"), h("th", { class: "num" }, ""), h("th", { class: "num" }, "Cost")));
  for (const s of r.stages) {
    table.append(h("tr", {}, h("td", {}, s.id), h("td", { class: "num" }, s.attempts), h("td", { class: "num" }, s.tokens.toLocaleString()), h("td", {}), h("td", { class: "num" }, fmtCost(s.cost_usd))));
  }
}

// ---------------------------------------------------------------- tabs/views

function setTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const name of ["activity", "artifacts", "changes", "usage"]) $(`tab-${name}`).hidden = name !== tab;
  if (!state.run) return;
  if (tab === "artifacts") renderArtifacts();
  if (tab === "changes") renderChanges();
  if (tab === "usage") renderUsage();
}
for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => setTab(b.dataset.tab));

function setView(view) {
  state.view = view;
  for (const b of document.querySelectorAll(".view-btn")) b.classList.toggle("active", b.dataset.view === view);
  $("view-runs").hidden = view !== "runs";
  $("view-models").hidden = view !== "models";
  if (view === "models") loadModels();
}
for (const b of document.querySelectorAll(".view-btn")) b.addEventListener("click", () => setView(b.dataset.view));

// ---------------------------------------------------------------- live data

async function loadStatus() {
  try {
    state.status = await api("/api/status");
    renderBanners();
  } catch {
    /* server restarting */
  }
}
async function loadModels() {
  state.models = await api("/api/models");
  renderModels();
}
const refreshRunsSoon = debounce(refreshRuns, 400);
const refreshStatusSoon = debounce(loadStatus, 400);
const refreshModelsSoon = debounce(loadModels, 300);

function connect() {
  const source = new EventSource("/api/stream");
  const dot = $("live-dot");
  source.onopen = () => dot.classList.add("on");
  source.onerror = () => dot.classList.remove("on");
  source.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    if (e.run_id && e.run_id === state.runId) {
      appendEvent(e);
      if (["stage.transition", "run.status", "approval.requested", "approval.decided", "llm.call", "artifact.written", "gate.evaluated", "run.model", "run.delivered", "isolation.status"].includes(e.type)) refreshRun();
      if (state.tab === "changes" && e.type === "workspace.landed") renderChanges();
    }
    if (["run.created", "run.status", "stage.transition", "approval.requested", "approval.decided"].includes(e.type)) {
      refreshRunsSoon();
      refreshStatusSoon();
    }
    if (e.type === "model.switched" || e.type.startsWith("config.")) {
      refreshModelsSoon();
      refreshStatusSoon();
    }
    // Hot reload: files under config/ or agents/ changed on disk.
    if (e.type === "config.reloaded") toast("Config reloaded");
    if (e.type === "config.error") toast("Config error. The last good config is still active.", true);
    if (e.type === "agents.reloaded") {
      refreshModelsSoon();
      refreshStatusSoon();
      refreshPipelinesSoon();
      const changed = (e.data.changed || []).join(", ");
      const broken = Object.keys(e.data.errors || {});
      toast(broken.length ? `Agent definitions reloaded, ${broken.length} invalid` : `Agents reloaded${changed ? ": " + changed : ""}`, broken.length > 0);
    }
    if (e.type === "pipelines.reloaded") {
      refreshPipelinesSoon();
      refreshStatusSoon();
    }
  };
}

async function loadPipelines() {
  state.pipelines = await api("/api/pipelines");
  renderPipelines();
}
const refreshPipelinesSoon = debounce(loadPipelines, 300);

async function init() {
  await Promise.all([loadStatus(), loadModels(), loadPipelines(), refreshRuns()]);
  connect();
  setInterval(loadStatus, 5000); // config / agent file edits show up even without events
  setInterval(() => state.runs.length && renderRunList(), 30000); // relative times
  const match = location.hash.match(/run=([^&]+)/);
  const initial = match ? decodeURIComponent(match[1]) : null;
  if (initial && state.runs.some((r) => r.id === initial)) selectRun(initial);
}

init().catch((err) => toast(err.message, true));
