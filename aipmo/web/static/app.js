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

/* ---------- PMO Core ----------
 * 台帳とブリーフィングを読んで見せる。画面から周を回したり通知したりは
 * しない。できる操作は「担当の提案を確定する」だけ（operator のみ。
 * ボタンを隠すのは権限制御ではなく、サーバーが 403 を返す）。
 * 文字列はすべて textContent で入れる — 課題名などは外部由来で信用できない。
 *
 * Shows the ledger and briefing; it never runs a cycle or notifies. The one
 * action is confirming an assignment proposal (operator only; hiding the
 * button is not access control, the server answers 403). Everything goes in
 * through textContent because issue titles come from outside.
 */

let writableTrackers = new Set();   // 担当を書き戻せるトラッカー / trackers we can write to
let pmoProject = "";          // 選択中のプロジェクト。空は「すべて」 / "" means all
const STALE_SECONDS = 15 * 60;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function humanAge(seconds) {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

function pmoSection(title, count) {
  const wrap = el("section", "pmo-block");
  const head = el("h3", "pmo-sub", title);
  if (count != null) head.append(el("span", "pmo-count", String(count)));
  wrap.append(head);
  return wrap;
}

function taskRow(task) {
  const row = el("details", "pmo-task");
  const summary = el("summary");
  summary.append(el("span", "tag score", String(task.score)));
  summary.append(el("span", "pmo-title", task.title));
  const meta = [task.project, task.key,
    task.assignee || task.suggested_assignee && `→ ${task.suggested_assignee}`,
    task.due_date].filter(Boolean).join(" · ");
  if (meta) summary.append(el("span", "pmo-meta", meta));
  row.append(summary);
  const list = el("ul", "pmo-reasons");
  for (const reason of task.reasons || []) list.append(el("li", null, reason));
  if (task.blocked) list.append(el("li", null, "blocked"));
  row.append(list);
  return row;
}

// そのタスクのトラッカーに書き戻せるか。宛先（トラッカーと識別子）が分かり、
// そのアダプタが更新できるときだけ。
// Whether the task's own tracker can be written to: its destination is known
// and that adapter can update issues.
function canWriteBack(task) {
  return Boolean(task.tracker && task.external_id && writableTrackers.has(task.tracker));
}

function proposalRow(task) {
  const row = el("div", "pmo-proposal");
  row.append(el("div", "pmo-title", task.title));
  row.append(el("div", "pmo-meta",
    `${[task.project, task.key].filter(Boolean).join(" · ")} → ${task.suggested_assignee}`
    + ` — ${task.suggestion_reason || ""}`));
  if (canRun) {
    const button = el("button", "btn btn-approve", t("web_pmo_accept", "Confirm assignee"));
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api("/api/pmo/assignments/accept", {
          method: "POST",
          body: JSON.stringify({ ref: task.id, writeback: canWriteBack(task) }),
        });
        toast(t("web_pmo_accepted", "Confirmed."));
        await refreshPmo();
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    });
    row.append(button);
  }
  return row;
}

