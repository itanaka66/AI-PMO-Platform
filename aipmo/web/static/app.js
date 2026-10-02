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
  card.append(previewBlock(item));

  if (canRun) {
    card.append(proposalActions(item));
  }

  return card;
}

/* 承認したらファイルがどう変わるか。開いたときに読む（読むだけ。何も書かない）。
 * What approving would do to the file; fetched when opened, read-only. */
function previewBlock(item) {
  const details = document.createElement("details");
  details.className = "proposal-json proposal-preview";
  const summary = document.createElement("summary");
  summary.textContent = t("web_preview", "承認したらどうなるか（反映後の差分）");
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
          ? p.problems.join(" / ") : t("web_preview_none", "この提案はファイルに反映できません")));
        return;
      }
      if (p.already_applied) body.append(el("div", "card-note", t("web_preview_applied", "すでに反映済みです")));
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
    // 反映先の WBS ファイルがあるとき、承認は反映までひと続き。結果を伝える。
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

  // 役割AIに任せた記録。直近の 1 件だけ（状態と、結果または理由）。
  // 外部由来の文字列なので textContent で入れる。
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

// そのタスクのトラッカーに書き戻せるか。宛先（トラッカーと識別子）が分かり、
// そのアダプタが更新できるときだけ。
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

// 承認したタスクを課題管理ツールに起票する(operator のみ)。起票は外の世界に課題を作る
// ので、ボタンを押したときだけ。見送ると、この一覧から消える。
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

