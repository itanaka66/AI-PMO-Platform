/* AI-PMO — mobile interface behaviour.
 *
 * No build step: a self-hosted server should not require a Node toolchain.
 */

const $ = (id) => document.getElementById(id);

let strings = {};
let canRun = false;
const t = (key, fallback) => strings[key] || fallback;

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    credentials: "include",
    ...options,
  });
  if (response.status === 401) {
    location.reload();          // expired — redirect to lock screen
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    throw new Error((detail && typeof detail === "object" ? detail.message : detail)
      || `HTTP ${response.status}`);
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

/* ---------- WBS replan proposals ----------
 * The diff produced by the WBS replan AI can only be reviewed and decided here.
 * The diff shape varies by template (raw forecast values or AI-generated replan diff),
 * so it is shown as formatted JSON without a fixed schema — the PM reviewing it
 * needs to see exactly what they are approving.
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
  card.append(previewBlock(item));

  if (canRun) {
    card.append(proposalActions(item));
  }

  return card;
}

/* Preview how the file changes on approval. Read on open (read-only, no writes).
 * What approving would do to the file; fetched when opened, read-only. */
function previewBlock(item) {
  const details = document.createElement("details");
  details.className = "proposal-json proposal-preview";
  const summary = document.createElement("summary");
  summary.textContent = t("web_preview", "Preview changes on approval (diff after applying)");
  details.append(summary);
  const body = document.createElement("div");
  details.append(body);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    try {
      const p = await api(`/api/wbs-proposals/${encodeURIComponent(item.id)}/preview`);
      body.replaceChildren();
      if (!p.applicable) {
        body.append(el("div", "card-note error", (p.problems && p.problems.length)
          ? p.problems.join(" / ") : t("web_preview_none", "This proposal cannot be applied to the file")));
        return;
      }
      if (p.already_applied) body.append(el("div", "card-note", t("web_preview_applied", "Already applied")));
      for (const line of p.report) body.append(el("div", "pmo-meta", line));
      const pre = document.createElement("pre");
      pre.className = "preview-diff";
      pre.textContent = p.diff || "";
      body.append(pre);
    } catch (error) {
      loaded = false;
      body.replaceChildren(el("div", "card-note error", error.message));
    }
  });
  return details;
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
    const result = await api(`/api/wbs-proposals/${id}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ note: note || null }),
    });
    // With a target WBS file, approving also applies; say what happened.
    if (decision === "approve" && result && result.applied === true) {
      toast(t("web_proposal_applied", "Approved and applied to the WBS file."));
    } else if (decision === "approve" && result && result.applied === false) {
      toast(result.error || "applied: false", "error");
    } else {
      toast(decision === "approve"
        ? t("web_proposal_approved", "Approved.")
        : t("web_proposal_rejected", "Rejected."));
    }
    await refreshProposals();
  } catch (error) {
    toast(error.message, "error");
    buttons.forEach((b) => { b.disabled = false; });
  }
}

/* ---------- templates ---------- */

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

/* ---------- run-progress dialog -------------------------
 *
 * `/api/runs` immediately returns `status: queued`; actual execution is
 * handled by a single worker queue (so LLM steps never overlap).
 * This polls `GET /api/runs/{id}` every few hundred ms and shows
 * queue position → current step in the dialog.
 *
 * `/api/runs` only returns `status: queued` right away; the actual run is
 * drained from a single-worker queue (so LLM-calling steps never overlap).
 * This polls `GET /api/runs/{id}` every few hundred ms and reflects it in
 * the dialog, from queue position through to the current step.
 */

function updateRunDialog(record) {
  const title = $("run-progress-title");
  const meter = $("run-progress-meter");
  const step = $("run-progress-step");

  if (record.status === "queued") {
    title.textContent = t("web_run_progress_title", "Running");
    meter.removeAttribute("value");
    step.textContent = record.queue_position
      ? t("web_run_queue_wait", "{n} ahead of you…").replace("{n}", String(record.queue_position))
      : "";
    return;
  }

  title.textContent = t("web_run_progress_title", "Running");
  const progress = record.progress;
  if (progress && progress.step_id) {
    meter.max = progress.total;
    meter.value = progress.index;
    step.textContent = t("web_run_step", "Step {i}/{n}: {step}")
      .replace("{i}", String(progress.index))
      .replace("{n}", String(progress.total))
      .replace("{step}", progress.step_id);
  } else {
    meter.removeAttribute("value");
    step.textContent = "";
  }
}

function renderRunProgressQueue(status, selfRunId) {
  const list = $("run-progress-queue");
  const rows = [];
  if (status.running) {
    rows.push({ id: status.running.id, state: "running", label: status.running.template });
  }
  status.waiting.forEach((item, index) => {
    rows.push({ id: item.id, state: "waiting", label: `${index + 1}. ${item.template}` });
  });
  list.replaceChildren(...rows.map((row) => {
    const li = document.createElement("li");
    li.dataset.state = row.state;
    if (row.id === selfRunId) li.dataset.self = "true";
    li.textContent = row.label;
    return li;
  }));
  list.hidden = rows.length === 0;
}

async function watchRun(runId) {
  const dialog = $("run-progress");
  const supportsDialog = dialog && typeof dialog.showModal === "function";
  if (supportsDialog && !dialog.open) dialog.showModal();

  try {
    //
    // Polling here is fine because this run was started by the person
    // watching it (not a standing background loop). Gives up after
    // repeated failures so a dead server never spins forever.
    let failures = 0;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      let record;
      try {
        record = await api(`/api/runs/${runId}`);
        failures = 0;
      } catch (error) {
        failures += 1;
        if (failures >= 5) throw error;
        continue;
      }
      if (record.status !== "queued" && record.status !== "running") {
        return record;
      }
      updateRunDialog(record);
      try {
        renderRunProgressQueue(await api("/api/queue"), runId);
      } catch (error) {
        // A failed queue read never interrupts the run's own progress.
      }
    }
  } finally {
    if (supportsDialog && dialog.open) dialog.close();
    $("run-progress-queue").replaceChildren();
    $("run-progress-queue").hidden = true;
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
    const started = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ path: item.path }),
    });
    const record = await watchRun(started.id);
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

/* ---------- run timeline (Gantt-style) ------------------
 *
 * Completed runs (success/failure) are start–end bars, in-progress runs
 * extend to "now" with a striped bar, and queued runs (no timestamps)
 * are listed numerically outside the axis.
 *
 * Done (success/failed) runs get a bar from start to finish; the running one
 * gets a striped bar stretching to "now"; queued runs have no timestamp yet,
 * so they're listed, numbered, outside the time axis instead.
 */
function renderRunTimeline(items) {
  const host = $("run-timeline");
  const timed = items.filter((r) => r.started_at && r.status !== "queued");
  const queued = items.filter((r) => r.status === "queued")
    .sort((a, b) => (a.queue_position || 0) - (b.queue_position || 0));

  if (timed.length === 0 && queued.length === 0) {
    host.hidden = true;
    return;
  }
  host.hidden = false;

  const now = Date.now();
  let start = now;
  let end = now;
  for (const record of timed) {
    const s = new Date(record.started_at).getTime();
    const e = record.finished_at ? new Date(record.finished_at).getTime() : now;
    if (s < start) start = s;
    if (e > end) end = e;
  }
  if (end - start < 60000) start = end - 60000;   // avoid bars too narrow to see
  const span = end - start;

  $("run-timeline-axis").replaceChildren(
    el("span", null, clock(new Date(start).toISOString())),
    el("span", null, clock(new Date(end).toISOString())));

  $("run-timeline-rows").replaceChildren(...timed.map((record) => {
    const row = el("div", "run-timeline-row");
    row.append(el("span", "run-timeline-label", record.template));
    const track = el("div", "run-timeline-track");
    const bar = el("div", "run-timeline-bar");
    bar.dataset.status = record.status;
    const s = new Date(record.started_at).getTime();
    const e = record.finished_at ? new Date(record.finished_at).getTime() : now;
    bar.style.left = `${((s - start) / span) * 100}%`;
    bar.style.width = `${Math.max(((e - s) / span) * 100, 0.6)}%`;
    track.append(bar);
    row.append(track);
    return row;
  }));

  const upcoming = $("run-timeline-upcoming");
  upcoming.replaceChildren(...queued.map((record, index) => {
    const li = document.createElement("li");
    li.textContent = `${record.queue_position || index + 1}. ${record.template}`;
    return li;
  }));
  upcoming.hidden = queued.length === 0;
}

/* ---------- run history ---------- */

function renderRuns(items) {
  renderRunTimeline(items);
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
 * Reads the ledger and briefing. Does not trigger cycles or notifications.
 * The only write action is confirming an assignment proposal (operator only).
 * Hiding the button is not access control — the server returns 403.
 * All strings are set via textContent — issue titles etc. come from external sources.
 *
 * Shows the ledger and briefing; it never runs a cycle or notifies. The one
 * action is confirming an assignment proposal (operator only; hiding the
 * button is not access control, the server answers 403). Everything goes in
 * through textContent because issue titles come from outside.
 */

let writableTrackers = new Set();   // trackers we can write back assignees to
let pmoProject = "";          // selected project; "" means all
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

  // What a role AI did for this task: the latest run only, with its result or
  // reason. Goes in through textContent, since it is text from outside.
  const last = (task.dispatches || []).slice(-1)[0];
  if (last) {
    const note = el("div", "pmo-agent", `${t("web_pmo_agent", "Role AI")} ${last.agent}: ${last.status}`);
    note.dataset.status = last.status;
    row.append(note);
    if (last.review) {
      const verdict = last.review.decision === "accepted" ? t("web_pmo_review_accepted", "accepted")
        : t("web_pmo_review_rejected", "sent back");
      row.append(el("div", "pmo-meta", `${verdict} · ${last.review.by}${last.review.note ? ` — ${last.review.note}` : ""}`));
    }
    const text = last.error || last.excerpt;
    if (text) row.append(el("div", "pmo-meta pmo-agent-result", text));
  }
  return row;
}

// Whether the task's own tracker can be written to: its destination is known
// and that adapter can update issues.
function canWriteBack(task) {
  return Boolean(task.tracker && task.external_id && writableTrackers.has(task.tracker));
}

function generatedRow(item) {
  const row = el("div", "pmo-proposal");
  row.append(el("div", "pmo-title", item.title));
  row.append(el("div", "pmo-meta", [item.project, item.priority, item.due_date]
    .filter(Boolean).join(" · ")));
  if (canRun) {
    const actions = el("div", "pmo-actions");
    for (const [decision, label, kind] of [
      ["approve", t("web_approve", "Approve"), "btn btn-approve"],
      ["reject", t("web_reject", "Reject"), "btn btn-reject"]]) {
      const button = el("button", kind, label);
      button.addEventListener("click", async () => {
        actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        try {
          await api("/api/pmo/proposals/decide", {
            method: "POST", body: JSON.stringify({ ref: item.id, decision }),
          });
          toast(t("web_pmo_decided", "Done."));
          await refreshPmo();
        } catch (error) {
          toast(error.message, "error");
          actions.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        }
      });
      actions.append(button);
    }
    row.append(actions);
  }
  return row;
}

// Filing a task into the tracker (operator only): it creates an issue in the outside
// world, so only on a click. Skipping removes it from this list.
function filingRow(item, tracker, canFile) {
  const row = el("div", "pmo-proposal");
  row.append(el("div", "pmo-title", item.title));
  row.append(el("div", "pmo-meta", [item.project, item.origin, item.priority, item.due_date]
    .filter(Boolean).join(" · ")));
  if (item.error) row.append(el("div", "card-note error", item.error));
  if (canRun) {
    const actions = el("div", "pmo-actions");
    const choices = [["skip", t("web_pmo_skip_filing", "Skip"), "btn btn-reject"]];
    if (canFile) choices.unshift(["file", `${t("web_pmo_file", "File")} → ${tracker}`, "btn btn-approve"]);
    for (const [decision, label, kind] of choices) {
      const button = el("button", kind, label);
      button.addEventListener("click", async () => {
        actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        try {
          await api("/api/pmo/filing", {
            method: "POST", body: JSON.stringify({ ref: item.id, decision }),
          });
          toast(t("web_pmo_filed", "Filed."));
          await refreshPmo();
        } catch (error) {
          toast(error.message, "error");
          actions.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        }
      });
      actions.append(button);
    }
    row.append(actions);
  }
  return row;
}

// A human checks a role AI's result: accept, or send back with a reason (operator only).
function reviewRow(item) {
  const row = el("div", "pmo-proposal");
  row.append(el("div", "pmo-title", item.title));
  row.append(el("div", "pmo-meta", `${t("web_pmo_agent", "Role AI")} ${item.agent} · ${item.task}`));
  if (item.excerpt) row.append(el("div", "pmo-meta pmo-agent-result", item.excerpt));
  if (canRun) {
    const note = el("input", "note-input");
    note.type = "text";
    note.placeholder = t("web_pmo_review_note", "Reason (required to send back)");
    note.setAttribute("aria-label", note.placeholder);
    row.append(note);
    const actions = el("div", "pmo-actions");
    for (const [decision, label, kind] of [
      ["accept", t("web_pmo_review_accept", "Accept"), "btn btn-approve"],
      ["reject", t("web_pmo_review_reject", "Send back"), "btn btn-reject"]]) {
      const button = el("button", kind, label);
      button.addEventListener("click", async () => {
        actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        try {
          await api("/api/pmo/agents/review", {
            method: "POST",
            body: JSON.stringify({ ref: item.task, dispatch: item.dispatch, decision, note: note.value }),
          });
          toast(t("web_pmo_reviewed", "Recorded."));
          await refreshPmo();
        } catch (error) {
          toast(error.message, "error");
          actions.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        }
      });
      actions.append(button);
    }
    row.append(actions);
  }
  return row;
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

  // Follow-up tasks the PMO Core proposed from an alert; not work until approved.
  // The Core's autonomous judgment: current diagnoses and recent decisions, read-only
  // (approving is done from the proposals above).
  const judgment = briefing && briefing.judgment;
  if (judgment) {
    const head = pmoSection(t("web_pmo_judgment", "Autonomous judgment"), judgment.diagnoses.length);
    if (judgment.paused) head.append(el("div", "card-note error", t("web_pmo_paused", "Paused")));
    if (judgment.tripped) head.append(el("div", "card-note error", t("web_pmo_tripped", "Breaker tripped")));
    for (const item of judgment.diagnoses) {
      const row = el("details", "pmo-task");
      const summary = el("summary");
      summary.append(el("span", "tag score", String(item.severity)));
      summary.append(el("span", "pmo-title", item.title));
      row.append(summary);
      const list = el("ul", "pmo-reasons");
      for (const line of item.evidence) list.append(el("li", null, line));
      row.append(list);
      head.append(row);
    }
    for (const item of (judgment.recent || []).slice(0, 5)) {
      const note = el("div", "pmo-agent", `${item.state || "-"}${item.auto ? " · auto" : ""} — ${item.title}`);
      note.dataset.status = item.state === "executed" ? "done" : (item.state === "failed" ? "failed" : "running");
      head.append(note);
    }
    if (judgment.diagnoses.length || (judgment.recent || []).length || judgment.paused) host.append(head);
  }

  const reviews = data.agent_review || [];
  if (reviews.length) {
    const block = pmoSection(t("web_pmo_review", "Role AI results awaiting review"), reviews.length);
    for (const item of reviews) block.append(reviewRow(item));
    host.append(block);
  }

  const filing = data.filing;
  if (filing && filing.pending.length) {
    const block = pmoSection(t("web_pmo_filing", "Waiting to be filed in the tracker"), filing.pending.length);
    for (const item of filing.pending) block.append(filingRow(item, filing.tracker, filing.can_file));
    host.append(block);
  }

  const generated = data.proposals || [];
  if (generated.length) {
    const block = pmoSection(t("web_pmo_generated", "Proposed tasks"), generated.length);
    for (const item of generated) block.append(generatedRow(item));
    host.append(block);
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
  const pace = (learned && learned.pace) || {};
  if (learned && (Object.keys(learned.member_factor).length
                  || Object.keys(learned.label_bonus).length
                  || Object.keys(learned.priority_delta || {}).length
                  || pace.team)) {
    const block = pmoSection(t("web_pmo_learning", "Learned adjustments"), null);
    for (const [who, factor] of Object.entries(learned.member_factor)) {
      block.append(el("div", "pmo-meta", `${who}: ×${factor}`));
    }
    for (const [label, bonus] of Object.entries(learned.label_bonus)) {
      block.append(el("div", "pmo-meta", `${label}: +${bonus}`));
    }
    for (const [level, delta] of Object.entries(learned.priority_delta || {})) {
      block.append(el("div", "pmo-meta", `${level}: ${delta > 0 ? "+" : ""}${delta}`));
    }
    if (pace.team) {
      // When the estimates have not proved accurate, say the pace is not used.
      const used = pace.reliable ? "" : ` · ${t("web_pmo_pace_unused", "not used")}`;
      block.append(el("div", "pmo-meta",
        `${t("web_pmo_pace", "Pace")}: ${pace.team} d/pt · ±${Math.round(pace.median_error * 100)}%${used}`));
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

let pmoData = null;

async function refreshPmo() {
  // A deployment with no PMO data answers 404; that is an unused feature,
  // not an error, so the heading is hidden rather than toasting each refresh.
  try {
    const filter = pmoProject ? `project=${encodeURIComponent(pmoProject)}` : "";
    const data = await api(filter ? `/api/pmo?${filter}` : "/api/pmo");
    const { items } = await api(`/api/pmo/decisions?limit=20${filter ? "&" + filter : ""}`)
      .catch(() => ({ items: [] }));
    $("h-pmo").hidden = false;
    pmoData = data;
    renderPmo(data, items);
    renderToday();
    renderReviews();
    renderJudgment();
  } catch (error) {
    $("h-pmo").hidden = true;
    $("pmo").replaceChildren();
  }
}

/* ---------- today ----------
 * Combines /api/pmo briefing and inbox counts in one view. No new APIs or calculations
 * (counting is done client-side; ledger decisions are already made by the server). Strings via textContent.
 *
 * One screen recomposed from the briefing /api/pmo already returns plus the inbox count; no new
 * API and no new decision logic.
 */
function kpi(label, value, tone) {
  const card = el("div", "kpi");
  if (tone) card.dataset.tone = tone;
  card.append(el("div", "kpi-value", String(value)), el("div", "kpi-label", label));
  return card;
}

function todaySection(title, count) {
  const wrap = el("section", "today-block");
  const head = el("h3", "today-h", title);
  if (count != null) head.append(el("span", "pmo-count", String(count)));
  wrap.append(head);
  return wrap;
}

function renderToday() {
  const host = document.getElementById("today");
  if (!host) return;
  host.replaceChildren();
  const briefing = pmoData && pmoData.briefing;
  if (!briefing) {
    host.append(empty(t("web_today_none", "No PMO data yet")));
    return;
  }
  const waiting = inboxState.data ? inboxState.data.total : 0;
  const overloaded = (briefing.overloaded_members || []).length;
  const level = el("span", "tag level", `${t("web_pmo_level", "Overall")}: ${briefing.overall_level}`);
  level.dataset.level = briefing.overall_level;
  const head = el("div", "today-head");
  head.append(level);
  if (pmoData.briefing_age_seconds != null) {
    head.append(el("span", "pmo-meta", humanAge(pmoData.briefing_age_seconds)));
  }
  host.append(head);

  const kpis = el("div", "kpis");
  kpis.append(
    kpi(t("web_today_active", "Active"), briefing.active_count),
    kpi(t("web_today_alerts", "Alerts"), (briefing.alerts || []).length,
        (briefing.alerts || []).length ? "warn" : ""),
    kpi(t("web_today_waiting", "Awaiting decision"), waiting, waiting ? "do" : ""),
    kpi(t("web_today_overloaded", "Overloaded"), overloaded, overloaded ? "warn" : ""),
  );
  host.append(kpis);

  if (waiting) {
    const go = el("button", "btn today-go", `${t("web_today_open_inbox", "Decide in inbox")} (${waiting})`);
    go.type = "button";
    go.addEventListener("click", () => showTab("inbox"));
    host.append(go);
  }

  const cols = el("div", "today-cols");
  const left = el("div"), right = el("div");

  const top = todaySection(t("web_today_top", "Top priorities"), null);
  for (const item of (briefing.top_priorities || []).slice(0, 5)) {
    const row = el("div", "today-task");
    row.append(el("span", "today-score", String(item.score)));
    const body = el("div", "today-task-body");
    body.append(el("div", "pmo-title", item.title));
    body.append(el("div", "pmo-meta",
      [item.project, item.assignee, item.due_date].filter(Boolean).join(" · ")));
    if (item.reasons && item.reasons.length) {
      body.append(el("div", "pmo-meta", item.reasons.slice(0, 3).join(" / ")));
    }
    row.append(body);
    top.append(row);
  }
  left.append(top);

  const projects = briefing.projects || [];
  if (projects.length) {
    const block = todaySection(t("web_today_projects", "Projects"), projects.length);
    for (const p of projects) {
      const row = el("div", "today-project");
      const tag = el("span", "tag level", p.level);
      tag.dataset.level = p.level;
      row.append(el("span", "pmo-title", p.project), tag,
        el("span", "pmo-meta", `${p.active_count} / ${p.alert_count}`));
      block.append(row);
    }
    left.append(block);
  }

  const loads = briefing.member_loads || [];
  if (loads.length) {
    const block = todaySection(t("web_today_load", "Member load"), null);
    for (const m of loads) {
      const row = el("div", "today-load");
      const ratio = m.capacity ? m.load / m.capacity : 0;
      const bar = el("div", "bar");
      const fill = el("div", "bar-fill");
      fill.style.width = `${Math.min(100, Math.round(ratio * 100))}%`;
      if (ratio > 1) fill.dataset.over = "";
      bar.append(fill);
      row.append(el("span", "today-name", m.member), bar,
        el("span", "pmo-meta", `${m.load}/${m.capacity}`));
      block.append(row);
    }
    right.append(block);
  }

  const j = briefing.judgment;
  if (j && j.enabled) {
    const block = todaySection(t("web_today_judgment", "Autonomous judgment"), null);
    const state = j.tripped ? t("web_today_tripped", "Circuit breaker tripped")
      : j.paused ? t("web_today_paused", "Paused") : t("web_today_running", "Running");
    const modes = Object.values(j.autonomy || {});
    const autos = modes.filter((m) => m === "auto").length;
    block.append(el("div", "pmo-meta", `${state} · auto ${autos}/${modes.length}`));
    for (const d of (j.diagnoses || []).slice(0, 3)) {
      block.append(el("div", "pmo-meta", d.title || d.summary || d.kind || ""));
    }
    right.append(block);
  }

  cols.append(left, right);
  host.append(cols);
}

/* ---------- members & learning ----------
 * /api/learning/members (read-only). Shows why the capacity was adjusted,
 * using outcomes (on-time rate, average delay, count). Members with too few
 * outcomes are not adjusted (factor ×1.00) — that is shown as-is.
 *
 * Why each capacity was adjusted, from the track record; members with too few samples are shown uncorrected.
 */
let membersState = { data: null };

async function refreshMembers() {
  try {
    membersState.data = await api("/api/learning/members");
  } catch (error) {
    membersState.data = null;                       // 403 (scoped viewer) or no ledger
  }
  renderMembers();
}

function memberCard(m) {
  const card = el("div", "member-card");
  const head = el("div", "member-head");
  head.append(el("span", "pmo-title", m.name));
  if (m.is_agent) head.append(el("span", "tag", "AI"));
  const factor = el("span", "tag", `×${m.factor.toFixed(2)}`);
  factor.dataset.dir = m.factor < 1 ? "down" : m.factor > 1 ? "up" : "flat";
  head.append(factor);
  card.append(head);
  if (m.capacity != null) {
    const row = el("div", "today-load");
    const bar = el("div", "bar");
    const fill = el("div", "bar-fill");
    const scale = Math.max(m.capacity, m.effective_capacity || 0, m.load, 1);
    fill.style.width = `${Math.min(100, (m.load / scale) * 100)}%`;
    if (m.load > (m.effective_capacity || m.capacity)) fill.dataset.over = "";
    const mark = el("span", "cap-mark");
    mark.style.left = `${((m.effective_capacity || m.capacity) / scale) * 100}%`;
    bar.append(fill, mark);
    row.append(bar, el("span", "pmo-meta", `${m.load} / ${m.effective_capacity} (${m.capacity})`));
    card.append(row);
  }
  const facts = [];
  if (m.on_time_rate != null) {
    facts.push(`${t("web_members_ontime", "On time")} ${Math.round(m.on_time_rate * 100)}%`);
    facts.push(`${t("web_members_late", "Avg delay")} ${m.avg_late_days} d`);
  }
  if (m.pace != null) facts.push(`${t("web_members_pace", "Pace")} ${m.pace} d/pt`);
  facts.push(`${t("web_members_samples", "Outcomes")} ${m.samples}`);
  card.append(el("div", "pmo-meta", facts.join(" · ")));
  if (m.samples < 5) card.append(el("div", "pmo-meta", t("web_members_few", "Too few outcomes to adjust")));
  return card;
}

function renderMembers() {
  const host = document.getElementById("members");
  if (!host) return;
  host.replaceChildren();
  const data = membersState.data;
  if (!data) {
    host.append(empty(t("web_today_none", "No PMO data yet")));
    return;
  }
  const grid = el("div", "members-grid");
  for (const m of data.members) grid.append(memberCard(m));
  host.append(grid);

  const side = todaySection(t("web_members_learned", "Learned adjustments"), null);
  const base = data.baseline_late_rate != null ? `${Math.round(data.baseline_late_rate * 100)}%` : "—";
  side.append(el("div", "pmo-meta",
    `${t("web_members_samples", "Outcomes")} ${data.samples} · ${t("web_members_baseline", "Overall delay rate")} ${base}`));
  for (const l of data.labels) {
    side.append(el("div", "pmo-meta", `${l.label}: +${l.bonus}${l.samples ? ` (${l.samples})` : ""}`));
  }
  for (const p of data.priorities) {
    side.append(el("div", "pmo-meta",
      `${p.priority}: ${p.delta > 0 ? "+" : ""}${p.delta}${p.samples ? ` (${p.samples})` : ""}`));
  }
  side.append(el("div", "pmo-meta", data.pace.reliable
    ? t("web_members_pace_ok", "Estimates reliable — used in ranking")
    : t("web_members_pace_no", "Estimates not reliable — not used in ranking")));
  host.append(side);
}

/* ---------- integrations ----------
 * /api/integrations (read-only). Shows adapter ping, writeback flag, latest collection result,
 * WBS file check, filing config, and member accounts. Name-based lookup queries external services
 * so it is not done here — use `aipmo members` instead.
 *
 * State of the integrations, read-only. Name lookups call the tracker and are not done here.
 */
let integState = { data: null };

async function refreshIntegrations() {
  try {
    integState.data = await api("/api/integrations");
  } catch (error) {
    integState.data = null;
  }
  renderIntegrations();
}

function renderProbe(report) {
  const card = el("div", "probe");
  card.dataset.ok = String(report.ok);
  card.append(el("div", "pmo-title", `${report.ok ? "✓" : "✗"} ${report.name}`));
  for (const step of report.steps) {
    const line = el("div", "probe-step");
    line.dataset.ok = String(step.ok);
    line.append(el("span", "probe-mark", step.ok ? "✓" : "✗"),
      el("span", "probe-id", t(`web_integ_step_${step.id}`, step.id)),
      el("span", "pmo-meta", `${step.ms} ms · ${step.detail}`));
    card.append(line);
    if (step.hint) card.append(el("div", "card-note error", t(`web_integ_hint_${step.hint}`, step.hint)));
  }
  return card;
}

function renderIntegrations() {
  const host = document.getElementById("integrations");
  if (!host) return;
  host.replaceChildren();
  const d = integState.data;
  if (!d) {
    host.append(empty(t("web_today_none", "No PMO data yet")));
    return;
  }
  const grid = el("div", "members-grid");
  for (const a of d.adapters) {
    const card = el("div", "member-card integ-card");
    card.dataset.healthy = String(a.healthy);
    const head = el("div", "member-head");
    head.append(el("span", "pmo-title", a.name),
      el("span", "tag", a.healthy ? t("web_integ_ok", "Connected") : t("web_integ_down", "Unreachable")));
    card.append(head);
    card.append(el("div", "pmo-meta", a.writeback
      ? t("web_integ_writeback", "Can write back assignee and file issues") : t("web_integ_readonly", "Read-only")));
    grid.append(card);
  }
  host.append(grid);

  if (canRun) {
    const run = el("button", "btn", t("web_integ_check", "Check connection"));
    run.type = "button";
    run.id = "integ-check";
    const results = el("div", "integ-results");
    results.id = "integ-results";
    results.setAttribute("aria-live", "polite");
    run.addEventListener("click", async () => {
      run.disabled = true;
      results.replaceChildren(el("div", "pmo-meta", t("web_integ_checking", "Checking…")));
      try {
        const { reports } = await api("/api/integrations/check", { method: "POST", body: "{}" });
        results.replaceChildren(...reports.map(renderProbe));
      } catch (error) {
        results.replaceChildren(el("div", "card-note error", error.message));
      } finally {
        run.disabled = false;
      }
    });
    host.append(run, results);
  }

  const col = todaySection(t("web_integ_collect", "Progress collection"), null);
  if (!d.collection) {
    col.append(el("div", "pmo-meta", t("web_integ_collect_none", "Collection is not configured or has not run yet")));
  } else {
    const c = d.collection;
    col.append(el("div", "pmo-meta", `${clock(c.at)} · ${c.refreshed} refreshed · ${c.failed} failed · ${c.completed} completed`));
    if (c.error) col.append(el("div", "card-note error", c.error));
    for (const s of c.sources || []) {
      const line = el("div", "wbs-msg", `${s.id}: ${s.error || `${s.items} items`}`);
      line.dataset.level = s.error ? "error" : "ok";
      col.append(line);
    }
  }
  host.append(col);

  const wbs = todaySection(t("web_integ_wbs", "WBS file check"), null);
  if (!d.wbs) {
    wbs.append(el("div", "pmo-meta", "—"));
  } else {
    wbs.append(el("div", "pmo-meta", `${clock(d.wbs.checked_at)} · ${d.wbs.file}`));
    wbs.append(d.wbs.error ? el("div", "card-note error", d.wbs.error)
      : el("div", "pmo-meta", `${t("web_integ_drift", "Drift")} ${d.wbs.found}`));
  }
  host.append(wbs);

  if (d.filing) {
    const f = todaySection(t("web_integ_filing", "Filing"), d.filing.pending);
    f.append(el("div", "pmo-meta",
      `${d.filing.tracker} · auto: ${d.filing.auto.length ? d.filing.auto.join(", ") : "—"} · ${d.filing.can_file ? t("web_integ_ok", "Connected") : t("web_integ_down", "Unreachable")}`));
    host.append(f);
  }

  const acc = todaySection(t("web_integ_accounts", "Member accounts"), d.accounts.length);
  for (const m of d.accounts) {
    const pairs = Object.entries(m.accounts).map(([k, v]) => `${k}: ${v}`);
    acc.append(el("div", "pmo-meta", `${m.member}${m.is_agent ? " (AI)" : ""} — ${pairs.length ? pairs.join(", ") : "—"}`));
  }
  acc.append(el("div", "pmo-meta", d.lookup_assignees
    ? t("web_integ_lookup_on", "Members without an explicit account are looked up by name (check with aipmo members)")
    : t("web_integ_lookup_off", "Name-based lookup is disabled")));
  host.append(acc);
}

/* ---------- reviews ----------
 * Review history for role-AI results (/api/agents/reviews, read-only) and currently pending.
 * Accept/reject is done from the inbox (no new write entry points).
 *
 * Human reviews of role-AI results (read-only history) and what still waits. Deciding happens in the inbox.
 */
let reviewsState = { data: null };

async function refreshReviews() {
  try {
    reviewsState.data = await api("/api/agents/reviews?limit=50");
  } catch (error) {
    reviewsState.data = null;
  }
  renderReviews();
}

function renderReviews() {
  const host = document.getElementById("reviews");
  if (!host) return;
  host.replaceChildren();
  const briefing = pmoData && pmoData.briefing;
  const data = reviewsState.data;
  if (!briefing || !data) {
    host.append(empty(t("web_today_none", "No PMO data yet")));
    return;
  }
  const cards = el("div", "kpis");
  for (const [agent, c] of Object.entries(data.tally)) {
    const total = c.accepted + c.rejected;
    cards.append(kpi(agent, total ? `${Math.round((c.accepted / total) * 100)}%` : "—"));
    cards.lastChild.append(el("div", "pmo-meta",
      `${t("web_review_accepted", "Accepted")} ${c.accepted} / ${t("web_review_rejected", "Rejected")} ${c.rejected}`));
  }
  if (cards.children.length) host.append(cards);

  const pending = (briefing.agent_review && briefing.agent_review.pending) || [];
  const wait = todaySection(t("web_review_pending", "Awaiting review"), pending.length);
  for (const p of pending) {
    const row = el("div", "today-task");
    const body = el("div", "today-task-body");
    body.append(el("div", "pmo-title", p.title), el("div", "pmo-meta", [p.project, p.agent].filter(Boolean).join(" · ")));
    if (p.excerpt) body.append(el("div", "review-excerpt", p.excerpt));
    row.append(body);
    wait.append(row);
  }
  if (pending.length) {
    const go = el("button", "btn today-go", t("web_today_open_inbox", "Decide in inbox"));
    go.type = "button";
    go.addEventListener("click", () => { inboxState.filter = "review"; showTab("inbox"); renderInbox(); });
    wait.append(go);
  } else {
    wait.append(el("div", "pmo-meta", "—"));
  }
  host.append(wait);

  const history = todaySection(t("web_review_history", "Review history"), data.total);
  for (const e of data.items) {
    const row = el("div", "review-row");
    row.dataset.decision = e.decision;
    const label = e.decision === "accepted" ? t("web_review_accepted", "Accepted") : t("web_review_rejected", "Rejected");
    row.append(el("span", "tag", label), el("span", "pmo-title", e.title || e.task),
      el("div", "pmo-meta", [e.project, e.agent, e.by, clock(e.at)].filter(Boolean).join(" · ")));
    if (e.note) row.append(el("div", "pmo-meta", e.note));
    history.append(row);
  }
  if (!data.items.length) history.append(el("div", "pmo-meta", "—"));
  host.append(history);
}

/* ---------- judgment ----------
 * Status, autonomy levels, diagnoses, and recent judgments (from briefing).
 * Operators can pause / resume / reset the circuit breaker (POST /api/judgment/*).
 * Autonomy level changes are not possible from the UI — they stay as configured.
 *
 * State, autonomy, diagnoses and recent actions from the briefing. An operator may pause, resume or reset
 * the breaker; autonomy levels stay in the config file.
 */
let judgmentCtl = {};
let judgmentAuto = { config: {}, override: {}, can_raise: false };

async function refreshJudgmentControl() {
  try {
    const res = await api("/api/judgment/control");
    judgmentCtl = res.control || {};
    judgmentAuto = res.autonomy || judgmentAuto;
  } catch (error) {
    judgmentCtl = {};
  }
  renderJudgment();
}

async function judgmentControl(action, button) {
  button.disabled = true;
  try {
    const result = await api(`/api/judgment/${action}`, { method: "POST", body: "{}" });
    judgmentCtl = result.control || {};
    toast(t("web_judgment_next_cycle", "Request sent. The scheduler will apply it next cycle."));
    await refreshPmo();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderJudgment() {
  const host = document.getElementById("judgment");
  if (!host) return;
  host.replaceChildren();
  const j = pmoData && pmoData.briefing && pmoData.briefing.judgment;
  if (!j || !j.enabled) {
    host.append(empty(t("web_judgment_off", "Autonomous judgment is not configured")));
    return;
  }
  // A pause is a request the resident reads next cycle; show the requested state so a press is visible.
  const wantPaused = judgmentCtl.paused === undefined ? j.paused : Boolean(judgmentCtl.paused);
  const head = el("div", "today-head");
  const state = j.tripped ? t("web_today_tripped", "Circuit breaker tripped")
    : j.paused ? t("web_today_paused", "Paused") : t("web_today_running", "Running");
  const chip = el("span", "tag level", state);
  chip.dataset.level = j.tripped ? "critical" : j.paused ? "high" : "low";
  head.append(chip, el("span", "pmo-meta", `${t("web_today_waiting", "Awaiting decision")} ${j.pending}`));
  if (wantPaused !== j.paused) {
    const note = el("span", "tag", wantPaused ? t("web_judgment_req_pause", "Pause requested (applied next cycle)")
      : t("web_judgment_req_resume", "Resume requested (applied next cycle)"));
    note.id = "judgment-requested";
    head.append(note);
  }
  host.append(head);

  if (canRun) {
    const bar = el("div", "judgment-controls");
    for (const [action, label] of [["pause", t("web_judgment_pause", "Pause")],
                                   ["resume", t("web_judgment_resume", "Resume")],
                                   ["reset", t("web_judgment_reset", "Reset circuit breaker")]]) {
      const hidden = (action === "pause" && wantPaused) || (action === "resume" && !wantPaused)
        || (action === "reset" && !j.tripped);
      if (hidden) continue;
      const button = el("button", "btn", label);
      button.type = "button";
      button.dataset.action = action;
      button.addEventListener("click", () => judgmentControl(action, button));
      bar.append(button);
    }
    host.append(bar);
  }

  const levels = todaySection(t("web_judgment_autonomy", "Autonomy levels"), null);
  const rank = { off: 0, propose: 1, auto: 2 };
  for (const [kind, shown] of Object.entries(j.autonomy || {})) {
    const base = (judgmentAuto.config && judgmentAuto.config[kind]) || (j.autonomy_config || {})[kind] || shown;
    const row = el("div", "today-project");
    row.append(el("span", "pmo-title", kind));
    const changed = judgmentAuto.override && judgmentAuto.override[kind];
    if (changed) {
      const mark = el("span", "tag", t("web_auto_changed", "Changed"));
      mark.title = t("web_auto_config", "Config file value: {x}").replace("{x}", base);
      row.append(mark);
    }
    if (canRun) {
      const select = document.createElement("select");
      select.className = "auto-select";
      select.dataset.remedy = kind;
      select.setAttribute("aria-label", kind);
      const requested = (judgmentAuto.override && judgmentAuto.override[kind]) || base;
      for (const level of ["off", "propose", "auto"]) {
        const option = el("option", null, level);
        option.value = level;
        option.disabled = rank[level] > rank[base] && !(judgmentAuto.can_raise || j.ui_can_raise);
        select.append(option);
      }
      select.value = requested;
      select.addEventListener("change", async () => {
        select.disabled = true;
        try {
          await api("/api/judgment/autonomy", { method: "POST", body: JSON.stringify({ remedy: kind, level: select.value }) });
          toast(t("web_judgment_next_cycle", "Request sent. The scheduler will apply it next cycle."));
          await refreshJudgmentControl();
        } catch (error) {
          toast(error.message, "error");
          select.value = requested;
        } finally {
          select.disabled = false;
        }
      });
      row.append(select);
    } else {
      const tag = el("span", "tag", shown);
      tag.dataset.mode = shown;
      row.append(tag);
    }
    levels.append(row);
  }
  levels.append(el("div", "pmo-meta", t("web_auto_hint", "You can lower or pause autonomy levels from this screen.")));
  host.append(levels);

  const diagnoses = todaySection(t("web_judgment_diagnoses", "Current diagnoses"), (j.diagnoses || []).length);
  for (const d of j.diagnoses || []) {
    const row = el("div", "today-task");
    row.append(el("span", "today-score", String(d.severity)));
    const body = el("div", "today-task-body");
    body.append(el("div", "pmo-title", d.title));
    for (const line of (d.evidence || []).slice(0, 3)) body.append(el("div", "pmo-meta", line));
    row.append(body);
    diagnoses.append(row);
  }
  host.append(diagnoses);

  const recent = todaySection(t("web_judgment_recent", "Recent judgments"), (j.recent || []).length);
  for (const r of j.recent || []) {
    const row = el("div", "review-row");
    row.append(el("span", "tag", r.state), el("span", "pmo-title", r.title),
      el("div", "pmo-meta", `${clock(r.at)}${r.result ? ` · ${r.result}` : ""}`));
    recent.append(row);
  }
  host.append(recent);
}

/* ---------- tasks ----------
 * List and detail from /api/tasks (read-only). Score shown as per-component bars (parts).
 * Assignment confirmation, filing, etc. are in the inbox and PMO Core — no writes from this screen.
 *
 * The task table (read-only /api/tasks) and a detail pane (/api/tasks/{id}); the score is drawn from its
 * per-item parts. No write happens here — deciding stays in the inbox.
 */
const PART_LABEL = {
  priority: "priority", due: "due", pace: "pace", blocked: "blocked",
  corroboration: "templates", unassigned: "unassigned", label: "label", other: "other",
};
let tasksState = { data: null, selected: null, detail: null, query: {
  project: "", assignee: "", q: "", sort: "score", limit: 50 }, ready: false };

function tasksUrl() {
  const q = tasksState.query;
  const params = new URLSearchParams({ sort: q.sort, limit: String(q.limit) });
  for (const key of ["project", "assignee", "q"]) if (q[key]) params.set(key, q[key]);
  return `/api/tasks?${params}`;
}

const signed = (n) => (n >= 0 ? `+${n}` : String(n));

function scoreBar(parts, score) {
  const bar = el("div", "score-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", parts.map((p) => `${PART_LABEL[p.kind] || p.kind} ${signed(p.points)}`).join(", "));
  const total = Math.max(score, 1);
  for (const part of parts) {
    if (part.points <= 0) continue;
    const seg = el("span", "score-seg");
    seg.dataset.kind = part.kind;
    seg.style.width = `${(part.points / total) * 100}%`;
    seg.title = part.text;
    bar.append(seg);
  }
  return bar;
}

function tasksFilters(data) {
  const wrap = el("div", "tasks-filters");
  const search = document.createElement("input");
  search.type = "search";
  search.id = "tasks-q";
  search.placeholder = t("web_tasks_search", "Search");
  search.setAttribute("aria-label", t("web_tasks_search", "Search"));
  search.value = tasksState.query.q;
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { tasksState.query.q = search.value.trim(); refreshTasks(true); }, 250);
  });
  const select = (id, label, current, options, onChange) => {
    const node = document.createElement("select");
    node.id = id;
    node.setAttribute("aria-label", label);
    for (const [value, text] of options) {
      const option = el("option", null, text);
      option.value = value;
      node.append(option);
    }
    node.value = current;
    node.addEventListener("change", () => onChange(node.value));
    return node;
  };
  const all = t("web_tasks_all", "All");
  wrap.append(
    search,
    select("tasks-project", t("web_today_projects", "Projects"), tasksState.query.project,
      [["", all], ...data.projects.map((p) => [p, p])],
      (v) => { tasksState.query.project = v; refreshTasks(true); }),
    select("tasks-assignee", t("web_tasks_assignee", "Assignee"), tasksState.query.assignee,
      [["", all], ["-", t("web_tasks_unassigned", "Unassigned")], ...data.assignees.map((p) => [p, p])],
      (v) => { tasksState.query.assignee = v; refreshTasks(true); }),
    select("tasks-sort", t("web_tasks_sort", "Sort"), tasksState.query.sort,
      [["score", t("web_tasks_sort_score", "Score")], ["due", t("web_tasks_sort_due", "Due date")],
       ["priority", t("web_tasks_sort_priority", "Priority")]],
      (v) => { tasksState.query.sort = v; refreshTasks(true); }),
  );
  return wrap;
}

function tasksRow(item) {
  const row = el("button", "task-row");
  row.type = "button";
  row.dataset.id = item.id;
  if (tasksState.selected === item.id) row.setAttribute("aria-current", "true");
  const score = el("span", "task-score", String(item.score));
  const body = el("span", "task-body");
  body.append(el("span", "pmo-title", item.title),
    el("span", "pmo-meta", [item.project, item.key, item.assignee || t("web_tasks_unassigned", "Unassigned"),
      item.due_date].filter(Boolean).join(" · ")),
    scoreBar(item.parts, item.score));
  row.append(score, body);
  row.addEventListener("click", () => selectTask(item.id));
  return row;
}

async function selectTask(id) {
  tasksState.selected = id;
  for (const node of document.querySelectorAll(".task-row")) {
    if (node.dataset.id === id) node.setAttribute("aria-current", "true");
    else node.removeAttribute("aria-current");
  }
  const pane = document.getElementById("task-detail");
  if (!pane) return;
  try {
    tasksState.detail = await api(`/api/tasks/${encodeURIComponent(id)}`);
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  renderTaskDetail(pane);
  if (window.matchMedia("(max-width: 899.98px)").matches) pane.scrollIntoView({ block: "start" });
}

function renderTaskDetail(pane) {
  pane.replaceChildren();
  const d = tasksState.detail;
  if (!d) {
    pane.append(el("p", "lead", t("web_tasks_pick", "Select a task to see its score breakdown")));
    return;
  }
  pane.append(el("h3", "wbs-dh", d.title));
  pane.append(el("div", "pmo-meta", [d.project, d.key || d.id, d.status, d.assignee, d.due_date,
    d.priority].filter(Boolean).join(" · ")));
  const parts = el("div", "task-parts");
  parts.append(el("h4", "wbs-eh", `${t("web_tasks_parts", "Score breakdown")} — ${d.score}`));
  for (const part of d.parts) {
    const line = el("div", "task-part");
    const seg = el("span", "score-seg");
    seg.dataset.kind = part.kind;
    line.append(seg, el("span", "task-part-pts", signed(part.points)), el("span", "pmo-meta", part.text));
    parts.append(line);
  }
  pane.append(parts);
  if (d.alerts.length) {
    const block = el("div");
    block.append(el("h4", "wbs-eh", t("web_today_alerts", "Alerts")));
    for (const a of d.alerts) {
      const line = el("div", "wbs-msg", `${a.title} — ${a.message}`);
      line.dataset.level = a.severity === "critical" || a.severity === "high" ? "error" : "warn";
      block.append(line);
    }
    pane.append(block);
  }
  if (d.dispatches.length) {
    const block = el("div");
    block.append(el("h4", "wbs-eh", t("web_tasks_runs", "Role AI runs")));
    for (const r of d.dispatches.slice(-5)) {
      block.append(el("div", "pmo-meta", `${r.agent || ""} · ${r.status} · ${clock(r.at)}`));
    }
    pane.append(block);
  }
  if (d.history.length) {
    const block = el("div");
    block.append(el("h4", "wbs-eh", t("web_tasks_history", "Decision history")));
    for (const h of d.history.slice(0, 8)) {
      block.append(el("div", "pmo-meta", `${clock(h.at)} ${h.kind}`));
    }
    pane.append(block);
  }
}

function renderTasks() {
  const host = $("tasks");
  const data = tasksState.data;
  const keepSearch = document.activeElement && document.activeElement.id === "tasks-q";
  host.replaceChildren();
  if (!data) {
    host.append(empty(t("web_today_none", "No PMO data yet")));
    return;
  }
  host.append(tasksFilters(data));
  const grid = el("div", "tasks-grid");
  const list = el("div", "tasks-list");
  if (!data.items.length) list.append(empty("—"));
  for (const item of data.items) list.append(tasksRow(item));
  if (data.total > data.items.length) {
    const more = el("button", "btn tasks-more",
      `${t("web_tasks_more", "Show more")} (${data.items.length}/${data.total})`);
    more.type = "button";
    more.addEventListener("click", () => { tasksState.query.limit += 50; refreshTasks(false); });
    list.append(more);
  }
  const pane = el("aside", "wbs-detail");
  pane.id = "task-detail";
  renderTaskDetail(pane);
  grid.append(list, pane);
  host.append(grid);
  if (keepSearch) {
    const box = document.getElementById("tasks-q");
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  }
}

async function refreshTasks(reset) {
  if (reset) tasksState.query.limit = 50;
  try {
    tasksState.data = await api(tasksUrl());
    tasksState.ready = true;
  } catch (error) {
    tasksState.data = null;
  }
  renderTasks();
}

/* ---------- WBS ----------
 * Shows the /api/wbs tree as a table and timeline bars (read-only). No writes from this screen
 * (changes are proposed via the replan section below and approved by a human).
 * Bar end = completion date or deadline. Bar start = latest end among dependencies or left axis.
 * Tasks without dates have no bar. Strings are set via textContent.
 *
 * The WBS tree as a table with schedule bars, from the read-only /api/wbs. Bars come from dates alone:
 * end = done_on else due; start = the latest end among dependencies, else the axis start. No dates, no bar.
 */
let wbsState = { data: null, selected: null, collapsed: new Set(), nodes: new Map() };
const DAY = 86400000;
const dayOf = (iso) => (iso ? Date.parse(`${iso}T00:00:00Z`) : null);

function wbsStatusLabel(status) {
  return { done: "done", in_progress: "doing", blocked: "blocked", todo: "todo" }[status] || status;
}

function indexWbs(nodes, depth, out) {
  for (const node of nodes) {
    node._depth = depth;
    out.set(node.id, node);
    if (node.children) indexWbs(node.children, depth + 1, out);
  }
  return out;
}

function wbsEnd(node) {
  const end = dayOf(node.done_on);
  return end != null ? end : dayOf(node.due);
}

function wbsAxis(data) {
  const points = [Date.now()];
  for (const node of wbsState.nodes.values()) {
    for (const iso of [node.due, node.done_on]) if (iso) points.push(dayOf(iso));
  }
  if (data.wbs.deadline) points.push(dayOf(data.wbs.deadline));
  const min = Math.min(...points) - 3 * DAY, max = Math.max(...points) + 3 * DAY;
  return { min, max, span: Math.max(max - min, DAY) };
}

function wbsBar(node, axis) {
  const cell = el("div", "wbs-barcell");
  const today = el("span", "wbs-today");
  today.style.left = `${((Date.now() - axis.min) / axis.span) * 100}%`;
  cell.append(today);
  const end = wbsEnd(node);
  if (end == null) return cell;
  let start = axis.min + 3 * DAY;
  for (const dep of node.depends_on || []) {
    const d = wbsState.nodes.get(dep);
    const e = d ? wbsEnd(d) : null;
    if (e != null && e > start) start = e;
  }
  start = Math.min(start, end - DAY);
  const bar = el("span", "wbs-bar");
  bar.dataset.status = node.status;
  if ((node.flags || []).includes("overdue")) bar.dataset.late = "";
  bar.style.left = `${((start - axis.min) / axis.span) * 100}%`;
  bar.style.width = `${Math.max(1.5, ((end - start) / axis.span) * 100)}%`;
  cell.append(bar);
  return cell;
}

/* Dependency arrows: from the right edge of a prerequisite bar to the left edge of the dependent bar.
 * SVG overlaid on the table (read-only). Only links expanded rows. No arrow if a task has no bar.
 * Dependency arrows from a dependency's bar end to the dependent's bar start, as an SVG overlay. */
function drawWbsArrows(table) {
  table.querySelector("svg.wbs-arrows")?.remove();
  const base = table.getBoundingClientRect();
  const bars = new Map();
  for (const row of table.querySelectorAll(".wbs-row")) {
    const bar = row.querySelector(".wbs-bar");
    if (bar) bars.set(row.dataset.id, bar.getBoundingClientRect());
  }
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "wbs-arrows");
  svg.setAttribute("width", String(table.scrollWidth));
  svg.setAttribute("height", String(table.scrollHeight));
  svg.setAttribute("aria-hidden", "true");
  const defs = document.createElementNS(NS, "defs");
  const marker = document.createElementNS(NS, "marker");
  for (const [k, v] of Object.entries({ id: "wbs-arrowhead", viewBox: "0 0 8 8", refX: "7", refY: "4",
                                        markerWidth: "6", markerHeight: "6", orient: "auto" })) {
    marker.setAttribute(k, v);
  }
  const head = document.createElementNS(NS, "path");
  head.setAttribute("d", "M0 0 L8 4 L0 8 z");
  head.setAttribute("fill", "currentColor");
  marker.append(head);
  defs.append(marker);
  svg.append(defs);
  let drawn = 0;
  for (const node of wbsState.nodes.values()) {
    const to = bars.get(node.id);
    if (!to) continue;
    for (const dep of node.depends_on || []) {
      const from = bars.get(dep);
      if (!from) continue;
      const x1 = from.right - base.left + table.scrollLeft, y1 = from.top + from.height / 2 - base.top;
      const x2 = to.left - base.left + table.scrollLeft, y2 = to.top + to.height / 2 - base.top;
      const mid = Math.max(x1 + 6, Math.min(x2 - 6, (x1 + x2) / 2));
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", `M${x1} ${y1} H${mid} V${y2} H${x2 - 1}`);
      path.setAttribute("class", "wbs-arrow");
      path.setAttribute("marker-end", "url(#wbs-arrowhead)");
      if (node.critical && wbsState.nodes.get(dep)?.critical) path.dataset.critical = "";
      svg.append(path);
      drawn += 1;
    }
  }
  svg.dataset.count = String(drawn);
  table.append(svg);
}

function wbsRows(nodes, axis, host) {
  for (const node of nodes) {
    const row = el("div", "wbs-row");
    row.dataset.id = node.id;
    row.dataset.leaf = String(node.leaf);
    if (wbsState.selected === node.id) row.setAttribute("aria-current", "true");
    const name = el("div", "wbs-name");
    name.style.paddingLeft = `${node._depth * 16}px`;
    if (!node.leaf) {
      const caret = el("button", "wbs-caret", wbsState.collapsed.has(node.id) ? "▸" : "▾");
      caret.type = "button";
      caret.setAttribute("aria-label", node.name);
      caret.setAttribute("aria-expanded", String(!wbsState.collapsed.has(node.id)));
      caret.addEventListener("click", (event) => {
        event.stopPropagation();
        if (wbsState.collapsed.has(node.id)) wbsState.collapsed.delete(node.id);
        else wbsState.collapsed.add(node.id);
        renderWbs();
      });
      name.append(caret);
    }
    name.append(el("span", "wbs-id", node.id), el("span", "wbs-title", node.name));
    const flagged = (node.flags || []).filter((f) => f !== "unestimated");
    if (flagged.length) {
      const mark = el("span", "wbs-flag", "!");
      mark.title = flagged.join(", ");
      name.append(mark);
    }
    const status = el("span", "tag wbs-status", wbsStatusLabel(node.status));
    status.dataset.status = node.status;
    const extra = node.leaf ? (node.owner || "") : `${node.percent}%`;
    row.append(name, status, el("div", "wbs-owner", extra), el("div", "wbs-effort",
      node.effort != null ? String(node.effort) : ""), wbsBar(node, axis));
    row.addEventListener("click", () => {
      wbsState.selected = node.id;
      renderWbs();
    });
    host.append(row);
    if (!node.leaf && !wbsState.collapsed.has(node.id)) wbsRows(node.children, axis, host);
  }
}

const EDIT_FIELDS = [
  ["status", "select", ["todo", "in_progress", "blocked", "done"]],
  ["effort", "number"], ["due", "date"], ["owner", "text"],
  ["priority", "select", ["", "Highest", "High", "Medium", "Low", "Lowest"]],
];

function wbsEditForm(node) {
  const form = el("form", "wbs-form");
  const inputs = {};
  for (const [name, kind, options] of EDIT_FIELDS) {
    const label = el("label", "wbs-field", name);
    const input = kind === "select" ? document.createElement("select") : document.createElement("input");
    if (kind === "select") {
      for (const o of options) { const opt = el("option", null, o || "—"); opt.value = o; input.append(opt); }
    } else {
      input.type = kind;
      if (kind === "number") { input.min = "0"; input.step = "any"; }
    }
    const current = node[name] == null ? "" : String(node[name]);
    input.value = current;
    input.dataset.original = current;
    inputs[name] = input;
    label.append(input);
    form.append(label);
  }
  const result = el("div", "wbs-edit-result");
  const changes = () => {
    const out = [];
    for (const [name, kind] of EDIT_FIELDS) {
      const input = inputs[name];
      if (input.value === input.dataset.original || input.value === "") continue;
      out.push({ op: "set", node: node.id, field: name, value: kind === "number" ? Number(input.value) : input.value });
    }
    return out;
  };
  const send = async (apply) => {
    const list = changes();
    result.replaceChildren();
    if (!list.length) { result.append(el("div", "pmo-meta", t("web_wbs_nochange", "No changes"))); return; }
    try {
      const r = await api("/api/wbs/edit", { method: "POST", body: JSON.stringify({ changes: list, apply }) });
      if (apply) {
        toast(t("web_wbs_applied", "Applied to the WBS file"));
        await refreshWbs();
        return;
      }
      for (const line of r.report) result.append(el("div", "pmo-meta", line));
      const pre = document.createElement("pre");
      pre.className = "preview-diff";
      pre.textContent = r.diff || "";
      result.append(pre);
      apply_.hidden = !r.changed;
    } catch (error) {
      result.append(el("div", "card-note error", error.message));
      apply_.hidden = true;
    }
  };
  const preview = el("button", "btn", t("web_wbs_preview_btn", "Preview diff"));
  preview.type = "button";
  preview.addEventListener("click", () => send(false));
  const apply_ = el("button", "btn btn-approve", t("web_wbs_apply", "Apply to WBS file"));
  apply_.type = "button";
  apply_.hidden = true;
  apply_.addEventListener("click", () => send(true));
  const cancel = el("button", "btn", t("web_wbs_cancel", "Cancel"));
  cancel.type = "button";
  cancel.addEventListener("click", () => { form.remove(); });
  form.addEventListener("input", () => { apply_.hidden = true; result.replaceChildren(); });
  form.append(el("div", "pmo-meta", t("web_wbs_edit_hint", "Preview the diff before applying.")),
    el("div", "wbs-form-actions"), result);
  form.querySelector(".wbs-form-actions").append(preview, apply_, cancel);
  return form;
}

function wbsDetail(host) {
  host.replaceChildren();
  const node = wbsState.nodes.get(wbsState.selected);
  if (!node) {
    host.append(el("p", "lead", t("web_wbs_pick", "Select a task to see its evidence and dependencies")));
    return;
  }
  const messages = (wbsState.data.problems || []).filter((p) => p.node === node.id);
  host.append(el("h3", "wbs-dh", `${node.id} ${node.name}`));
  const facts = [node.owner, node.priority, node.due && `${t("web_wbs_deadline", "Deadline")} ${node.due}`,
    node.done_on && `done ${node.done_on}`, node.effort != null && `${node.effort} pt`].filter(Boolean);
  if (facts.length) host.append(el("div", "pmo-meta", facts.join(" · ")));
  if (node.depends_on && node.depends_on.length) {
    host.append(el("div", "pmo-meta", `${t("web_wbs_depends", "Depends on")}: ${node.depends_on.join(", ")}`));
  }
  for (const p of messages) {
    const note = el("div", "wbs-msg", p.message);
    note.dataset.level = p.level;
    host.append(note);
  }
  if (node.leaf) {
    const block = el("div", "wbs-evidence");
    block.append(el("h4", "wbs-eh", t("web_wbs_evidence", "Evidence")));
    if (!node.evidence.length) block.append(el("div", "pmo-meta", "—"));
    for (const ev of node.evidence) {
      const line = el("div", "wbs-ev");
      line.dataset.ok = String(ev.ok);
      line.append(el("span", "wbs-evmark", ev.ok ? "✓" : "✗"), el("code", null, ev.spec));
      if (!ev.ok && ev.why) line.append(el("div", "pmo-meta", ev.why));
      block.append(line);
    }
    host.append(block);
  }
  if (node.notes) host.append(el("p", "pmo-meta", node.notes));
  if (node.leaf && wbsState.data.can_edit) {
    const edit = el("button", "btn wbs-edit-btn", t("web_wbs_edit", "Edit"));
    edit.type = "button";
    edit.addEventListener("click", () => { edit.replaceWith(wbsEditForm(node)); });
    host.append(edit);
  }
}

/* ---------- WBS download / import ---------- */

function wbsDownload(path, filename) {
  const a = document.createElement("a");
  a.href = path;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function importWbs() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  input.style.display = "none";
  document.body.appendChild(input);
  input.onchange = async () => {
    const file = input.files && input.files[0];
    input.remove();
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await fetch("/api/wbs/import", {
        method: "POST",
        credentials: "include",
        body: form,
      }).then(async (r) => {
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          throw new Error(body.detail?.message || body.detail || r.statusText);
        }
        return r.json();
      });
      const warn = result.warnings > 0
        ? ` (warning ${result.warnings})`
        : "";
      toast(`${t("web_wbs_import_ok", "Import successful")} — ${result.nodes} nodes${warn}`);
      await refreshWbs();
    } catch (err) {
      toast(String(err.message || err), "error");
    }
  };
  input.click();
}

function renderWbs() {
  const host = $("wbs-screen");
  const data = wbsState.data;
  host.replaceChildren();
  if (!data) return;
  const summary = data.summary;
  const head = el("div", "wbs-head");
  head.append(el("h3", "wbs-name-h", data.wbs.name));

  const meter = el("div", "wbs-meter");
  const bar = el("div", "bar");
  const fill = el("div", "bar-fill");
  fill.style.width = `${summary.percent_by_effort}%`;
  bar.append(fill);
  meter.append(bar, el("span", "wbs-pct", `${summary.percent_by_effort}%`));
  head.append(el("div", "pmo-meta", t("web_wbs_progress", "Progress (estimate weight)")), meter);
  const facts = [
    `${summary.done}/${summary.leaves} done`, `${summary.in_progress} doing`, `${summary.blocked} blocked`,
    data.wbs.deadline && `${t("web_wbs_deadline", "Deadline")} ${data.wbs.deadline}`,
    data.velocity.per_day != null && `${t("web_wbs_velocity", "Velocity")} ${data.velocity.per_day}/d`,
  ].filter(Boolean);
  head.append(el("div", "pmo-meta", facts.join(" · ")));
  if (data.forecast) {
    const f = data.forecast;
    const drift = f.drift_days != null ? `drift ${f.drift_days > 0 ? "+" : ""}${f.drift_days} d` : "";
    head.append(el("div", "pmo-meta", `${f.projected_finish || f.finish_date || ""} ${drift}`.trim()));
  } else if (data.error_count) {
    head.append(el("div", "card-note error",
      t("web_wbs_forecast_off", "Forecast unavailable (fix errors first)")));
  }
  host.append(head);

  const pageBar = el("div", "wbs-page-bar");
  const btnTemplate = el("button", "btn wbs-action-btn", t("web_wbs_template", "Template"));
  btnTemplate.type = "button";
  btnTemplate.title = t("web_wbs_template_tip", "Download blank Excel template for import");
  btnTemplate.onclick = () => wbsDownload("/api/wbs/template", "wbs-template.xlsx");
  pageBar.append(btnTemplate);
  const btnExport = el("button", "btn wbs-action-btn", t("web_wbs_export", "Export"));
  btnExport.type = "button";
  btnExport.title = t("web_wbs_export_tip", "Download current WBS as Excel");
  btnExport.onclick = () => wbsDownload("/api/wbs/export", "wbs.xlsx");
  pageBar.append(btnExport);
  if (data.can_edit) {
    const btnImport = el("button", "btn wbs-action-btn", t("web_wbs_import", "Import"));
    btnImport.type = "button";
    btnImport.title = t("web_wbs_import_tip", "Load WBS from an Excel file (replaces the current WBS)");
    btnImport.onclick = importWbs;
    pageBar.append(btnImport);
  }
  host.append(pageBar);

  const grid = el("div", "wbs-grid");
  const table = el("div", "wbs-table");
  table.setAttribute("role", "list");
  wbsRows(data.tree, wbsAxis(data), table);
  const side = el("aside", "wbs-detail");
  side.id = "wbs-detail";
  wbsDetail(side);
  grid.append(table, side);
  host.append(grid);
  requestAnimationFrame(() => drawWbsArrows(table));
  if (typeof ResizeObserver !== "undefined") {
    let last = 0;
    new ResizeObserver(() => {
      const w = table.clientWidth;
      if (w !== last) { last = w; drawWbsArrows(table); }
    }).observe(table);
  }

  const lower = el("div", "wbs-lower");
  if (data.ready && data.ready.length) {
    const block = todaySection(t("web_wbs_ready", "Ready to start"), data.ready.length);
    for (const r of data.ready.slice(0, 6)) {
      block.append(el("div", "pmo-meta", `${r.id} ${r.name}${r.effort != null ? ` · ${r.effort} pt` : ""}`));
    }
    lower.append(block);
  }
  if (data.problems && data.problems.length) {
    const block = todaySection(t("web_wbs_problems", "Drift & warnings"), data.problems.length);
    for (const p of data.problems) {
      const line = el("div", "wbs-msg", `${p.node || ""} ${p.message}`.trim());
      line.dataset.level = p.level;
      block.append(line);
    }
    lower.append(block);
  }
  host.append(lower);
}

async function refreshWbs() {
  try {
    const data = await api("/api/wbs");
    wbsState.nodes = indexWbs(data.tree, 0, new Map());
    wbsState.data = data;
    if (!wbsState.nodes.has(wbsState.selected)) wbsState.selected = null;
    renderWbs();
    return true;
  } catch (error) {                       // 404 (not configured) or 403 (no permission) means unused feature
    wbsState.data = null;
    $("wbs-screen").replaceChildren();
    return false;
  }
}

/* ---------- inbox ----------
 * Aggregates all items awaiting a human decision (judgments, proposals, assignments,
 * reviews, filing, replan) across types. List is from /api/inbox (read-only).
 * Actions call the existing APIs each item points to — no new write entry points.
 * Strings come from external sources so they are set via textContent.
 *
 * The inbox gathers everything waiting for a human decision. The list comes from /api/inbox (read-only);
 * deciding calls the existing endpoint each item's `actions` names, so no new write path exists.
 */
const KIND_FALLBACK = {
  judgment: "Judgment", followup: "Followup", wbs: "WBS", assignment: "Assignment",
  review: "Review", filing: "Filing", replan: "Replan",
};
const PRIMARY_TABS = ["today", "inbox", "tasks", "wbs"];
const TAB_ORDER = ["today", "inbox", "tasks", "wbs", "reviews", "judgment", "members", "integrations", "tools"];
const SECONDARY_TABS = TAB_ORDER.filter((n) => !PRIMARY_TABS.includes(n));
let inboxState = { data: null, filter: "all", selected: null };
let tabsEnabled = false;

const kindLabel = (kind) => t(`web_inbox_kind_${kind}`, KIND_FALLBACK[kind] || kind);

function ageLabel(seconds) {
  const days = Math.floor(seconds / 86400);
  if (days >= 1) return t("web_inbox_days_ago", "{n} days ago").replace("{n}", String(days));
  const hours = Math.floor(seconds / 3600);
  if (hours >= 1) return t("web_inbox_hours_ago", "{n} hours ago").replace("{n}", String(hours));
  return t("web_inbox_just_now", "just now");
}

function showTab(name) {
  if (!tabsEnabled) return;
  for (const node of document.querySelectorAll("[data-tab]")) {
    if (node.dataset.tab === name) node.removeAttribute("data-off");
    else node.setAttribute("data-off", "");
  }
  for (const button of $("tabs").querySelectorAll("button[data-target]")) {
    button.setAttribute("aria-pressed", String(button.dataset.target === name));
  }
  const more = document.getElementById("tabs-more-button");
  if (more) {                                   // when viewing a "more" screen, mark "more" as selected
    more.setAttribute("aria-pressed", String(SECONDARY_TABS.includes(name)));
    more.setAttribute("aria-expanded", "false");
  }
  $("tabs").removeAttribute("data-more");
  $("main").classList.toggle("wide", name !== "tools");
  if (location.hash !== `#${name}`) history.pushState(null, "", `#${name}`);
}

