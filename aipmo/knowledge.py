"""ナレッジ候補の「公開可能性スコア」算出。

最終判断は必ず人間が行う。ここでの数値は、レビュー待ち一覧
（`pending_candidates` は publicability_score の降順）の並び順を決めるための
下書きであって、承認・却下そのものを自動化するものではない。

数えれば決まる・照合すれば決まる範囲だけを見る。言語モデルには頼らない
— 「公開してよいか」は誤ると取り返しがつかない判断で、間違っても
もっともらしく見えるものに任せるべきではない。

The final call is always a human's. This score only orders the review queue
(`pending_candidates` sorts by it, descending); it never approves or rejects
anything by itself.

Only checks things that are countable or matchable are used here — no
language model. Whether something is safe to publish is a decision that
cannot be undone if wrong, and that is not a call to hand to something that
is plausible even when mistaken.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Jira 風の課題キー。実装の具体的な識別子は、一般化された知見には要らない。
# A Jira-style issue key: a concrete implementation identifier a generalized
# pattern has no need to carry.
ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

# knowledge_level（3=まだ具体的 〜 6=最も一般化）ごとの基点。
# 差の分だけ余地を残し、残りをテキストの精査で加減する。
# Base score per knowledge_level (3 = still specific .. 6 = most generalized),
# leaving room for the text checks below to adjust it.
LEVEL_BASE = {3: 10.0, 4: 35.0, 5: 60.0, 6: 85.0}


@dataclass
class PublicabilityScore:
    value: float
    # 人が読んで分かる根拠。ブラックボックスの数値だけを渡さない。
    # Reasons a person can read; the number never travels alone.
    reasons: list[str] = field(default_factory=list)


def score_publicability(
    knowledge: dict[str, Any],
    *,
    knowledge_level: int = 3,
    consent_level: str | None = None,
    tenant: str | None = None,
) -> PublicabilityScore:
    """0〜100 の目安値を算出する / compute a 0-100 estimate.

    consent_level は `postgres.consent_level` の結果（A/B/C）をそのまま渡す
    ことを想定している。A（二次利用不可）は無条件で 0 にする — 基点や
    テキストの中身に関わらず、そのテナントの知見は外に出さない。

    `consent_level` is meant to be passed straight from the result of
    `postgres.consent_level` (A/B/C). A (no secondary use) forces 0
    unconditionally — nothing about the base score or the text content can
    override that a tenant's knowledge does not leave the tenant.
    """
    reasons: list[str] = []
    score = LEVEL_BASE.get(knowledge_level, LEVEL_BASE[3])
    reasons.append(f"knowledge_level={knowledge_level} → 基点 {score:.0f}")

    if consent_level == "A":
        reasons.append("利用許諾レベル A（二次利用不可） → 0点に固定")
        return PublicabilityScore(0.0, reasons)
    if consent_level == "C":
        score += 10
        reasons.append("利用許諾レベル C（事例公開可） → +10")
    elif consent_level is None:
        # 未確認のまま高い点数を出すと、確認したかのように見える。
        # A high score without a checked consent level would read as checked.
        score *= 0.5
        reasons.append("利用許諾レベルが未確認 → 慎重のため半減")

    text = " ".join(_flatten_strings(knowledge))

    if EMAIL_RE.search(text):
        score -= 30
        reasons.append("メールアドレスらしき文字列を含む → -30")

    if tenant and tenant.lower() in text.lower():
        score -= 40
        reasons.append("テナント名を含む可能性 → -40")

    if ISSUE_KEY_RE.search(text):
        score -= 10
        reasons.append("課題番号のような具体的な識別子を含む → -10")

    score = max(0.0, min(100.0, score))
    return PublicabilityScore(score, reasons)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for v in value.values():
            found.extend(_flatten_strings(v))
        return found
    if isinstance(value, list):
        found = []
        for v in value:
            found.extend(_flatten_strings(v))
        return found
    return []