function renderPmo(data, decisions) {
  const host = $("pmo");
  host.replaceChildren();
  const { briefing, tasks } = data;

  // プロジェクトが2つ以上見えるときだけ、絞り込みを出す。閲覧者を1つに
  // 限定しているなら、選ぶ余地が無いので出さない。
  // The filter appears only when two or more projects are visible; a viewer
  // confined to one has nothing to choose.
  if ((data.projects || []).length > 1) {
    const select = el("select", "pmo-project");
    select.setAttribute("aria-label", t("web_pmo_all_projects", "All projects"));
    const all = el("option", null, t("web_pmo_all_projects", "All projects"));
    all.value = "";
    select.append(all);
    for (const name of data.projects) {
      const option = el("option", null, name);
      option.value = name;
      select.append(option);
    }
    select.value = pmoProject;
    select.addEventListener("change", () => {
      pmoProject = select.value;
      refreshPmo();
    });
    host.append(select);
  }

  if (!briefing) {
    host.append(empty(t("web_pmo_none", "No PMO data yet.")));
  } else {
    const top = el("div", "pmo-top");
    const level = el("span", "tag level", `${t("web_pmo_level", "Overall")}: ${briefing.overall_level}`);
    level.dataset.level = briefing.overall_level;
    top.append(level, el("span", "pmo-meta", `${briefing.active_count}`));
    host.append(top);

    const age = data.briefing_age_seconds;
    if (age != null && age > STALE_SECONDS) {
      const warn = el("div", "card-note error",
        t("web_pmo_stale", "Not updated for {age}.").replace("{age}", humanAge(age)));
      host.append(warn);
    }

    if (briefing.alerts.length) {
      const block = pmoSection(t("web_pmo_alerts", "Alerts"), briefing.alerts.length);
      for (const alert of briefing.alerts) {
        const row = el("div", "pmo-alert");
        row.dataset.severity = alert.severity;
        row.append(el("span", "tag sev", alert.severity));
        row.append(el("span", "pmo-title", alert.title));
        row.append(el("div", "pmo-meta", alert.message));
        block.append(row);
      }
      host.append(block);
    }

    if (briefing.responses && briefing.responses.length) {
      const block = pmoSection(t("web_pmo_responses", "Responses"), null);
      for (const r of briefing.responses) {
        block.append(el("div", "pmo-meta", `${r.id} · ${r.template} · ${r.status}`));
      }
      host.append(block);
    }
  }

  const proposals = tasks.filter((task) => task.suggested_assignee && !task.assignee);
  if (proposals.length) {
    const block = pmoSection(t("web_pmo_proposals", "Assignment proposals"), proposals.length);
    for (const task of proposals) block.append(proposalRow(task));
    host.append(block);
  }

  if (tasks.length) {
    const block = pmoSection(t("web_pmo_priorities", "Priorities"), tasks.length);
    for (const task of tasks.slice(0, 10)) block.append(taskRow(task));
    host.append(block);
  }

  if (briefing && briefing.member_loads && briefing.member_loads.length) {
    const block = pmoSection(t("web_pmo_members", "Member load"), null);
    for (const m of briefing.member_loads) {
      const row = el("div", "pmo-load");
      row.append(el("span", "pmo-title", m.member));
      const meter = el("progress");
      meter.max = m.capacity;
      meter.value = Math.min(m.load, m.capacity);
      if (m.load > m.capacity) meter.dataset.over = "true";
      row.append(meter, el("span", "pmo-meta", `${m.load}/${m.capacity}`));
      block.append(row);
    }
    host.append(block);
  }

  const learned = briefing && briefing.learning;
  if (learned && (Object.keys(learned.member_factor).length
                  || Object.keys(learned.label_bonus).length)) {
    const block = pmoSection(t("web_pmo_learning", "Learned adjustments"), null);
    for (const [who, factor] of Object.entries(learned.member_factor)) {
      block.append(el("div", "pmo-meta", `${who}: ×${factor}`));
    }
    for (const [label, bonus] of Object.entries(learned.label_bonus)) {
      block.append(el("div", "pmo-meta", `${label}: +${bonus}`));
    }
    host.append(block);
  }

  if (decisions.length) {
    const details = el("details", "pmo-decisions");
    details.append(el("summary", null, t("web_pmo_decisions", "Recent decisions")));
    for (const d of decisions.slice(0, 20)) {
      const { at, kind, ...rest } = d;
      const detail = Object.values(rest).filter((v) => typeof v === "string").join(" · ");
      details.append(el("div", "pmo-meta", `${clock(at)}  ${kind}  ${detail}`));
    }
    host.append(details);
  }
}

async function refreshPmo() {
  // PMO のデータが無い構成（台帳が無い・未設定）は 404。エラーではなく
  // 「使っていない機能」として、見出しごと隠す。
  // A deployment with no PMO data answers 404; that is an unused feature,
  // not an error, so the heading is hidden rather than toasting each refresh.
  try {
    const filter = pmoProject ? `project=${encodeURIComponent(pmoProject)}` : "";
    const data = await api(filter ? `/api/pmo?${filter}` : "/api/pmo");
    const { items } = await api(`/api/pmo/decisions?limit=20${filter ? "&" + filter : ""}`)
      .catch(() => ({ items: [] }));
    $("h-pmo").hidden = false;
    renderPmo(data, items);
  } catch (error) {
    $("h-pmo").hidden = true;
    $("pmo").replaceChildren();
  }
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
    writableTrackers = new Set(session.writeback || []);
    $("h-pmo").textContent = t("web_pmo", "PMO Core");
    $("h-proposals").textContent = t("web_proposals", "WBS Proposals");
    $("h-templates").textContent = t("web_templates", "Templates");
    $("h-runs").textContent = t("web_runs", "Runs");

    const { items } = await api("/api/templates");
    renderTemplates(items);
    await refreshRuns();
    await refreshPmo();
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
    refreshPmo().catch(() => {});
    refreshProposals().catch(() => {});
    refreshHealth();
  }
});