// 役割AIの成果を、人が確かめて「認める／差し戻す」(operator のみ)。差し戻しには理由が要る。
// 外部由来の文字列は textContent で入れる。
// A human checks a role AI's result: accept, or send back with a reason (operator only).
function reviewRow(item) {
  const row = el("div", "pmo-proposal");
  row.append(el("div", "pmo-title", item.title));
  row.append(el("div", "pmo-meta", `${t("web_pmo_agent", "Role AI")} ${item.agent} · ${item.task}`));
  if (item.excerpt) row.append(el("div", "pmo-meta pmo-agent-result", item.excerpt));
  if (canRun) {
    const note = el("input");
    note.type = "text";
    note.placeholder = t("web_pmo_review_note", "Reason (required to send back)");
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

  // PMO Core が警告から起こした対応タスクの提案。承認されるまで仕事ではない。
  // Follow-up tasks the PMO Core proposed from an alert; not work until approved.
  // PMO Core の自律的な判断: いまの診断と、直近の判断。見るだけ(承認は上の提案から)。
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
      // 見積りが当たっていないときは、ペースを順位に使っていないことを明記する。
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

/* ---------- 今日 / today ----------
 * /api/pmo のブリーフィングと受信箱の件数を、1 画面に組み替える。新しい API も計算も持たない
 * （数えるのは画面で、台帳の判断はサーバーが済ませたもの）。文字は textContent で入れる。
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
    host.append(empty(t("web_today_none", "PMO のデータがまだありません")));
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
    kpi(t("web_today_active", "進行中"), briefing.active_count),
    kpi(t("web_today_alerts", "警告"), (briefing.alerts || []).length,
        (briefing.alerts || []).length ? "warn" : ""),
    kpi(t("web_today_waiting", "判断待ち"), waiting, waiting ? "do" : ""),
    kpi(t("web_today_overloaded", "過負荷"), overloaded, overloaded ? "warn" : ""),
  );
  host.append(kpis);

  if (waiting) {
    const go = el("button", "btn today-go", `${t("web_today_open_inbox", "受信箱で決める")} (${waiting})`);
    go.type = "button";
    go.addEventListener("click", () => showTab("inbox"));
    host.append(go);
  }

  const cols = el("div", "today-cols");
  const left = el("div"), right = el("div");

  const top = todaySection(t("web_today_top", "まず手を付けるもの"), null);
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
    const block = todaySection(t("web_today_projects", "プロジェクト"), projects.length);
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
    const block = todaySection(t("web_today_load", "メンバーの負荷"), null);
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
    const block = todaySection(t("web_today_judgment", "自律的な判断"), null);
    const state = j.tripped ? t("web_today_tripped", "遮断器が作動")
      : j.paused ? t("web_today_paused", "一時停止中") : t("web_today_running", "動作中");
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

/* ---------- メンバーと学習 / members & learning ----------
 * /api/learning/members（読むだけ）。上限がなぜ補正されたかを、実績（期限内の割合・平均の遅れ・件数）で見せる。
 * 実績が少ない人は補正しない（係数 ×1.00）ことも、そのまま出す。
 *
 * Why each capacity was adjusted, from the track record; members with too few samples are shown uncorrected.
 */
let membersState = { data: null };

async function refreshMembers() {
  try {
    membersState.data = await api("/api/learning/members");
  } catch (error) {
    membersState.data = null;                       // 403（範囲を限られた閲覧）や台帳なし
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
    facts.push(`${t("web_members_ontime", "期限内")} ${Math.round(m.on_time_rate * 100)}%`);
    facts.push(`${t("web_members_late", "平均の遅れ")} ${m.avg_late_days} d`);
  }
  if (m.pace != null) facts.push(`${t("web_members_pace", "ペース")} ${m.pace} d/pt`);
  facts.push(`${t("web_members_samples", "実績")} ${m.samples}`);
  card.append(el("div", "pmo-meta", facts.join(" · ")));
  if (m.samples < 5) card.append(el("div", "pmo-meta", t("web_members_few", "実績が少ないので補正していません")));
  return card;
}

function renderMembers() {
  const host = document.getElementById("members");
  if (!host) return;
  host.replaceChildren();
  const data = membersState.data;
  if (!data) {
    host.append(empty(t("web_today_none", "PMO のデータがまだありません")));
    return;
  }
  const grid = el("div", "members-grid");
  for (const m of data.members) grid.append(memberCard(m));
  host.append(grid);

  const side = todaySection(t("web_members_learned", "学習した補正"), null);
  const base = data.baseline_late_rate != null ? `${Math.round(data.baseline_late_rate * 100)}%` : "—";
  side.append(el("div", "pmo-meta",
    `${t("web_members_samples", "実績")} ${data.samples} · ${t("web_members_baseline", "全体の遅れ率")} ${base}`));
  for (const l of data.labels) {
    side.append(el("div", "pmo-meta", `${l.label}: +${l.bonus}${l.samples ? ` (${l.samples})` : ""}`));
  }
  for (const p of data.priorities) {
    side.append(el("div", "pmo-meta",
      `${p.priority}: ${p.delta > 0 ? "+" : ""}${p.delta}${p.samples ? ` (${p.samples})` : ""}`));
  }
  side.append(el("div", "pmo-meta", data.pace.reliable
    ? t("web_members_pace_ok", "見積りは当たっているので、順位に使います")
    : t("web_members_pace_no", "見積りの当たりが確かめられないので、順位には使いません")));
  host.append(side);
}

/* ---------- 連携 / integrations ----------
 * /api/integrations（読むだけ）。アダプタの疎通・書き戻せるか・収集の直近の結果・WBS ファイルの検査・
 * 起票の設定・メンバーのアカウント。名前からの引き当ては外部への問い合わせなので、ここでは行わない
 * （`aipmo members`）。
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

function renderIntegrations() {
  const host = document.getElementById("integrations");
  if (!host) return;
  host.replaceChildren();
  const d = integState.data;
  if (!d) {
    host.append(empty(t("web_today_none", "PMO のデータがまだありません")));
    return;
  }
  const grid = el("div", "members-grid");
  for (const a of d.adapters) {
    const card = el("div", "member-card integ-card");
    card.dataset.healthy = String(a.healthy);
    const head = el("div", "member-head");
    head.append(el("span", "pmo-title", a.name),
      el("span", "tag", a.healthy ? t("web_integ_ok", "疎通") : t("web_integ_down", "不通")));
    card.append(head);
    card.append(el("div", "pmo-meta", a.writeback
      ? t("web_integ_writeback", "担当・起票を書き戻せる") : t("web_integ_readonly", "読むだけ")));
    grid.append(card);
  }
  host.append(grid);

  const col = todaySection(t("web_integ_collect", "進捗の収集"), null);
  if (!d.collection) {
    col.append(el("div", "pmo-meta", t("web_integ_collect_none", "収集は設定されていないか、まだ動いていません")));
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

  const wbs = todaySection(t("web_integ_wbs", "WBS ファイルの検査"), null);
  if (!d.wbs) {
    wbs.append(el("div", "pmo-meta", "—"));
  } else {
    wbs.append(el("div", "pmo-meta", `${clock(d.wbs.checked_at)} · ${d.wbs.file}`));
    wbs.append(d.wbs.error ? el("div", "card-note error", d.wbs.error)
      : el("div", "pmo-meta", `${t("web_integ_drift", "ずれ")} ${d.wbs.found}`));
  }
  host.append(wbs);

  if (d.filing) {
    const f = todaySection(t("web_integ_filing", "起票"), d.filing.pending);
    f.append(el("div", "pmo-meta",
      `${d.filing.tracker} · auto: ${d.filing.auto.length ? d.filing.auto.join(", ") : "—"} · ${d.filing.can_file ? t("web_integ_ok", "疎通") : t("web_integ_down", "不通")}`));
    host.append(f);
  }

  const acc = todaySection(t("web_integ_accounts", "メンバーのアカウント"), d.accounts.length);
  for (const m of d.accounts) {
    const pairs = Object.entries(m.accounts).map(([k, v]) => `${k}: ${v}`);
    acc.append(el("div", "pmo-meta", `${m.member}${m.is_agent ? " (AI)" : ""} — ${pairs.length ? pairs.join(", ") : "—"}`));
  }
  acc.append(el("div", "pmo-meta", d.lookup_assignees
    ? t("web_integ_lookup_on", "書いていない人は、名前でトラッカーから引き当てます（aipmo members で確認）")
    : t("web_integ_lookup_off", "名前の引き当ては無効です")));
  host.append(acc);
}

/* ---------- 成果レビュー / reviews ----------
 * 役割AIの成果を人が確かめた履歴（/api/agents/reviews、読むだけ）と、いま待っているもの。
 * 認める／差し戻すは受信箱で行う（書き込みの入口を増やさない）。
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
    host.append(empty(t("web_today_none", "PMO のデータがまだありません")));
    return;
  }
  const cards = el("div", "kpis");
  for (const [agent, c] of Object.entries(data.tally)) {
    const total = c.accepted + c.rejected;
    cards.append(kpi(agent, total ? `${Math.round((c.accepted / total) * 100)}%` : "—"));
    cards.lastChild.append(el("div", "pmo-meta",
      `${t("web_review_accepted", "認めた")} ${c.accepted} / ${t("web_review_rejected", "差し戻し")} ${c.rejected}`));
  }
  if (cards.children.length) host.append(cards);

  const pending = (briefing.agent_review && briefing.agent_review.pending) || [];
  const wait = todaySection(t("web_review_pending", "確認待ち"), pending.length);
  for (const p of pending) {
    const row = el("div", "today-task");
    const body = el("div", "today-task-body");
    body.append(el("div", "pmo-title", p.title), el("div", "pmo-meta", [p.project, p.agent].filter(Boolean).join(" · ")));
    if (p.excerpt) body.append(el("div", "review-excerpt", p.excerpt));
    row.append(body);
    wait.append(row);
  }
  if (pending.length) {
    const go = el("button", "btn today-go", t("web_today_open_inbox", "受信箱で決める"));
    go.type = "button";
    go.addEventListener("click", () => { inboxState.filter = "review"; showTab("inbox"); renderInbox(); });
    wait.append(go);
  } else {
    wait.append(el("div", "pmo-meta", "—"));
  }
  host.append(wait);

  const history = todaySection(t("web_review_history", "確かめた履歴"), data.total);
  for (const e of data.items) {
    const row = el("div", "review-row");
    row.dataset.decision = e.decision;
    const label = e.decision === "accepted" ? t("web_review_accepted", "認めた") : t("web_review_rejected", "差し戻し");
    row.append(el("span", "tag", label), el("span", "pmo-title", e.title || e.task),
      el("div", "pmo-meta", [e.project, e.agent, e.by, clock(e.at)].filter(Boolean).join(" · ")));
    if (e.note) row.append(el("div", "pmo-meta", e.note));
    history.append(row);
  }
  if (!data.items.length) history.append(el("div", "pmo-meta", "—"));
  host.append(history);
}

/* ---------- 自律的な判断 / judgment ----------
 * 状態・自律度・診断・直近の判断（ブリーフィングから）。operator は、止める／再開／遮断器を戻すだけできる
 * （POST /api/judgment/*）。自律度の変更は設定ファイルのまま — 画面からは変えられない。
 *
 * State, autonomy, diagnoses and recent actions from the briefing. An operator may pause, resume or reset
 * the breaker; autonomy levels stay in the config file.
 */
let judgmentCtl = {};

async function refreshJudgmentControl() {
  try {
    judgmentCtl = (await api("/api/judgment/control")).control || {};
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
    toast(t("web_judgment_next_cycle", "依頼しました。常駐が次の周で反映します"));
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
    host.append(empty(t("web_judgment_off", "自律的な判断は設定されていません")));
    return;
  }
  // 画面の「止める」は依頼。常駐が次の周で読むまで、ブリーフィングの状態は変わらない。
  // 依頼済みの状態（control）を出して、押したのに何も起きないように見えないようにする。
  // A pause is a request the resident reads next cycle; show the requested state so a press is visible.
  const wantPaused = judgmentCtl.paused === undefined ? j.paused : Boolean(judgmentCtl.paused);
  const head = el("div", "today-head");
  const state = j.tripped ? t("web_today_tripped", "遮断器が作動")
    : j.paused ? t("web_today_paused", "一時停止中") : t("web_today_running", "動作中");
  const chip = el("span", "tag level", state);
  chip.dataset.level = j.tripped ? "critical" : j.paused ? "high" : "low";
  head.append(chip, el("span", "pmo-meta", `${t("web_today_waiting", "判断待ち")} ${j.pending}`));
  if (wantPaused !== j.paused) {
    const note = el("span", "tag", wantPaused ? t("web_judgment_req_pause", "一時停止を依頼済み（次の周で反映）")
      : t("web_judgment_req_resume", "再開を依頼済み（次の周で反映）"));
    note.id = "judgment-requested";
    head.append(note);
  }
  host.append(head);

  if (canRun) {
    const bar = el("div", "judgment-controls");
    for (const [action, label] of [["pause", t("web_judgment_pause", "一時停止")],
                                   ["resume", t("web_judgment_resume", "再開")],
                                   ["reset", t("web_judgment_reset", "遮断器を戻す")]]) {
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

  const levels = todaySection(t("web_judgment_autonomy", "自律度"), null);
  for (const [kind, mode] of Object.entries(j.autonomy || {})) {
    const row = el("div", "today-project");
    const tag = el("span", "tag", mode);
    tag.dataset.mode = mode;
    row.append(el("span", "pmo-title", kind), tag);
    levels.append(row);
  }
  levels.append(el("div", "pmo-meta", t("web_judgment_config_only", "自律度は設定ファイル（config.yaml）で変えます")));
  host.append(levels);

  const diagnoses = todaySection(t("web_judgment_diagnoses", "いまの診断"), (j.diagnoses || []).length);
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

  const recent = todaySection(t("web_judgment_recent", "直近の判断"), (j.recent || []).length);
  for (const r of j.recent || []) {
    const row = el("div", "review-row");
    row.append(el("span", "tag", r.state), el("span", "pmo-title", r.title),
      el("div", "pmo-meta", `${clock(r.at)}${r.result ? ` · ${r.result}` : ""}`));
    recent.append(row);
  }
  host.append(recent);
}

/* ---------- タスク / tasks ----------
 * /api/tasks（読むだけ）の一覧と、/api/tasks/{id} の詳細。点数は項目ごとの内訳（parts）を棒で見せる。
 * 担当の確定・起票などの操作は、受信箱と PMO Core にある（この画面には書き込みが無い）。
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
  search.placeholder = t("web_tasks_search", "検索");
  search.setAttribute("aria-label", t("web_tasks_search", "検索"));
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
  const all = t("web_tasks_all", "すべて");
  wrap.append(
    search,
    select("tasks-project", t("web_today_projects", "プロジェクト"), tasksState.query.project,
      [["", all], ...data.projects.map((p) => [p, p])],
      (v) => { tasksState.query.project = v; refreshTasks(true); }),
    select("tasks-assignee", t("web_tasks_assignee", "担当"), tasksState.query.assignee,
      [["", all], ["-", t("web_tasks_unassigned", "担当なし")], ...data.assignees.map((p) => [p, p])],
      (v) => { tasksState.query.assignee = v; refreshTasks(true); }),
    select("tasks-sort", t("web_tasks_sort", "並び"), tasksState.query.sort,
      [["score", t("web_tasks_sort_score", "点数")], ["due", t("web_tasks_sort_due", "期限")],
       ["priority", t("web_tasks_sort_priority", "優先度")]],
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
    el("span", "pmo-meta", [item.project, item.key, item.assignee || t("web_tasks_unassigned", "担当なし"),
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
    pane.append(el("p", "lead", t("web_tasks_pick", "タスクを選ぶと、点数の内訳が出ます")));
    return;
  }
  pane.append(el("h3", "wbs-dh", d.title));
  pane.append(el("div", "pmo-meta", [d.project, d.key || d.id, d.status, d.assignee, d.due_date,
    d.priority].filter(Boolean).join(" · ")));
  const parts = el("div", "task-parts");
  parts.append(el("h4", "wbs-eh", `${t("web_tasks_parts", "点数の内訳")} — ${d.score}`));
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
    block.append(el("h4", "wbs-eh", t("web_today_alerts", "警告")));
    for (const a of d.alerts) {
      const line = el("div", "wbs-msg", `${a.title} — ${a.message}`);
      line.dataset.level = a.severity === "critical" || a.severity === "high" ? "error" : "warn";
      block.append(line);
    }
    pane.append(block);
  }
  if (d.dispatches.length) {
    const block = el("div");
    block.append(el("h4", "wbs-eh", t("web_tasks_runs", "役割AIの実行")));
    for (const r of d.dispatches.slice(-5)) {
      block.append(el("div", "pmo-meta", `${r.agent || ""} · ${r.status} · ${clock(r.at)}`));
    }
    pane.append(block);
  }
  if (d.history.length) {
    const block = el("div");
    block.append(el("h4", "wbs-eh", t("web_tasks_history", "判断の履歴")));
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
    host.append(empty(t("web_today_none", "PMO のデータがまだありません")));
    return;
  }
  host.append(tasksFilters(data));
  const grid = el("div", "tasks-grid");
  const list = el("div", "tasks-list");
  if (!data.items.length) list.append(empty("—"));
  for (const item of data.items) list.append(tasksRow(item));
  if (data.total > data.items.length) {
    const more = el("button", "btn tasks-more",
      `${t("web_tasks_more", "さらに表示")} (${data.items.length}/${data.total})`);
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
 * /api/wbs（読むだけ）の木を、表と工程バーで見せる。WBS ファイルを変える操作は、この画面には無い
 * （変更の提案は下の「再計画提案」から、人が承認する）。工程バーは日付だけから描く：
 * 終わり = 完了日、無ければ期限。始まり = 依存先の終わりの最も遅い日、無ければ軸の左端。
 * 日付の無い作業にはバーを描かない。文字は textContent で入れる。
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

function wbsDetail(host) {
  host.replaceChildren();
  const node = wbsState.nodes.get(wbsState.selected);
  if (!node) {
    host.append(el("p", "lead", t("web_wbs_pick", "作業を選ぶと、証拠と依存が出ます")));
    return;
  }
  const messages = (wbsState.data.problems || []).filter((p) => p.node === node.id);
  host.append(el("h3", "wbs-dh", `${node.id} ${node.name}`));
  const facts = [node.owner, node.priority, node.due && `${t("web_wbs_deadline", "期限")} ${node.due}`,
    node.done_on && `done ${node.done_on}`, node.effort != null && `${node.effort} pt`].filter(Boolean);
  if (facts.length) host.append(el("div", "pmo-meta", facts.join(" · ")));
  if (node.depends_on && node.depends_on.length) {
    host.append(el("div", "pmo-meta", `${t("web_wbs_depends", "依存")}: ${node.depends_on.join(", ")}`));
  }
  for (const p of messages) {
    const note = el("div", "wbs-msg", p.message);
    note.dataset.level = p.level;
    host.append(note);
  }
  if (node.leaf) {
    const block = el("div", "wbs-evidence");
    block.append(el("h4", "wbs-eh", t("web_wbs_evidence", "証拠")));
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
  head.append(el("div", "pmo-meta", t("web_wbs_progress", "進捗（見積りの重み）")), meter);
  const facts = [
    `${summary.done}/${summary.leaves} done`, `${summary.in_progress} doing`, `${summary.blocked} blocked`,
    data.wbs.deadline && `${t("web_wbs_deadline", "期限")} ${data.wbs.deadline}`,
    data.velocity.per_day != null && `${t("web_wbs_velocity", "速度")} ${data.velocity.per_day}/d`,
  ].filter(Boolean);
  head.append(el("div", "pmo-meta", facts.join(" · ")));
  if (data.forecast) {
    const f = data.forecast;
    const drift = f.drift_days != null ? `drift ${f.drift_days > 0 ? "+" : ""}${f.drift_days} d` : "";
    head.append(el("div", "pmo-meta", `${f.projected_finish || f.finish_date || ""} ${drift}`.trim()));
  } else if (data.error_count) {
    head.append(el("div", "card-note error",
      t("web_wbs_forecast_off", "予測は出せません（先に誤りを直してください）")));
  }
  host.append(head);

  const grid = el("div", "wbs-grid");
  const table = el("div", "wbs-table");
  table.setAttribute("role", "list");
  wbsRows(data.tree, wbsAxis(data), table);
  const side = el("aside", "wbs-detail");
  side.id = "wbs-detail";
  wbsDetail(side);
  grid.append(table, side);
  host.append(grid);

  const lower = el("div", "wbs-lower");
  if (data.ready && data.ready.length) {
    const block = todaySection(t("web_wbs_ready", "次に着手できる"), data.ready.length);
    for (const r of data.ready.slice(0, 6)) {
      block.append(el("div", "pmo-meta", `${r.id} ${r.name}${r.effort != null ? ` · ${r.effort} pt` : ""}`));
    }
    lower.append(block);
  }
  if (data.problems && data.problems.length) {
    const block = todaySection(t("web_wbs_problems", "ずれ・注意"), data.problems.length);
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
  } catch (error) {                       // 設定が無い(404)・権限が無い(403)は「使わない画面」
    wbsState.data = null;
    $("wbs-screen").replaceChildren();
    return false;
  }
}

/* ---------- 受信箱 / inbox ----------
 * 人の判断を待っているもの（判断・提案・担当・成果のレビュー・起票・再計画案）を、種類をまたいで
 * 1 つの一覧に集める。一覧は /api/inbox（読むだけ）。決める操作は、各項目の actions が指す
 * 既存の API を、そのまま呼ぶ — 書き込みの入口は増やさない。文字は外から来るので textContent で入れる。
 *
 * The inbox gathers everything waiting for a human decision. The list comes from /api/inbox (read-only);
 * deciding calls the existing endpoint each item's `actions` names, so no new write path exists.
 */
const KIND_FALLBACK = {
  judgment: "判断", followup: "対応", wbs: "WBS", assignment: "担当",
  review: "成果", filing: "起票", replan: "再計画",
};
const TAB_ORDER = ["today", "inbox", "tasks", "wbs", "reviews", "judgment", "members", "integrations", "tools"];
let inboxState = { data: null, filter: "all", selected: null };
let tabsEnabled = false;

const kindLabel = (kind) => t(`web_inbox_kind_${kind}`, KIND_FALLBACK[kind] || kind);

function ageLabel(seconds) {
  const days = Math.floor(seconds / 86400);
  if (days >= 1) return t("web_inbox_days_ago", "{n} 日前").replace("{n}", String(days));
  const hours = Math.floor(seconds / 3600);
  if (hours >= 1) return t("web_inbox_hours_ago", "{n} 時間前").replace("{n}", String(hours));
  return t("web_inbox_just_now", "いま");
}

function showTab(name) {
  if (!tabsEnabled) return;
  for (const node of document.querySelectorAll("[data-tab]")) {
    if (node.dataset.tab === name) node.removeAttribute("data-off");
    else node.setAttribute("data-off", "");
  }
  for (const button of $("tabs").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.target === name));
  }
  $("main").classList.toggle("wide", name !== "tools");
  if (location.hash !== `#${name}`) history.pushState(null, "", `#${name}`);
}

function setupTabs(enabled) {
  tabsEnabled = enabled;
  const nav = $("tabs");
  if (!enabled) {                       // 受信箱が使えない構成は、従来どおり全部を並べる
    nav.hidden = true;
    return;
  }
  const labels = {
    today: t("web_today", "今日"), inbox: t("web_inbox", "受信箱"), tasks: t("web_tasks", "タスク"), reviews: t("web_reviews", "成果"),
    judgment: t("web_judgment", "判断"), members: t("web_members", "メンバー"),
    integrations: t("web_integrations", "連携"), tools: t("web_tools", "ツール"),
    wbs: wbsState.data ? "WBS" : t("web_proposals", "WBS Proposals"),
  };
  nav.replaceChildren();
  for (const name of TAB_ORDER) {
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
    nav.append(button);
  }
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
    box.append(el("strong", null, t("web_inbox_empty", "判断を待つものはありません")),
      el("span", null, t("web_inbox_empty_hint", "新しい提案や警告が出ると、ここに集まります")));
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
  filters.setAttribute("aria-label", t("web_inbox_filter", "種類"));
  const choices = [["all", t("web_inbox_all", "すべて"), data.total]]
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
    pane.append(el("div", "inbox-empty", t("web_inbox_select", "項目を選ぶと、理由と操作が出ます")));
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
    pane.append(el("p", "lead", t("web_inbox_viewer", "閲覧用のトークンでは、決められません")));
    return;
  }
  let note = null;
  if (item.actions.some((a) => a.needs_note)) {
    const wrap = el("div", "inbox-note");
    const label = el("label", null, t("web_inbox_note", "理由（差し戻すときは必須）"));
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
    toast(t("web_inbox_need_note", "理由を書いてください"), "error");
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
    toast(t("web_inbox_done", "決めました"));
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
    await refreshWbs();
    // 受信箱が使える構成（PMO Core の台帳がある）なら、タブで切り替える。無ければ従来どおり。
    // With a PMO ledger the sections become tabs and the inbox leads; otherwise nothing changes.
    setupTabs(await refreshInbox());
    if (tabsEnabled) { refreshTasks(true); refreshReviews(); refreshJudgmentControl(); refreshMembers(); refreshIntegrations(); }
    refreshHealth();
  } catch (error) {
    toast(error.message, "error");
  }
}

boot();

// 戻るボタンやリンクでハッシュが変わったら、その画面を出す。
// Show the screen the hash names when it changes (back button, links).
const followHash = () => {
  const wanted = location.hash.slice(1);
  if (tabsEnabled && TAB_ORDER.includes(wanted)) showTab(wanted);
};
window.addEventListener("hashchange", followHash);
window.addEventListener("popstate", followHash);

// 画面に戻ったときだけ更新する。定期ポーリングは電池を消費するので避ける。
// Refresh on return to the screen; polling on a timer would drain the battery.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshRuns().catch(() => {});
    refreshPmo().catch(() => {});
    refreshProposals().catch(() => {});
    refreshWbs();
    if (tabsEnabled) { refreshInbox(); refreshTasks(false); refreshReviews(); refreshMembers(); refreshIntegrations(); }
    refreshHealth();
  }
});
