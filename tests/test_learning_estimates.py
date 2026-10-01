"""優先度の重みと見積り精度（ペース）の学習のテスト / priority-weight and estimate learning.

確かめるのは、(1) 実績から重みとペースが決定論的に出ること、(2) 標本が少ない・見積りが
当たっていないときは使わないこと（歯止め）、(3) 台帳が見積りと着手を実績に残すこと、
(4) 採点が学習結果を反映し、理由を説明できること、(5) PMO Core が一巡して反映すること。

(1) weights and pace come out of track record deterministically; (2) nothing is used on
too few samples or when the estimates prove inaccurate; (3) the ledger records estimate
and start in the outcome; (4) scoring reflects what was learned and says why;
(5) the PMO Core wires it through a whole cycle.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from aipmo import cli
from aipmo.pmo_core import Member, PmoCore
from aipmo.pmo_learning import (
    PRIORITY_DELTA_CAP,
    learn,
)
from aipmo.task_engine import Task, TaskEngine, extract_candidates, score_task
from aipmo.wbs import task_items

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 1)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": [], "tracker": "", "external_id": ""}
    return {**base, **kw}


def outcome(priority=None, late=0, who="ann", effort=None, days=None):
    return {"assignee": who, "late_days": late, "labels": [], "priority": priority,
            "effort": effort, "duration_days": days}


# ===== (1) 優先度の重み / priority weights ========================================

def test_a_priority_that_slips_more_than_average_weighs_more_and_one_that_lands_weighs_less():
    outcomes = ([outcome("Low", late=5)] * 20 + [outcome("High", late=0)] * 20
                + [outcome("Medium", late=0)] * 10 + [outcome("Medium", late=3)] * 10)
    delta = learn(outcomes).priority_delta
    assert delta["low"] > 0 > delta["high"]
    assert "medium" not in delta or abs(delta["medium"]) <= 1          # 平均どおりならほぼ動かない


def test_priority_deltas_are_clamped_shrunk_and_need_enough_samples():
    huge = learn([outcome("Low", late=9)] * 300 + [outcome("High", late=0)] * 300).priority_delta
    assert abs(huge["low"]) <= PRIORITY_DELTA_CAP and abs(huge["high"]) <= PRIORITY_DELTA_CAP
    # 遅れ率 60% 対 20%（上限に張り付かない差）で、標本数だけを変える
    def mixed(n):
        late = round(n * 0.6)
        low = [outcome("Low", late=9)] * late + [outcome("Low", late=0)] * (n - late)
        early = round(n * 0.2)
        high = [outcome("High", late=9)] * early + [outcome("High", late=0)] * (n - early)
        return learn(low + high).priority_delta
    assert 0 < mixed(5)["low"] < mixed(50)["low"] <= PRIORITY_DELTA_CAP   # 小標本は平均側へ縮む
    few = learn([outcome("Low", late=9)] * 4 + [outcome("High", late=0)] * 40).priority_delta
    assert "low" not in few                                             # 5 件に満たない


def test_unset_priorities_and_undated_outcomes_teach_nothing():
    assert learn([outcome(None, late=5)] * 20 + [outcome("High", late=0)] * 20
                 ).priority_delta.get("") is None
    assert learn([outcome("Low", late=None)] * 30).priority_delta == {}


def test_priority_learning_can_be_switched_off():
    outcomes = [outcome("Low", late=5)] * 20 + [outcome("High", late=0)] * 20
    assert learn(outcomes, priority=False).priority_delta == {}
    assert learn(outcomes).priority_delta != {}


# ===== (1) ペースと見積り精度 / pace and estimate accuracy ============================

def steady(n=8, effort=2, days=4, who="ann"):
    return [outcome(effort=effort, days=days, who=who) for _ in range(n)]


def test_pace_is_the_median_days_per_point_and_a_steady_team_is_reliable():
    pace = learn(steady()).pace
    assert pace["team"] == 2.0 and pace["samples"] == 8
    assert pace["median_error"] == 0 and pace["reliable"] is True


def test_one_extreme_outcome_does_not_drag_the_pace():
    pace = learn(steady(7) + [outcome(effort=2, days=400)]).pace
    assert pace["team"] == 2.0                                          # 平均なら 100 を超える


def test_estimates_that_miss_widely_are_not_reliable_and_so_not_used():
    # 同じ 2 点が 1 日で終わったり 40 日かかったりする。見積りは当てにならない。
    noisy = [outcome(effort=2, days=d) for d in (1, 40, 2, 35, 1, 50, 3, 45)]
    pace = learn(noisy).pace
    assert pace["team"] is not None and pace["median_error"] > 0.75
    assert pace["reliable"] is False
    assert learn(noisy, max_error=50).pace["reliable"] is True           # 閾値は設定できる


def test_accuracy_is_judged_without_the_sample_it_is_predicting():
    # ペースが 1 日/点 と 10 日/点 に割れている履歴。全体の中央値（1）を自分も含めて
    # 当てると、1 日/点 の標本は誤差 0 で「当たっている」ように見える。自分を除いて
    # 測ると、残りの中央値は 5.5 になり、当たっていないことが分かる。
    # Pace splits between 1 and 10 days per point. Predicting each sample from a
    # median that includes itself looks perfect; leaving it out shows the truth.
    split = [outcome(effort=2, days=2)] * 3 + [outcome(effort=2, days=20)] * 2
    pace = learn(split).pace
    # 1 日/点 の 3 件は、自分を除くと予測が 5.5 日/点 になり、誤差 4.5 倍。
    # 中央値の誤差は 4.5 で、当てにならない。
    assert pace["median_error"] == pytest.approx(4.5) and pace["reliable"] is False


def test_members_pace_is_blended_toward_the_team_and_needs_enough_samples():
    outcomes = steady(10, days=4, who="ann") + steady(6, days=10, who="bob") \
        + steady(2, days=40, who="cat")
    pace = learn(outcomes).pace
    assert pace["members"]["ann"] < pace["members"]["bob"]
    assert "cat" not in pace["members"]                                  # 5 件に満たない
    team = pace["team"]
    assert team < pace["members"]["bob"] < 5.0                           # チームへ寄せる（縮小）


def test_samples_without_an_estimate_or_a_start_teach_no_pace():
    junk = [outcome(effort=None, days=3), outcome(effort=0, days=3), outcome(effort=-1, days=3),
            outcome(effort=True, days=3), outcome(effort=2, days=None)] * 4
    pace = learn(junk).pace
    assert pace["samples"] == 0 and pace["team"] is None and pace["reliable"] is False


def test_finishing_within_a_day_counts_as_one_day_and_too_few_samples_teach_nothing():
    assert learn([outcome(effort=2, days=0)] * 6).pace["team"] == 0.5    # 最短 1 日
    assert learn(steady(4)).pace["team"] is None
    assert learn(steady(8), estimates=False).pace["team"] is None


def test_learning_is_deterministic_and_the_signature_ignores_mere_growth():
    data = steady(8) + [outcome("Low", late=3), outcome("High", late=0)] * 6
    assert learn(data).as_dict() == learn(list(data)).as_dict()
    # ペースの標本が増えただけ（値は同じ）なら、変化として記録しない
    assert learn(steady(8), priority=False).signature() == learn(
        steady(9), priority=False).signature()
    assert learn(steady(8), priority=False).signature() != learn(
        steady(8, days=9), priority=False).signature()


# ===== (3) 台帳が見積りと着手を残す / the ledger records estimate and start ==========

def test_an_estimate_is_read_from_the_usual_field_names():
    def effort_of(item):
        return extract_candidates({"items": [{"key": "A-1", "summary": "x", **item}]}, "jira")[0]["effort"]
    assert effort_of({"effort": 3}) == 3
    assert effort_of({"story_points": 5}) == 5
    assert effort_of({"points": "2.5"}) == 2.5
    assert effort_of({"estimate": 1}) == 1
    for bad in ({"effort": 0}, {"effort": -2}, {"effort": True}, {"effort": "many"}, {}):
        assert effort_of(bad) is None


def test_wbs_nodes_carry_their_estimate_into_the_ledger():
    from aipmo.wbs import Node, Wbs
    node = Node(id="1.1", name="n", effort=3.0)
    items = task_items(Wbs(id="w", name="W", deadline=None, velocity_per_day=None,
                           velocity_window_days=28, roots=[node]))
    assert items[0]["effort"] == 3.0
    assert extract_candidates({"items": items}, "wbs_file")[0]["effort"] == 3.0


def test_start_is_the_first_time_it_was_seen_in_progress_and_survives_a_step_back(tmp_path):
    clock = Clock()
    te = TaskEngine(tmp_path / "l.db", now=clock)
    te.ingest("t", "r1", [cand(key="A-1", title="x", status="To Do", effort=2)])
    assert te.find("A-1").started_at is None
    clock.now = NOW + timedelta(days=2)
    te.ingest("t", "r2", [cand(key="A-1", title="x", status="In Progress", effort=2)])
    started = te.find("A-1").started_at
    assert started == clock.now.isoformat()
    clock.now = NOW + timedelta(days=3)
    te.ingest("t", "r3", [cand(key="A-1", title="x", status="To Do")])   # 差し戻し
    te.ingest("t", "r4", [cand(key="A-1", title="x", status="In Review")])
    assert te.find("A-1").started_at == started and te.find("A-1").effort == 2


def test_a_finished_task_leaves_its_estimate_and_actual_days(tmp_path):
    clock = Clock()
    te = TaskEngine(tmp_path / "l.db", now=clock)
    te.ingest("t", "r1", [cand(key="A-1", title="x", status="In Progress", effort=3,
                               assignee="ann", due_date="2026-10-20"),
                          cand(key="A-2", title="y", status="To Do")])
    clock.now = NOW + timedelta(days=5)
    te.ingest("t", "r2", [cand(key="A-1", title="x", done=True),
                          cand(key="A-2", title="y", done=True)])
    by_task = {o["task"]: o for o in te.outcomes}
    assert by_task["JIRA:A-1"]["effort"] == 3 and by_task["JIRA:A-1"]["duration_days"] == 5
    assert by_task["JIRA:A-2"]["effort"] is None and by_task["JIRA:A-2"]["duration_days"] is None


# ===== (4) 採点への反映 / scoring ===================================================

def open_task(**kw) -> Task:
    base = dict(id="JIRA:A-1", key="A-1", title="t", due_date="2026-10-11", effort=5.0,
                assignee="ann", priority="Medium")
    return Task(**{**base, **kw})


def test_a_priority_delta_changes_the_weight_and_says_so():
    plain, why_plain = score_task(open_task(due_date=None), TODAY)
    more, why_more = score_task(open_task(due_date=None), TODAY, priority_delta={"medium": 6})
    less, why_less = score_task(open_task(due_date=None), TODAY, priority_delta={"medium": -6})
    assert more == plain + 6 and less == plain - 6
    assert "実績による補正 +6" in why_more[0] and "実績による補正 -6" in why_less[0]
    assert why_plain[0] == "優先度 Medium +15"                           # 補正が無ければ従来どおり
    floor, _ = score_task(open_task(due_date=None, priority="Lowest"), TODAY,
                          priority_delta={"lowest": -10})
    assert floor == 0                                                    # 優先度の点は 0 を下回らない
    unaffected, _ = score_task(open_task(due_date=None, priority="High"), TODAY,
                               priority_delta={"medium": 9})
    assert unaffected == score_task(open_task(due_date=None, priority="High"), TODAY)[0]


PACE = {"reliable": True, "team": 2.0, "members": {}}


def test_a_task_the_estimate_cannot_fit_before_its_due_date_is_raised_with_the_reason():
    task = open_task()                                  # 5 点 × 2 日/点 = 10 日、期限まで 10 日
    base = score_task(task, TODAY)[0]
    assert score_task(task, TODAY, pace=PACE)[0] == base                # ちょうど間に合う → 加点なし
    tight = open_task(due_date="2026-10-08")            # 期限まで 7 日。足りない 3 日 → +10
    points, reasons = score_task(tight, TODAY, pace=PACE)
    assert points == score_task(tight, TODAY)[0] + 10
    assert any("5 点 × 実績ペース 2.0 日/点" in r and "+10" in r for r in reasons)
    very = open_task(due_date="2026-10-04")             # 期限まで 3 日。足りない 7 日 → +20
    assert score_task(very, TODAY, pace=PACE)[0] == score_task(very, TODAY)[0] + 20


@pytest.mark.parametrize("task,pace", [
    (open_task(due_date="2026-10-04"), {**PACE, "reliable": False}),     # 見積りが当たっていない
    (open_task(due_date="2026-10-04"), None),
    (open_task(due_date="2026-10-04", effort=None), PACE),               # 見積りが無い
    (open_task(due_date=None), PACE),                                    # 期限が無い
    (open_task(due_date="2026-09-20"), PACE),                            # すでに超過（別に加点済み）
])
def test_pace_is_not_applied_without_reliable_pace_estimate_and_due_date(task, pace):
    assert score_task(task, TODAY, pace=pace)[0] == score_task(task, TODAY)[0]


def test_the_members_own_pace_is_preferred_and_time_already_spent_is_credited():
    slow = {"reliable": True, "team": 1.0, "members": {"ann": 3.0}}
    task = open_task(due_date="2026-10-08", effort=3.0)   # 3 点: 自分のペース 3 → 9 日 > 7 日
    assert score_task(task, TODAY, pace=slow)[0] > score_task(task, TODAY)[0]
    assert score_task(open_task(due_date="2026-10-08", effort=3.0, assignee="bob"), TODAY,
                      pace=slow)[0] == score_task(
        open_task(due_date="2026-10-08", effort=3.0, assignee="bob"), TODAY)[0]   # チーム 1.0 → 3 日
    started = open_task(due_date="2026-10-08", effort=3.0,
                        started_at=(NOW - timedelta(days=5)).isoformat())
    assert score_task(started, TODAY, pace=slow)[0] == score_task(started, TODAY)[0]  # 残り 4 日 ≤ 7


# ===== (5) PMO Core が一巡して反映する / a whole cycle ================================

def seed_history(te: TaskEngine, who="ann", n=8, effort=2, days=4, priority="Low", late=0):
    te.add_outcomes([outcome(priority, late=late, who=who, effort=effort, days=days)
                     for _ in range(n)])


def test_the_cycle_applies_priority_and_pace_and_logs_the_change_once(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    seed_history(te, n=10)
    seed_history(te, n=10, priority="High", late=0)
    te.add_outcomes([outcome("Low", late=6)] * 10)
    te.ingest("t", "r", [cand(key="A-1", title="間に合わない", priority="Medium", effort=5,
                              assignee="ann", due_date="2026-10-08")])
    core = PmoCore(task_engine=te, members=[Member("ann")])
    briefing = core.cycle()

    learned = briefing["learning"]
    assert learned["priority_delta"]["low"] > 0
    assert learned["pace"]["reliable"] is True and learned["pace"]["team"] == 2.0
    task = te.find("A-1")
    assert any("実績ペース" in r for r in task.reasons)
    core.cycle()
    log = (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8")
    assert log.count("model_updated") == 1
    entry = json.loads([line for line in log.splitlines() if "model_updated" in line][0])
    assert entry["pace_reliable"] is True and entry["pace_team"] == 2.0


def test_pace_changes_the_ranking_only_when_learning_of_estimates_is_on(tmp_path):
    def rank(**flags):
        folder = tmp_path / ("on" if not flags else "off")
        folder.mkdir()
        te = TaskEngine(folder / "task-ledger.db", now=Clock())
        seed_history(te, n=10)
        te.ingest("t", "r", [cand(key="A-1", title="tight", priority="Medium", effort=5,
                                  assignee="ann", due_date="2026-10-08"),
                             cand(key="A-2", title="relaxed", priority="Medium", effort=1,
                                  assignee="ann", due_date="2026-10-30")])
        PmoCore(task_engine=te, members=[Member("ann")], **flags).cycle()
        return {t.key: t.score for t in te.ranked()}

    on, off = rank(), rank(learn_estimates=False)
    assert on["A-1"] > off["A-1"] and on["A-2"] == off["A-2"]


def test_an_inaccurate_history_leaves_the_ranking_alone(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    te.add_outcomes([outcome(effort=2, days=d) for d in (1, 40, 2, 35, 1, 50, 3, 45)])
    te.ingest("t", "r", [cand(key="A-1", title="tight", effort=5, assignee="ann",
                              due_date="2026-10-04")])
    core = PmoCore(task_engine=te, members=[Member("ann")])
    briefing = core.cycle()
    assert briefing["learning"]["pace"]["reliable"] is False
    assert not any("実績ペース" in r for r in te.find("A-1").reasons)


def test_turning_learning_off_clears_every_learned_number(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    seed_history(te, n=10)
    core = PmoCore(task_engine=te, members=[Member("ann")])
    core.cycle()
    assert te.pace and te.pace["reliable"]
    core.learning = False
    core.cycle()
    assert te.pace == {} and te.priority_delta == {} and te.label_bonus == {}


def test_a_real_history_teaches_the_pace_end_to_end(tmp_path):
    """タスクが着手され完了するのを台帳が観測し、そこからペースが出て順位に効く。"""
    clock = Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    core = PmoCore(task_engine=te, members=[Member("ann")])
    for i in range(6):                                  # 2 点を 4 日で終える、を 6 回
        key = f"H-{i}"
        te.ingest("t", f"s{i}", [cand(key=key, title=key, status="In Progress",
                                      effort=2, assignee="ann")])
        clock.now += timedelta(days=4)
        te.ingest("t", f"d{i}", [cand(key=key, title=key, done=True)])
    te.ingest("t", "open", [cand(key="N-1", title="次の仕事", status="To Do", effort=5,
                                 assignee="ann", due_date=(clock.now + timedelta(days=6)
                                                          ).date().isoformat())])
    briefing = core.cycle()
    pace = briefing["learning"]["pace"]
    assert pace["team"] == 2.0 and pace["samples"] == 6 and pace["reliable"] is True
    assert any("実績ペース 2.0 日/点" in r for r in te.find("N-1").reasons)


def test_cli_shows_the_learned_priority_and_pace(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\npmo_core:\n  members: [ann]\n", encoding="utf-8")
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock(), tenant="acme")
    seed_history(te, n=10, priority="High")                  # High は期限内、Low は遅れる
    te.add_outcomes([outcome("Low", late=6)] * 10)
    te.close()
    assert cli.main(["--config", str(config), "pmo"]) == 0
    out = capsys.readouterr().out
    assert "優先度 low: 重み +" in out and "ペース: チーム 2 日/点" in out
    assert "順位に使う" in out

    off = tmp_path / "off.yaml"
    off.write_text("tenant: acme\npmo_core:\n  members: [ann]\n  learning:\n"
                   "    estimates: false\n    priority: false\n", encoding="utf-8")
    assert cli.main(["--config", str(off), "pmo"]) == 0
    assert "ペース" not in capsys.readouterr().out


def test_cli_tasks_ranks_with_what_was_learned_and_does_not_store_unlearned_scores(
        tmp_path, capsys):
    """`aipmo tasks` は再採点を台帳に保存する。学習なしの点数を書き込んではいけない。

    以前はここで学習結果を読み込まず、ラベルの加点を含む学習の効果が、このコマンドの
    実行のたびに台帳から消えて画面とずれていた。
    """
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\npmo_core:\n  members: [ann]\n", encoding="utf-8")
    real_now = datetime.now(timezone.utc)
    te = TaskEngine(tmp_path / "task-ledger.db", tenant="acme")
    seed_history(te, n=10)
    seed_history(te, n=10, priority="High")
    te.add_outcomes([outcome("Low", late=6)] * 10)
    te.ingest("t", "r", [cand(
        key="A-1", title="間に合わない", priority="Medium", effort=5, assignee="ann",
        due_date=(real_now + timedelta(days=6)).date().isoformat())])
    te.close()

    assert cli.main(["--config", str(config), "tasks", "--why"]) == 0
    out = capsys.readouterr().out
    assert "実績ペース 2.0 日/点" in out
    stored = TaskEngine(tmp_path / "task-ledger.db", tenant="acme").find("A-1")
    assert any("実績ペース" in r for r in stored.reasons)              # 台帳にも学習込みで残る

    off = tmp_path / "off.yaml"
    off.write_text("tenant: acme\npmo_core:\n  members: [ann]\n  learning: {enabled: false}\n",
                   encoding="utf-8")
    assert cli.main(["--config", str(off), "tasks", "--why"]) == 0
    assert "実績ペース" not in capsys.readouterr().out


def test_the_screen_shows_priority_corrections_and_the_pace_without_parsing_html():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "aipmo" / "web" / "static"
          / "app.js").read_text(encoding="utf-8")
    assert "priority_delta" in js and "web_pmo_pace" in js and "web_pmo_pace_unused" in js
    assert "innerHTML" not in js
