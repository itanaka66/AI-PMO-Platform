"""Teams の Transcript (WebVTT) を読む / parsing Teams transcripts.

Graph が返す Transcript は WebVTT で、発言者は `<v 山田 太郎>` の形で入る。
議事録の質は、誰が言ったかを取り違えないことにかかっている。担当者の割り当てを
間違えた TODO は、無いより悪い。

Graph returns transcripts as WebVTT with the speaker in a `<v Name>` tag. The
quality of the minutes rests on not confusing who said what: a task assigned to
the wrong person is worse than no task at all.

純粋な関数にしてある。ネットワークなしで実データの形を試せるようにするため。
Kept as pure functions so the real data shape can be exercised without network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# <v 山田 太郎>発言</v> — 閉じタグは省略されることがある
# The closing tag is often absent.
SPEAKER = re.compile(r"<v\s+([^>]+?)>(.*?)(?:</v>|$)", re.S)

# 00:00:12.345 --> 00:00:15.678
TIMING = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
)

TAGS = re.compile(r"<[^>]+>")


@dataclass
class Utterance:
    speaker: str | None
    text: str
    start: str | None = None
    end: str | None = None


def parse_vtt(content: str) -> list[Utterance]:
    """WebVTT を発言の並びにする / turn WebVTT into a sequence of utterances."""
    utterances: list[Utterance] = []

    for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n")):
        block = block.strip()
        if not block or block.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        start = end = None
        lines: list[str] = []
        for line in block.split("\n"):
            timing = TIMING.search(line)
            if timing:
                start, end = timing.group(1), timing.group(2)
                continue
            # キュー識別子（UUID や連番）は本文ではない
            # A cue identifier is not dialogue.
            if not lines and _is_cue_id(line):
                continue
            lines.append(line)

        body = "\n".join(lines).strip()
        if not body:
            continue

        match = SPEAKER.search(body)
        if match:
            speaker = match.group(1).strip()
            text = TAGS.sub("", match.group(2)).strip()
        else:
            speaker = None
            text = TAGS.sub("", body).strip()

        if text:
            utterances.append(Utterance(speaker=speaker, text=text,
                                        start=start, end=end))

    return utterances


def _is_cue_id(line: str) -> bool:
    stripped = line.strip()
    if not stripped or "-->" in stripped:
        return False
    if stripped.isdigit():
        return True
    # Teams は UUID 形式のキュー識別子を付ける / Teams uses UUID-shaped cue ids
    return bool(re.fullmatch(r"[0-9a-f-]{20,}(/\d+-\d+)?", stripped, re.I))


def merge_consecutive(utterances: list[Utterance]) -> list[Utterance]:
    """同じ人の連続した発言をまとめる。

    WebVTT は数秒ごとに切れるので、そのまま渡すと一人の発言が
    十数個の断片になる。文の途中で切れた状態で LLM に渡すと、
    発言の意図を取り違える。

    WebVTT breaks every few seconds, so one person's point arrives as a dozen
    fragments. Handing those to the model mid-sentence distorts what was meant.
    """
    merged: list[Utterance] = []
    for utterance in utterances:
        if merged and merged[-1].speaker == utterance.speaker:
            previous = merged[-1]
            merged[-1] = Utterance(
                speaker=previous.speaker,
                text=f"{previous.text} {utterance.text}".strip(),
                start=previous.start,
                end=utterance.end,
            )
        else:
            merged.append(utterance)
    return merged


def to_text(utterances: list[Utterance], with_timestamps: bool = False) -> str:
    """LLM に渡す平文にする / flatten for the model."""
    lines = []
    for utterance in utterances:
        name = utterance.speaker or "(不明)"
        stamp = f"[{utterance.start[:8]}] " if with_timestamps and utterance.start else ""
        lines.append(f"{stamp}{name}: {utterance.text}")
    return "\n".join(lines)


def speakers(utterances: list[Utterance]) -> list[str]:
    """登場した発言者を、最初に出た順で返す。

    集合ではなく順序を保つ。議事録の参加者欄が毎回並び替わると、
    差分が読めなくなる。

    Order is preserved rather than using a set: an attendee list that reshuffles
    on every run makes the minutes impossible to diff.
    """
    seen: list[str] = []
    for utterance in utterances:
        if utterance.speaker and utterance.speaker not in seen:
            seen.append(utterance.speaker)
    return seen
