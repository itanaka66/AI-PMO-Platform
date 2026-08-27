/* AI-PMO — スマホ向け画面の挙動 / mobile interface behaviour.
 *
 * ビルド工程を持たない。自前で立てるサーバーに Node のツールチェーンを
 * 要求したくないので、素の JS のまま置く。
 * No build step: a self-hosted server should not require a Node toolchain.
 */

const $ = (id) => document.getElementById(id);

let strings = {};
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

/* ---------- テンプレート / templates ---------- */

function renderTemplates(items) {
  const host = $("templates");
  host.replaceChildren();

  if (!items.length) {
    host.append(empty(t("web_no_templates", "No templates found.")));
    return;
  }

  for (const item of items) {
    const card = document.createElement("button");
    card.className = "card";
    card.disabled = !item.valid;

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

    if (item.valid) {
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

  meta.append(name, id, when);
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
    document.documentElement.lang = session.lang || "en";
    $("tenant").textContent = session.tenant;
    $("h-templates").textContent = t("web_templates", "Templates");
    $("h-runs").textContent = t("web_runs", "Runs");

    const { items } = await api("/api/templates");
    renderTemplates(items);
    await refreshRuns();
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
    refreshHealth();
  }
});
