/* AI-PMO — スマホ向け画面の挙動 / mobile interface behaviour.
 *
 * ビルド工程を持たない。自前で立てるサーバーに Node のツールチェーンを
 * 要求したくないので、素の JS のまま置く。
 * No build step: a self-hosted server should not require a Node toolchain.
 */

const $ = (id) => document.getElementById(id);

let strings = {};
let canRun = false;
const t = (key, fallback) => strings[key] || fallback;

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  if (response.status === 401) {
    location.reload();          // Cookie 失効 → 施錠画面へ / expired, show lock
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

let toastTimer;
function toast(message, kind) {
  const el = $("toast");
  el.textContent = message;
  el.dataset.kind = kind || "info";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString([], {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

/* ---------- WBS 再計画提案 / WBS replan proposals ----------
 * WBS再計画AIが作った差分は、ここでしか人が見て決められない。
 * diff の中身はテンプレートによって形が異なるため（生の予測値の場合も、
 * AIが考えた再計画差分の場合もある）、スキーマを決め打ちせず整形JSONの
 * まま見せる — 判断するPMが実際に何を承認するかそのまま読めることを
 * 優先する。
 *
 * A proposal's diff is the only place a human sees and decides on what the
 * WBS-replanning AI produced. Its shape varies by template (a raw forecast,
 * or an AI-authored replan diff), so this shows it as formatted JSON rather
 * than assuming a schema — the reviewer needs to see exactly what they
 * would be approving.
 */

const TIER_LABEL = { 1: "tier 1", 2: "tier 2", 3: "tier 3" };

function renderProposals(items) {
  const host = $("proposals");
  host.replaceChildren();

  if (!items.length) {
    host.append(empty(t("web_no_proposals", "No pending proposals.")));
    return;
  }

  for (const item of items) {
    host.append(proposalCard(item));
  }
}

function proposalCard(item) {
  const card = document.createElement("div");
  card.className = "proposal";
  card.dataset.tier = String(item.tier ?? "");

  const head = document.createElement("div");
  head.className = "card-head";

  const tag = document.createElement("span");
  tag.className = "tag tier";
  tag.textContent = TIER_LABEL[item.tier] || `tier ${item.tier ?? "?"}`;
  head.append(tag);

  const wbs = document.createElement("span");
  wbs.className = "card-name";
  wbs.textContent = item.wbs_version_from || item.id.slice(0, 8);
  head.append(wbs);

  // A/B の複数案がある場合、どの案かを示す。単一案（option_label 無し）
  // では何も表示しない -- 従来通りの見た目を保つ。
  // When multiple alternatives exist for the same wbs/tier, this shows
  // which one. A single-option proposal (no option_label) shows nothing,
  // keeping the previous appearance unchanged.
  if (item.option_label) {
    const option = document.createElement("span");
    option.className = "tag option";
    option.textContent = item.option_label;
    head.append(option);
  }

  const when = document.createElement("span");
  when.className = "run-time";
  when.textContent = clock(item.created_at);
  head.append(when);

  card.append(head);

  if (item.rationale) {
    const rationale = document.createElement("p");
    rationale.className = "proposal-rationale";
    rationale.textContent = item.rationale;
    card.append(rationale);
  }

  if (item.confidence != null) {
    const confidence = document.createElement("div");
    confidence.className = "card-note";
    confidence.textContent = `confidence: ${Math.round(item.confidence * 100)}%`;
    card.append(confidence);
  }

  if (item.assumptions && Object.keys(item.assumptions).length) {
    card.append(jsonBlock("assumptions", item.assumptions));
  }
  card.append(jsonBlock("diff", item.diff));

  if (canRun) {
    card.append(proposalActions(item));
  }

  return card;
}

function jsonBlock(label, value) {
  const details = document.createElement("details");
  details.className = "proposal-json";

  const summary = document.createElement("summary");
  summary.textContent = label;
  details.append(summary);

  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(pre);

  return details;
}

function proposalActions(item) {
  const wrap = document.createElement("div");
  wrap.className = "proposal-actions";

  const note = document.createElement("input");
  note.type = "text";
  note.className = "note-input";
  note.placeholder = t("web_proposal_note_placeholder", "Optional note");

  const approve = document.createElement("button");
  approve.className = "btn btn-approve";
  approve.textContent = t("web_approve", "Approve");
  approve.addEventListener("click", () => decideProposal(item.id, "approve", note.value, wrap));

  const reject = document.createElement("button");
  reject.className = "btn btn-reject";
  reject.textContent = t("web_reject", "Reject");
  reject.addEventListener("click", () => decideProposal(item.id, "reject", note.value, wrap));

  wrap.append(note, approve, reject);
  return wrap;
}

async function decideProposal(id, decision, note, wrap) {
  const buttons = wrap.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });

  try {
    await api(`/api/wbs-proposals/${id}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ note: note || null }),
    });
    toast(decision === "approve"
      ? t("web_proposal_approved", "Approved.")
      : t("web_proposal_rejected", "Rejected."));
    await refreshProposals();
  } catch (error) {
    toast(error.message, "error");
    buttons.forEach((b) => { b.disabled = false; });
  }
}

/* ---------- テンプレート / templates ---------- */

function renderTemplates(items) {
  const host = $("templates");
  host.replaceChildren();

  if (!items.length) {
    host.append(empty(t("web_no_templates", "No templates found.")));
    return;
  }

  if (!canRun) {
    host.append(empty(t("web_view_only", "This token can view but not run")));
  }

  for (const item of items) {
    const card = document.createElement("button");
    card.className = "card";
    // 閲覧のみの相手には押せない見た目にする。ただしこれは案内であって
    // 権限管理ではない。拒否はサーバー側が行う。
    // Viewers see it as untappable — but this is a courtesy, not access
    // control. The refusal happens on the server.
    card.disabled = !item.valid || !canRun;

    const head = document.createElement("div");
    head.className = "card-head";

    const name = document.createElement("span");
    name.className = "card-name";
    name.textContent = item.name;
    head.append(name);

    if (item.industry) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.industry;
      head.append(tag);
    }
    card.append(head);

    const note = document.createElement("div");
    if (item.valid) {
      note.className = "card-note";
      // 工程数は、押す前に規模がわかる唯一の手がかり
      // The step count is the only cue to a template's size before running it.
      note.textContent = item.description
        || `${item.steps.length} steps · ${item.trigger}`;
    } else {
      note.className = "card-note error";
      note.textContent = item.error;
    }
    card.append(note);

    if (item.valid && canRun) {
      card.addEventListener("click", () => run(item, card, note));
    }
    host.append(card);
  }
}

async function run(item, card, note) {
  const original = note.textContent;
  card.disabled = true;
  note.replaceChildren();
  const spinner = document.createElement("span");
  spinner.className = "busy";
  note.append(spinner, " ", t("web_running", "Running…"));

  try {
    const record = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ path: item.path }),
    });
    await refreshRuns();
    if (record.status === "failed") {
      toast(record.error || t("web_run_failed", "Run failed."), "error");
    } else {
      toast(t("web_run_done", "Finished."));
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    card.disabled = false;
    note.textContent = original;
  }
}

/* ---------- 実行履歴 / run history ---------- */

function renderRuns(items) {
  const host = $("runs");
  host.replaceChildren();

  if (!items.length) {
    host.append(empty(t("web_no_runs", "Nothing has run yet. Tap a template above.")));
    return;
  }

  for (const record of items) {
    host.append(runRow(record));
  }
}

function runRow(record) {
  const wrap = document.createElement("div");
  wrap.className = "run";
  wrap.dataset.status = record.status;

  const summary = document.createElement("button");
  summary.className = "run-summary";
  summary.setAttribute("aria-expanded", "false");

  const meta = document.createElement("div");
  meta.className = "run-meta";

  const name = document.createElement("span");
  name.className = "run-name";
  name.textContent = record.template;

  const id = document.createElement("span");
  id.textContent = record.id.slice(0, 6);

  const when = document.createElement("span");
  when.className = "run-time";
  when.textContent = clock(record.started_at);

  meta.append(name, id);
  if (record.started_by) {
    const who = document.createElement("span");
    who.textContent = record.started_by === "schedule" ? "auto" : record.started_by;
    meta.append(who);
  }
  meta.append(when);
  summary.append(meta, stepBar(record.steps));
  wrap.append(summary);

  const detail = document.createElement("ul");
  detail.className = "steps";
  detail.hidden = true;
  for (const step of record.steps) {
    detail.append(stepRow(step));
  }
  wrap.append(detail);

  summary.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    summary.setAttribute("aria-expanded", String(!detail.hidden));
  });

  // 失敗した実行は開いた状態で出す。確認したいのはそこなので。
  // Failed runs open by default: that is what the reader came for.
  if (record.status === "failed") {
    detail.hidden = false;
    summary.setAttribute("aria-expanded", "true");
  }
  return wrap;
}

function stepBar(steps) {
  const bar = document.createElement("div");
  bar.className = "bar";

  // 所要時間に比例させる。ただし短い工程が消えないよう下限を置く。
  // Proportional to duration, with a floor so brief steps stay visible.
  const total = steps.reduce((sum, s) => sum + Math.max(s.duration_ms, 1), 0) || 1;
  for (const step of steps) {
    const seg = document.createElement("span");
    seg.className = "seg";
    seg.dataset.status = step.status;
    seg.style.flexGrow = String(Math.max(step.duration_ms, 1) / total);
    seg.title = `${step.id} — ${step.status}`;
    bar.append(seg);
  }
  return bar;
}

const MARKS = { success: "✓", failed: "✗", skipped: "–" };

function stepRow(step) {
  const li = document.createElement("li");
  li.dataset.status = step.status;

  const mark = document.createElement("span");
  mark.className = "mark";
  mark.textContent = MARKS[step.status] || "?";

  const name = document.createElement("span");
  name.textContent = step.id;

  const dur = document.createElement("span");
  dur.className = "dur";
  dur.textContent = `${step.duration_ms}ms`;

  li.append(mark, name, dur);

  if (step.error) {
    const err = document.createElement("div");
    err.className = "step-error";
    err.textContent = step.error;
    li.append(err);
  }
  return li;
}

function empty(message) {
  const div = document.createElement("div");
  div.className = "empty";
  div.textContent = message;
  return div;
}

/* ---------- 起動 / boot ---------- */

async function refreshRuns() {
  const { items } = await api("/api/runs");
  renderRuns(items);
}

async function refreshProposals() {
  // postgres アダプタが未設定の構成もある（承認フローを使わないテナント）。
  // その場合は 503 が返るので、エラーではなく「何も無い」として扱う —
  // 使っていない機能のために毎回トーストでエラーを出すのは邪魔になる。
  //
  // Some deployments run without the postgres adapter (no approval
  // workflow in use), which returns 503. That is treated as "nothing to
  // show" rather than an error - surfacing a toast every refresh for a
  // feature that is not in use would just be noise.
  try {
    const { items } = await api("/api/wbs-proposals");
    $("h-proposals").hidden = false;
    renderProposals(items);
  } catch (error) {
    $("h-proposals").hidden = true;
    $("proposals").replaceChildren();
  }
}

async function refreshHealth() {
  try {
    const { adapters } = await api("/api/health");
    const values = Object.values(adapters);
    const ok = values.length > 0 && values.every(Boolean);
    $("pulse").dataset.state = ok ? "ok" : "down";
  } catch {
    $("pulse").dataset.state = "down";
  }
}

async function boot() {
  try {
    const session = await api("/api/session");
    strings = session.strings || {};
    canRun = Boolean(session.can_run);
    document.documentElement.lang = session.lang || "en";
    $("tenant").textContent = session.tenant;
    $("h-proposals").textContent = t("web_proposals", "WBS Proposals");
    $("h-templates").textContent = t("web_templates", "Templates");
    $("h-runs").textContent = t("web_runs", "Runs");

    const { items } = await api("/api/templates");
    renderTemplates(items);
    await refreshRuns();
    await refreshProposals();
    refreshHealth();
  } catch (error) {
    toast(error.message, "error");
  }
}

boot();

// 画面に戻ったときだけ更新する。定期ポーリングは電池を消費するので避ける。
// Refresh on return to the screen; polling on a timer would drain the battery.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshRuns().catch(() => {});
    refreshProposals().catch(() => {});
    refreshHealth();
  }
});