function setupTabs(enabled) {
  tabsEnabled = enabled;
  const nav = $("tabs");
  if (!enabled) {                       // when inbox is not available, show all tabs as before
    nav.hidden = true;
    return;
  }
  const labels = {
    today: t("web_today", "Today"), inbox: t("web_inbox", "Inbox"), tasks: t("web_tasks", "Tasks"), reviews: t("web_reviews", "Reviews"),
    judgment: t("web_judgment", "Judgment"), members: t("web_members", "Members"),
    integrations: t("web_integrations", "Integrations"), tools: t("web_tools", "Tools"),
    wbs: wbsState.data ? "WBS" : t("web_proposals", "WBS Proposals"),
  };
  nav.replaceChildren();
  const sheet = el("div", "tabs-sheet");
  sheet.id = "tabs-sheet";
  const makeButton = (name) => {
    const button = el("button", null, labels[name]);
    button.type = "button";
    button.dataset.target = name;
    if (name === "inbox") {
      const count = el("span", "count", "");
      count.id = "inbox-count";
      count.hidden = true;
      button.append(count);
    }
    button.addEventListener("click", () => showTab(name));
    return button;
  };
  for (const name of TAB_ORDER) {
    if (PRIMARY_TABS.includes(name)) nav.append(makeButton(name));
    else sheet.append(makeButton(name));
  }
  const more = el("button", "tabs-more", t("web_tabs_more", "More"));
  more.type = "button";
  more.id = "tabs-more-button";
  more.setAttribute("aria-controls", "tabs-sheet");
  more.setAttribute("aria-expanded", "false");
  more.addEventListener("click", () => {
    const open = nav.getAttribute("data-more") === null;
    if (open) nav.setAttribute("data-more", ""); else nav.removeAttribute("data-more");
    more.setAttribute("aria-expanded", String(open));
  });
  nav.append(more, sheet);
  nav.hidden = false;
  $("inbox-view").hidden = false;
  $("today-view").hidden = false;
  $("tasks-view").hidden = false;
  $("reviews-view").hidden = false;
  $("judgment-view").hidden = false;
  $("members-view").hidden = false;
  $("integrations-view").hidden = false;
  const wanted = location.hash.slice(1);
  showTab(TAB_ORDER.includes(wanted) ? wanted : "today");
  renderToday();
  updateInboxCount();
}

