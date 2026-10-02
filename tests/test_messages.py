"""サーバーが組み立てる文章の多言語化（aipmo/messages.py）のテスト。

確かめること:
  (1) 全キーが 8 言語にあり、`{差し込み}` が全言語で同じ（訳し忘れ・差し込みの取り違えを止める）
  (2) 英語など日本語以外の言語では、サーバーが組み立てる文章に日本語が混ざらない（受信箱・点数の内訳・WBS の注意）
  (3) 日本語は、これまでと同じ文章のまま（CLI・既存の画面を変えない）
  (4) 台帳に保存された文章（診断・理由・タイトル）は、書いた時点の言語のまま出る
  (5) 未知の言語は英語に、未知のキーはキーそのものになる（落ちない）

Every key has eight languages with identical placeholders; non-Japanese servers compose no Japanese in the
inbox, score parts or WBS notes; Japanese is unchanged; stored text stays as written; unknown language or key
degrades instead of crashing.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from aipmo import cli, demo
from aipmo.messages import LANGS, MESSAGES, translate

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "msg-operator"
CJK = re.compile(r"[぀-ヿ㐀-鿿]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
NAMES = ("佐藤", "鈴木", "田中", "高橋", "開発AI")                      # メンバー名はデータで、訳さない


def no_cjk(text: str) -> bool:
    for name in NAMES:
        text = text.replace(name, "X")
    return not CJK.search(text)


def test_every_key_has_eight_languages_with_the_same_placeholders():
    assert len(MESSAGES) > 80
    for key, row in MESSAGES.items():
        assert set(row) == set(LANGS), key
        reference = sorted(PLACEHOLDER.findall(row["ja"]))
        for lang in LANGS:
            assert row[lang].strip(), (key, lang)
            assert sorted(PLACEHOLDER.findall(row[lang])) == reference, (key, lang)


def test_languages_other_than_japanese_and_chinese_are_not_japanese():
    for key, row in MESSAGES.items():
        for lang in ("en", "ko", "es", "fr", "de", "pt"):
            assert not CJK.search(row[lang]) or lang == "ko" and not re.search(r"[぀-ヿ]", row[lang]) \
                and not re.search(r"[一-鿿]", row[lang]), (key, lang)
        assert not re.search(r"[぀-ヿ]", row["zh"]), key       # 中国語に仮名は混ざらない


def test_missing_language_or_key_does_not_crash():
    assert translate("xx", "act_approve") == "Approve"                # 未知の言語は英語
    assert translate("ja", "no_such_key") == "no_such_key"            # 未知のキーはキーそのもの
    assert translate("en", "j_summary") == "Proposal: {remedy}"       # 差し込み不足でも落ちない
    assert translate("en", "j_summary", remedy="X") == "Proposal: X"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    base = tmp_path_factory.mktemp("msg") / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path, lang: str) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, lang=lang,
                     store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]),
                     filing=cli._web_filing(config), wbs_view=cli._wbs_view(config, base))
    return TestClient(app)


def get(client: TestClient, path: str):
    response = client.get(path, headers={"x-aipmo-token": OPERATOR})
    assert response.status_code == 200, response.text
    return response.json()


def composed_inbox_text(item: dict) -> list[str]:
    """サーバーが組み立てた文章だけ（見出し・操作・決まった文）。台帳の文章（タイトル・根拠の行）は除く。"""
    out = [a["label"] for a in item["actions"]] + [s["heading"] for s in item["detail"]["sections"]]
    # 担当・成果の要約は、メンバー名（データ）を含むので除く
    return out if item["kind"] in ("assignment", "review") else out + [item["summary"]]


@pytest.mark.parametrize("lang", ["en", "es", "fr", "de", "pt"])
def test_the_inbox_is_composed_in_the_servers_language(base, lang):
    items = get(client_for(base, lang), "/api/inbox")["items"]
    assert len(items) == 14
    for item in items:
        for text in composed_inbox_text(item):
            assert no_cjk(text), (lang, item["id"], text)
        for section in item["detail"]["sections"]:
            if section["tone"] in ("do", "dont"):                    # 「承認すると…」「しないこと」はすべて訳される
                assert all(no_cjk(line) for line in section["lines"]), (lang, section)
    kinds = {i["kind"] for i in items}
    assert kinds == {"judgment", "followup", "wbs", "assignment", "review", "filing"}


def test_japanese_is_unchanged_and_each_language_differs(base):
    ja = get(client_for(base, "ja"), "/api/inbox")["items"]
    judgment = next(i for i in ja if i["kind"] == "judgment")
    assert [a["label"] for a in judgment["actions"]] == ["承認する", "却下する"]
    assert [s["heading"] for s in judgment["detail"]["sections"]][-3:] == ["承認すると", "しないこと", "却下すると"]
    headings = set()
    for lang in LANGS:
        data = get(client_for(base, lang), "/api/inbox")["items"]
        first = next(i for i in data if i["kind"] == "judgment")
        headings.add(first["actions"][0]["label"])
    assert len(headings) >= 6                                       # 言語ごとに別の文言


def test_stored_text_stays_as_it_was_written(base):
    en = get(client_for(base, "en"), "/api/inbox")["items"]
    judgment = next(i for i in en if i["kind"] == "judgment")
    assert CJK.search(judgment["title"])                           # 診断のタイトルは台帳に保存された日本語
    assert not CJK.search(judgment["summary"])                     # 「提案: …」は訳される


def test_score_parts_are_composed_in_the_servers_language(base):
    en = get(client_for(base, "en"), "/api/tasks?limit=200")["items"]
    texts = [p["text"] for i in en for p in i["parts"]]
    assert texts and not any(CJK.search(t) for t in texts)
    assert any(t.startswith("Priority ") for t in texts) and any("past the due date" in t for t in texts)
    ja = get(client_for(base, "ja"), "/api/tasks?limit=200")["items"]
    assert any("期限を" in p["text"] for i in ja for p in i["parts"])
    for item in en:
        assert sum(p["points"] for p in item["parts"]) == item["score"]


def test_wbs_notes_and_evidence_reasons_are_composed_in_the_servers_language(base):
    en = get(client_for(base, "en"), "/api/wbs")
    messages = [p["message"] for p in en["problems"]]
    assert messages and not any(CJK.search(m) for m in messages)
    assert any("evidence is missing" in m for m in messages) and any("All the evidence" in m for m in messages)
    nodes = []

    def walk(items):
        for n in items:
            nodes.append(n)
            walk(n.get("children", []))

    walk(en["tree"])
    reasons = [e["why"] for n in nodes for e in n.get("evidence", []) if not e["ok"]]
    assert reasons and not any(CJK.search(r) for r in reasons) and "was not found" in reasons[0]
    ja = get(client_for(base, "ja"), "/api/wbs")
    assert any("証拠がありません" in p["message"] for p in ja["problems"])
