"""`aipmo knowledge`（ナレッジ公開候補のレビュー）の CLI テスト。

アダプタ自体の振る舞い（list_candidates・edit_candidate・decide_candidate）は
tests/test_data_adapters.py で確認済み。ここでの主眼は、CLI の出力と
配線（--backend・--status・--text・--by・--note、知らない候補 id の扱い）。

The adapter's own behaviour is covered in tests/test_data_adapters.py. The
focus here is the CLI's output and wiring (--backend/--status/--text/--by/
--note, and handling of an unknown candidate id).
"""
from __future__ import annotations

from aipmo import cli
from aipmo.adapters.base import AdapterRegistry
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider
from aipmo.llm.embeddings import HashEmbedder
from aipmo.llm.registry import LLMRegistry

from tests.test_data_adapters import FakeQdrantClient, QdrantAdapter


def cli_world(tmp_path, monkeypatch):
    client = FakeQdrantClient()
    adapter = QdrantAdapter(tenant="acme", embedder=HashEmbedder(), client=client)
    reg = AdapterRegistry()
    reg.register(adapter, name="vector_store")
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(reg, llms))
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")
    return config, adapter, client


def submit(adapter, text, score=50):
    adapter.invoke("submit_candidate", {"knowledge": {"text": text}, "publicability_score": score})


def test_list_shows_pending_sorted_by_score(tmp_path, monkeypatch, capsys):
    config, adapter, _ = cli_world(tmp_path, monkeypatch)
    submit(adapter, "低い方", score=10)
    submit(adapter, "高い方", score=90)

    assert cli.main(["--config", str(config), "knowledge", "list"]) == 0
    out = capsys.readouterr().out
    assert out.index("高い方") < out.index("低い方")
    assert "score=90" in out and "score=10" in out


def test_list_with_no_candidates_still_succeeds(tmp_path, monkeypatch, capsys):
    config, _, _ = cli_world(tmp_path, monkeypatch)
    assert cli.main(["--config", str(config), "knowledge", "list"]) == 0
    assert "(0)" in capsys.readouterr().out


def test_show_prints_text_and_reasons(tmp_path, monkeypatch, capsys):
    config, adapter, client = cli_world(tmp_path, monkeypatch)
    submit(adapter, "主要担当者への依存はスケジュールリスクになる")
    candidate_id = client.upserts[0][1][0].id

    assert cli.main(["--config", str(config), "knowledge", "show", candidate_id]) == 0
    out = capsys.readouterr().out
    assert "主要担当者への依存" in out
    assert "根拠" in out


def test_show_unknown_id_fails_cleanly(tmp_path, monkeypatch, capsys):
    config, _, _ = cli_world(tmp_path, monkeypatch)
    assert cli.main(["--config", str(config), "knowledge", "show", "nope"]) == 1
    assert "見つかりません" in capsys.readouterr().err


def test_edit_requires_text(tmp_path, monkeypatch, capsys):
    config, adapter, client = cli_world(tmp_path, monkeypatch)
    submit(adapter, "下書き")
    candidate_id = client.upserts[0][1][0].id

    assert cli.main(["--config", str(config), "knowledge", "edit", candidate_id]) == 1
    assert "--text" in capsys.readouterr().err


def test_edit_rewrites_content(tmp_path, monkeypatch, capsys):
    config, adapter, client = cli_world(tmp_path, monkeypatch)
    submit(adapter, "下書き")
    candidate_id = client.upserts[0][1][0].id

    assert cli.main(["--config", str(config), "knowledge", "edit", candidate_id,
                     "--text", "書き直した"]) == 0
    assert adapter.get_candidate(candidate_id)["payload"]["text"] == "書き直した"


def test_approve_promotes_and_records_reviewer(tmp_path, monkeypatch, capsys):
    config, adapter, client = cli_world(tmp_path, monkeypatch)
    submit(adapter, "主要担当者への依存はスケジュールリスクになる")
    candidate_id = client.upserts[0][1][0].id

    code = cli.main(["--config", str(config), "knowledge", "approve", candidate_id,
                     "--by", "sato", "--note", "良い一般化"])
    assert code == 0
    assert "承認して公開コレクションへ複製しました" in capsys.readouterr().out

    record = adapter.get_candidate(candidate_id)
    assert record["payload"]["review_status"] == "approved"
    assert record["payload"]["reviewed_by"] == "sato"
    assert record["payload"]["review_note"] == "良い一般化"
    public_collection, public_points = next(
        (c, p) for c, p in client.upserts if c == "public_pmo_knowledge")
    assert public_points[0].id == candidate_id


def test_reject_never_writes_public(tmp_path, monkeypatch, capsys):
    config, adapter, client = cli_world(tmp_path, monkeypatch)
    submit(adapter, "x")
    candidate_id = client.upserts[0][1][0].id

    assert cli.main(["--config", str(config), "knowledge", "reject", candidate_id,
                     "--by", "sato"]) == 0
    assert "却下しました" in capsys.readouterr().out
    assert all(c != "public_pmo_knowledge" for c, _ in client.upserts)
    assert adapter.get_candidate(candidate_id)["payload"]["review_status"] == "rejected"


def test_approve_unknown_id_fails_cleanly(tmp_path, monkeypatch, capsys):
    config, _, _ = cli_world(tmp_path, monkeypatch)
    assert cli.main(["--config", str(config), "knowledge", "approve", "nope"]) == 1
    assert "見つかりません" in capsys.readouterr().err


def test_backend_flag_picks_a_named_adapter_instead_of_the_logical_one(tmp_path, monkeypatch, capsys):
    client = FakeQdrantClient()
    adapter = QdrantAdapter(tenant="acme", embedder=HashEmbedder(), client=client)
    reg = AdapterRegistry()
    reg.register(adapter, name="qdrant")           # registered under its own name only
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(reg, llms))
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")

    assert cli.main(["--config", str(config), "knowledge", "list"]) == 1   # no logical "vector_store"
    assert "vector_store" in capsys.readouterr().err

    assert cli.main(["--config", str(config), "knowledge", "list", "--backend", "qdrant"]) == 0