function updateInboxCount() {
  const badge = document.getElementById("inbox-count");
  if (!badge) return;
  const total = inboxState.data ? inboxState.data.total : 0;
  badge.textContent = String(total);
  badge.hidden = !total;
  renderToday();
}

function inboxVisible() {
  const data = inboxState.data;
  if (!data) return [];
  return data.items.filter((i) => inboxState.filter === "all" || i.kind === inboxState.filter);
}

function renderInbox() {
  const host = $("inbox");
  const data = inboxState.data;
  host.replaceChildren();
  updateInboxCount();
  if (!data || !data.total) {
    const box = el("div", "inbox-empty");
    box.append(el("strong", null, t("web_inbox_empty", "Nothing awaiting a decision")),
      el("span", null, t("web_inbox_empty_hint", "New proposals and alerts will appear here")));
    host.append(box);
    return;
  }
  const items = inboxVisible();
  if (!items.some((i) => i.id === inboxState.selected)) {
    inboxState.selected = items.length ? items[0].id : null;
  }

  const layout = el("div", "inbox");
  const left = el("div");
  const filters = el("div", "inbox-filters");
  filters.setAttribute("role", "group");
  filters.setAttribute("aria-label", t("web_inbox_filter", "Type"));
  const choices = [["all", t("web_inbox_all", "All"), data.total]]
    .concat(Object.entries(data.by_kind).filter(([, n]) => n > 0)
      .map(([kind, n]) => [kind, kindLabel(kind), n]));
  for (const [kind, label, n] of choices) {
    const button = el("button", null, label);
    button.type = "button";
    button.append(el("span", "n", String(n)));
    button.setAttribute("aria-pressed", String(inboxState.filter === kind));
    button.addEventListener("click", () => { inboxState.filter = kind; renderInbox(); });
    filters.append(button);
  }
  left.append(filters);

  const list = el("ul", "inbox-items");
  for (const item of items) {
    const li = el("li");
    const button = el("button", "inbox-item");
    button.type = "button";
    button.dataset.id = item.id;
    button.setAttribute("aria-current", String(item.id === inboxState.selected));
    const row = el("span", "row");
    const chip = el("span", "kind-chip", kindLabel(item.kind));
    chip.dataset.kind = item.kind;
    row.append(chip, el("span", "age", ageLabel(item.age_seconds)));
    button.append(row, el("span", "title", item.title), el("span", "sub", item.summary));
    button.addEventListener("click", () => selectInboxItem(item.id, true));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const ids = inboxVisible().map((i) => i.id);
      const next = ids[ids.indexOf(item.id) + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) { event.preventDefault(); selectInboxItem(next, false, true); }
    });
    li.append(button);
    list.append(li);
  }
  left.append(list);

  const detail = el("div", "inbox-detail");
  detail.id = "inbox-detail";
  detail.setAttribute("aria-live", "polite");
  layout.append(left, detail);
  host.append(layout);
  renderInboxDetail();
}

