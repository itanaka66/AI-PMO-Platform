"""メンバーの名前から、トラッカーのユーザー ID を引き当てる。

Plane は担当者を UUID、OpenProject は数値の ID で指定する。これまでは運用者が
`pmo_core.members[].accounts` に手で書く必要があった。トラッカーが返す「このプロジェクトで
担当にできる人」の一覧（アダプタの `list_assignees`、読み取り専用）と突き合わせて、
名前（や email）から ID を引き当てる。

**推測はしない。** 一致するのは、正規化した文字列が**完全に等しい**ときだけ
（大文字小文字・全角半角・空白の違いは吸収する。前方一致・部分一致・読みや綴りの近さは使わない）。
候補が 1 人に定まらなければ**書かない**（別人に割り当てるくらいなら、割り当てない方がよい）。
照合は強い手がかりから順に、最初に一致が出た段で決める：

1. email … メンバーの `email` と、ユーザーの email が等しい
2. ログイン名 … メンバーの名前と、ユーザーのログイン名・表示名（ユーザー名）が等しい
3. 氏名 … メンバーの名前と、ユーザーの氏名（姓名・名姓どちらの並びも）が等しい

その段で 1 人なら確定、2 人以上なら**曖昧**として止める（弱い段へ落ちて当て推量をしない）。
運用者が `accounts` に書いたものがあれば、常にそれを使う（引き当ては使わない）。

Resolves a member's name to a tracker user id. Never guessed: a match is a normalized
string that is *equal* (case, full/half width and spacing are absorbed; no prefix, substring
or "close" match). If it does not settle on exactly one person, nothing is written — assigning
nobody beats assigning the wrong person. Tiers are tried strongest first and the first tier
with any match decides: one person resolves, several is ambiguous (it never falls through to a
weaker tier to guess). An `accounts` entry written by the operator always wins.
"""
from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import AdapterError, AdapterRegistry

LIST_ACTION = "list_assignees"

# 一覧を使い回す秒数。常駐の起票が、書き込みごとに一覧を取り直さないように。
# How long a fetched list is reused, so a resident process does not refetch per write.
CACHE_SECONDS = 300.0


def normalize(text: Any) -> str:
    """比較用に正規化する：全角半角（NFKC）・大文字小文字・空白（全角含む）を吸収する。"""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(value.split())


@dataclass(frozen=True)
class Person:
    """トラッカーが返した、担当にできる人 1 人。"""
    id: str
    name: str = ""                      # 表示用の氏名
    login: str = ""
    display_name: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""

    def full_names(self) -> set[str]:
        out = {normalize(self.name)}
        if self.first_name or self.last_name:
            out.add(normalize(f"{self.first_name}{self.last_name}"))
            out.add(normalize(f"{self.last_name}{self.first_name}"))
        return {n for n in out if n}

    def label(self) -> str:
        return self.name or self.display_name or self.login or self.id


def person_of(item: dict[str, Any]) -> Person | None:
    """アダプタの `list_assignees` の 1 件から Person を作る。ID が無ければ使えない。"""
    ident = item.get("id")
    if ident in (None, ""):
        return None
    return Person(
        id=str(ident), name=str(item.get("name") or ""), login=str(item.get("login") or ""),
        display_name=str(item.get("display_name") or ""), email=str(item.get("email") or ""),
        first_name=str(item.get("first_name") or ""), last_name=str(item.get("last_name") or ""))


@dataclass(frozen=True)
class Resolution:
    status: str                          # unique | none | ambiguous
    id: str | None = None
    tier: str | None = None              # email | login | name
    person: Person | None = None
    candidates: tuple[Person, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == "unique"


def resolve(people: list[Person], name: str, email: str | None = None) -> Resolution:
    """名前（と email）に完全一致する人を探す。1 人に定まったときだけ unique。"""
    wanted_name, wanted_email = normalize(name), normalize(email)
    tiers: list[tuple[str, Callable[[Person], bool]]] = []
    if wanted_email:
        tiers.append(("email", lambda p: normalize(p.email) == wanted_email))
    if wanted_name:
        tiers.append(("login", lambda p: wanted_name in {normalize(p.login),
                                                          normalize(p.display_name)}))
        tiers.append(("name", lambda p: wanted_name in p.full_names()))
    for tier, matches in tiers:
        found = {p.id: p for p in people if matches(p)}
        if len(found) == 1:
            (person,) = found.values()
            return Resolution("unique", person.id, tier, person, (person,))
        if len(found) > 1:
            return Resolution("ambiguous", None, tier, None, tuple(found.values()))
    return Resolution("none")


class AssigneeResolver:
    """アダプタの担当候補の一覧を（短い時間だけ）持ち、メンバーを引き当てる。"""

    def __init__(self, adapters: AdapterRegistry, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.adapters = adapters
        self.clock = clock
        self._cache: dict[str, tuple[float, list[Person]]] = {}

    def can_resolve(self, tracker: str) -> bool:
        return (self.adapters.has(tracker)
                and LIST_ACTION in self.adapters.get(tracker).actions())

    def people(self, tracker: str) -> list[Person]:
        held = self._cache.get(tracker)
        if held is not None and self.clock() - held[0] < CACHE_SECONDS:
            return held[1]
        adapter = self.adapters.get(tracker)
        if adapter.writes(LIST_ACTION):                  # 読み取りだけを呼ぶ門番
            raise AdapterError(f"{tracker}.{LIST_ACTION} は書き込みを行います "
                               f"/ {tracker}.{LIST_ACTION} writes")
        result = adapter.invoke(LIST_ACTION, {})
        items = result.get("items") if isinstance(result, dict) else result
        people = [p for p in (person_of(i) for i in (items or []) if isinstance(i, dict)) if p]
        self._cache[tracker] = (self.clock(), people)
        return people

    def resolve(self, tracker: str, name: str, email: str | None = None) -> Resolution:
        return resolve(self.people(tracker), name, email)