function selectInboxItem(id, scroll, focus) {
  inboxState.selected = id;
  for (const button of document.querySelectorAll(".inbox-item")) {
    button.setAttribute("aria-current", String(button.dataset.id === id));
    if (focus && button.dataset.id === id) button.focus();
  }
  renderInboxDetail();
  if (scroll && window.matchMedia("(max-width: 899px)").matches) {
    $("inbox-detail").scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function renderInboxDetail() {
  const pane = $("inbox-detail");
  if (!pane) return;
  pane.replaceChildren();
  const item = (inboxState.data ? inboxState.data.items : []).find((i) => i.id === inboxState.selected);
  if (!item) {
    pane.append(el("div", "inbox-empty", t("web_inbox_select", "Select an item to see its details and actions")));
    return;
  }
  const chip = el("span", "kind-chip", kindLabel(item.kind));
  chip.dataset.kind = item.kind;
  pane.append(chip, el("h3", null, item.title), el("div", "lead", item.summary));
  for (const section of item.detail.sections) {
    const box = el("div", "inbox-section");
    box.dataset.tone = section.tone || "info";
    box.append(el("h4", null, section.heading));
    const ul = el("ul");
    for (const line of section.lines) ul.append(el("li", null, line));
    box.append(ul);
    pane.append(box);
  }

  const canAct = inboxState.data.can_act && item.actions.length;
  if (!canAct) {
    pane.append(el("p", "lead", t("web_inbox_viewer", "The viewer token cannot make decisions")));
    return;
  }
  let note = null;
  if (item.actions.some((a) => a.needs_note)) {
    const wrap = el("div", "inbox-note");
    const label = el("label", null, t("web_inbox_note", "Note (required when rejecting)"));
    note = el("textarea");
    note.id = "inbox-note";
    label.htmlFor = "inbox-note";
    wrap.append(label, note);
    pane.append(wrap);
  }
  const bar = el("div", "inbox-actions");
  for (const action of item.actions) {
    const button = el("button", `btn ${action.style === "primary" ? "btn-primary" : action.style === "danger" ? "btn-reject" : ""}`, action.label);
    button.type = "button";
    button.addEventListener("click", () => runInboxAction(item, action, note, bar));
    bar.append(button);
  }
  pane.append(bar);
}

async function runInboxAction(item, action, note, bar) {
  const text = note ? note.value.trim() : "";
  if (action.needs_note && !text) {
    toast(t("web_inbox_need_note", "Please enter a note"), "error");
    if (note) note.focus();
    return;
  }
  const buttons = bar.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  const body = { ...action.body };
  if (action.needs_note) body.note = text;
  const visible = inboxVisible().map((i) => i.id);
  const at = visible.indexOf(item.id);
  try {
    await api(action.path, { method: action.method, body: JSON.stringify(body) });
    toast(t("web_inbox_done", "Decision recorded"));
    inboxState.selected = visible[at + 1] || visible[at - 1] || null;
    await refreshInbox();
    refreshPmo().catch(() => {});
  } catch (error) {
    toast(error.message, "error");
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function refreshInbox() {
  try {
    const filter = pmoProject ? `?project=${encodeURIComponent(pmoProject)}` : "";
    inboxState.data = await api(`/api/inbox${filter}`);
    renderInbox();
    return true;
  } catch (error) {
    return false;
  }
}

/* ---------- boot ---------- */

async function refreshRuns() {
  const { items } = await api("/api/runs");
  renderRuns(items);
  await refreshQueueStatus();
}

async function refreshQueueStatus() {
  //
  // Read once per visit to the screen, same as the rest of this file's
  // refreshes — no standing poll, to keep with the no-battery-drain rule
  // already followed here.
  const host = $("run-queue-status");
  try {
    const status = await api("/api/queue");
    const parts = [];
    if (status.running) {
      parts.push(t("web_queue_running", "Running: {template}")
        .replace("{template}", status.running.template));
    }
    if (status.waiting.length > 0) {
      parts.push(t("web_queue_waiting", "Waiting: {n}")
        .replace("{n}", String(status.waiting.length)));
    }
    host.textContent = parts.join(" · ");
    host.hidden = parts.length === 0;
  } catch (error) {
    host.hidden = true;
  }
}

async function refreshProposals() {
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

/* ---------- knowledge candidate review -------
 *
 * Candidates placed in the private collection by `vector_store.submit_candidate`
 * (generalize_knowledge and similar templates) awaiting promotion to the public collection.
 * Approving, rejecting, and editing can only be done here (the adapter methods are not
 * callable from templates; only this UI and `aipmo knowledge` call them).
 *
 * Candidates a template like generalize_knowledge placed in the private
 * collection via vector_store.submit_candidate, awaiting promotion. Only
 * this screen and the CLI's `aipmo knowledge` can approve, reject, or edit
 * them — the adapter methods behind this are not reachable from a template.
 */

function renderKnowledge(items) {
  const host = $("knowledge");
  host.replaceChildren();

  if (!items.length) {
    host.append(empty(t("web_no_knowledge", "No pending knowledge candidates.")));
    return;
  }
  for (const item of items) host.append(knowledgeCard(item));
}

function knowledgeCard(item) {
  const payload = item.payload || {};
  const card = document.createElement("div");
  card.className = "proposal knowledge-card";

  const head = document.createElement("div");
  head.className = "card-head";
  const score = document.createElement("span");
  score.className = "tag tier";
  score.textContent = `score ${Math.round(payload.publicability_score || 0)}`;
  head.append(score);
  if (payload.knowledge_level != null) {
    head.append(el("span", "tag option", `level ${payload.knowledge_level}`));
  }
  card.append(head);

  const textArea = document.createElement("textarea");
  textArea.className = "knowledge-text";
  textArea.value = payload.text || "";
  textArea.readOnly = !canRun;
  card.append(textArea);

  if (payload.publicability_reasons && payload.publicability_reasons.length) {
    card.append(jsonBlock(t("web_knowledge_reasons", "reasons"), payload.publicability_reasons));
  }

  card.append(knowledgeReferenceStats(item.id));

  if (canRun) {
    card.append(knowledgeActions(item.id, textArea));
  }
  return card;
}

/* Reference information for a decision: how similar past candidates were decided by humans and the LLM.
 * Loaded when a card is opened, for the person about to make a decision.
 *
 * Reference stats for the person about to decide: how similar past
 * candidates were decided, by a human and by an LLM verdict when recorded.
 * Fetched as soon as the card renders. */
function knowledgeReferenceStats(id) {
  const box = el("div", "knowledge-stats");
  box.textContent = t("web_knowledge_stats_loading", "Loading reference stats…");
  api(`/api/knowledge/${encodeURIComponent(id)}/stats`).then((stats) => {
    box.replaceChildren();
    const head = el("div", "knowledge-stats-head",
      t("web_knowledge_stats_title", "Reference: {n} similar past candidates")
        .replace("{n}", String(stats.total_similar)));
    box.append(head);
    if (!Object.keys(stats.human_decisions).length && !Object.keys(stats.llm_decisions).length) {
      box.append(el("div", "card-note", t("web_knowledge_stats_none", "No decisions on similar candidates yet.")));
      return;
    }
    if (Object.keys(stats.human_decisions).length) {
      box.append(knowledgeStatsRow(t("web_knowledge_stats_human", "Human decisions"), stats.human_decisions));
    }
    if (Object.keys(stats.llm_decisions).length) {
      box.append(knowledgeStatsRow(t("web_knowledge_stats_llm", "LLM verdicts"), stats.llm_decisions));
    }
  }).catch(() => { box.replaceChildren(); });
  return box;
}

function knowledgeStatsRow(label, breakdown) {
  const row = el("div", "knowledge-stats-row");
  row.append(el("span", "knowledge-stats-label", label));
  for (const [key, s] of Object.entries(breakdown)) {
    row.append(el("span", "tag option", `${key}: ${s.count} (${s.percent}%)`));
  }
  return row;
}

function knowledgeActions(id, textArea) {
  const wrap = document.createElement("div");
  wrap.className = "proposal-actions";

  const note = document.createElement("input");
  note.type = "text";
  note.className = "note-input";
  note.placeholder = t("web_proposal_note_placeholder", "Optional note");

  const save = document.createElement("button");
  save.className = "btn";
  save.type = "button";
  save.textContent = t("web_knowledge_save", "Save edit");
  save.addEventListener("click", () => editKnowledgeCandidate(id, textArea.value, wrap));

  const approve = document.createElement("button");
  approve.className = "btn btn-approve";
  approve.type = "button";
  approve.textContent = t("web_approve", "Approve");
  approve.addEventListener("click", () => decideKnowledgeCandidate(id, "approve", note.value, wrap));

  const reject = document.createElement("button");
  reject.className = "btn btn-reject";
  reject.type = "button";
  reject.textContent = t("web_reject", "Reject");
  reject.addEventListener("click", () => decideKnowledgeCandidate(id, "reject", note.value, wrap));

  wrap.append(save, note, approve, reject);
  return wrap;
}

async function editKnowledgeCandidate(id, text, wrap) {
  const buttons = wrap.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/knowledge/${encodeURIComponent(id)}/edit`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    toast(t("web_knowledge_saved", "Saved."));
  } catch (error) {
    toast(error.message, "error");
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function decideKnowledgeCandidate(id, decision, note, wrap) {
  const buttons = wrap.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/knowledge/${encodeURIComponent(id)}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ note: note || null }),
    });
    toast(decision === "approve"
      ? t("web_knowledge_approved", "Approved and published.")
      : t("web_proposal_rejected", "Rejected."));
    await refreshKnowledge();
  } catch (error) {
    toast(error.message, "error");
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function refreshKnowledge() {
  //
  // Some deployments have no vector store configured (not using this
  // feature). That returns 503, treated as "nothing to show" rather than
  // an error.
  try {
    const { items } = await api("/api/knowledge");
    $("h-knowledge").hidden = false;
    renderKnowledge(items);
  } catch (error) {
    $("h-knowledge").hidden = true;
    $("knowledge").replaceChildren();
  }
}

/* ---------- trust-RAG toggle ----
 *
 * Opt-in auto-approval for candidates submitted by self_learning_cycle only
 * (aipmo/self_learning.py). Disabled by default. Returns 404 when there is no ledger,
 * so the checkbox is hidden entirely in that case. Only operators can toggle it
 * (viewers see it read-only).
 *
 * Opt-in auto-approval (aipmo/self_learning.py) for candidates from
 * self_learning_cycle specifically. Off by default; hidden entirely when
 * there is no ledger (404). Only operator can flip it; viewer sees it
 * read-only.
 */
async function refreshLearningControl() {
  const row = $("learning-trust-rag");
  try {
    const { trust_rag } = await api("/api/learning/control");
    row.hidden = false;
    const box = $("learning-trust-rag-checkbox");
    box.checked = trust_rag;
    box.disabled = !canRun;
    $("learning-trust-rag-label").textContent = t("web_learning_trust_rag",
      "Trust the RAG completely (auto-approve candidates from self_learning_cycle)");
  } catch (error) {
    row.hidden = true;
  }
}

async function setLearningTrustRag(enabled) {
  const box = $("learning-trust-rag-checkbox");
  box.disabled = true;
  try {
    await api("/api/learning/trust-rag", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    toast(enabled
      ? t("web_learning_enabled", "Enabled: future candidates from self_learning_cycle are auto-approved.")
      : t("web_learning_disabled", "Disabled: future candidates await a human again."));
  } catch (error) {
    box.checked = !enabled;         // revert on failure
    toast(error.message, "error");
  } finally {
    box.disabled = !canRun;
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
    $("h-knowledge").textContent = t("web_knowledge", "Knowledge");
    $("h-templates").textContent = t("web_templates", "Templates");
    $("h-runs").textContent = t("web_runs", "Runs");

    const { items } = await api("/api/templates");
    renderTemplates(items);
    await refreshRuns();
    await refreshPmo();
    await refreshProposals();
    await refreshKnowledge();
    await refreshLearningControl();
    $("learning-trust-rag-checkbox").addEventListener("change", (event) => {
      setLearningTrustRag(event.target.checked);
    });
    await refreshWbs();
    // With a PMO ledger the sections become tabs and the inbox leads; otherwise nothing changes.
    setupTabs(await refreshInbox());
    if (tabsEnabled) { refreshTasks(true); refreshReviews(); refreshJudgmentControl(); refreshMembers(); refreshIntegrations(); }
    refreshHealth();
  } catch (error) {
    toast(error.message, "error");
  }
}

boot();

// Show the screen the hash names when it changes (back button, links).
const followHash = () => {
  const wanted = location.hash.slice(1);
  if (tabsEnabled && TAB_ORDER.includes(wanted)) showTab(wanted);
};
window.addEventListener("hashchange", followHash);
window.addEventListener("popstate", followHash);

// Refresh on return to the screen; polling on a timer would drain the battery.
function refreshEverything() {
  refreshRuns().catch(() => {});
  refreshPmo().catch(() => {});
  refreshProposals().catch(() => {});
  refreshKnowledge().catch(() => {});
  refreshLearningControl().catch(() => {});
  refreshWbs();
  if (tabsEnabled) {
    refreshInbox(); refreshTasks(false); refreshReviews(); refreshMembers(); refreshIntegrations();
    refreshJudgmentControl();
  }
  refreshHealth();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshEverything();
    checkPulse();
  }
});

/* Auto-refresh: while visible, poll /api/pulse every 30 s and re-read on change.
 * Does not run in hidden tabs (battery). Does not interrupt screens with active input/edits.
 * Visible-tab only: poll a cheap marker every 30 s and reload everything only when it changed. */
const PULSE_SECONDS = 30;
let pulseMark = null;

function editingNow() {
  const a = document.activeElement;
  return Boolean(a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT"))
    || Boolean(document.querySelector(".wbs-form"));
}

async function checkPulse() {
  if (document.hidden) return;
  try {
    const p = await api("/api/pulse");
    const mark = `${p.briefing_at}|${p.log}`;
    if (pulseMark !== null && mark !== pulseMark && !editingNow()) refreshEverything();
    pulseMark = mark;
  } catch (error) {
  }
}

setInterval(checkPulse, PULSE_SECONDS * 1000);

/* ---------- idle auto-logout --------------
 *
 * If there is no mouse/keyboard/touch input for IDLE_LOGOUT_MS, log out and
 * return to the lock screen. The access-key cookie (aipmo_token) is httponly
 * and cannot be cleared from JS, so /api/logout is called to clear it —
 * reloading after that makes the server return 401 and show the lock screen.
 *
 * Logs out and returns to the lock screen after IDLE_LOGOUT_MS with no
 * mouse/keyboard/touch activity at all. The access-key cookie is httponly
 * and cannot be cleared from JS directly, so this calls the server's
 * /api/logout to clear it; reloading afterward gets the 401 lock screen.
 */
const IDLE_LOGOUT_MS = 10 * 60 * 1000;
let idleTimer;

async function idleLogout() {
  try {
    await fetch("/api/logout", { method: "POST", credentials: "include" });
  } catch (error) {
  }
  location.reload();
}

function resetIdleTimer() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(idleLogout, IDLE_LOGOUT_MS);
}

async function manualLogout() {
  try {
    await fetch("/api/logout", { method: "POST", credentials: "include" });
  } catch (error) {
  }
  // Navigate to / with no token in the URL so the lock screen is shown cleanly.
  location.href = "/";
}

document.getElementById("logout-btn").addEventListener("click", manualLogout);

["mousemove", "mousedown", "keydown", "touchstart", "scroll"].forEach((type) => {
  document.addEventListener(type, resetIdleTimer, { passive: true });
});
resetIdleTimer();
