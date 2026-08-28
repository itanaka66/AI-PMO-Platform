"""cron 式の解釈 / cron expression handling.

外部ライブラリに依存させていない。導入の簡単さを優先しているので、
定時起動のためだけに依存を1つ増やしたくない。
その代わり、境界の振る舞いはテストで固めてある。

No external dependency: ease of installation is a priority here, and adding one
just to fire jobs on time is a poor trade. The boundary behaviour is pinned
down by tests instead.

対応する記法 / Supported syntax
-------------------------------
    分 時 日 月 曜日
    0  9  *  *  MON-FRI

    *        すべて / any
    5        単一の値 / a single value
    1,3,5    列挙 / a list
    1-5      範囲 / a range
    */15     間隔 / a step
    MON-FRI  曜日名 / weekday names
    JAN,DEC  月名 / month names

日と曜日の両方を指定したときは OR / Day-of-month and day-of-week are ORed
-------------------------------------------------------------------------
`0 0 1 * MON` は「毎月1日**または**毎週月曜」です。AND ではありません。
これは Vixie cron からの振る舞いで、直感に反するのでテストで明示してあります。

`0 0 1 * MON` fires on the 1st of the month **or** on Mondays, not on Mondays
that fall on the 1st. This comes from Vixie cron; it surprises people, so the
tests state it explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

WEEKDAYS = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
}
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# 探索の上限。これを超えて見つからない式は、事実上「起動しない」。
# 2月30日のような、決して来ない指定を無限に探さないための歯止め。
# A ceiling on the search: an expression that finds nothing within a year
# effectively never fires. This stops a date like 30 February from being
# searched for forever.
# うるう日 (2月29日) は4年に一度しか来ない。1年で打ち切ると、
# 設定できるのに永久に起動しないスケジュールができてしまう。
# A leap day comes round every four years. Cutting the search off at one year
# would allow a schedule that can be written but never fires.
MAX_LOOKAHEAD_DAYS = 366 * 5


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool
    expression: str

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False

        # Python の weekday() は月曜が 0。cron は日曜が 0。
        # Python counts Monday as 0; cron counts Sunday as 0.
        weekday = (moment.weekday() + 1) % 7

        if self.day_restricted and self.weekday_restricted:
            return moment.day in self.days or weekday in self.weekdays
        if self.day_restricted:
            return moment.day in self.days
        if self.weekday_restricted:
            return weekday in self.weekdays
        return True


def parse(expression: str) -> Cron:
    fields = expression.split()
    if len(fields) != 5:
        raise CronError(
            f"cron は5つの項目が必要です / a cron expression needs five fields "
            f"(分 時 日 月 曜日): {expression!r}"
        )

    minute, hour, day, month, weekday = fields
    return Cron(
        minutes=_field(minute, 0, 59, {}, "分 / minute"),
        hours=_field(hour, 0, 23, {}, "時 / hour"),
        days=_field(day, 1, 31, {}, "日 / day"),
        months=_field(month, 1, 12, MONTHS, "月 / month"),
        weekdays=_normalise_weekdays(_field(weekday, 0, 7, WEEKDAYS, "曜日 / weekday")),
        day_restricted=day.strip() != "*",
        weekday_restricted=weekday.strip() != "*",
        expression=expression,
    )


def _normalise_weekdays(values: frozenset[int]) -> frozenset[int]:
    """cron では 0 も 7 も日曜 / both 0 and 7 mean Sunday."""
    return frozenset(0 if value == 7 else value for value in values)


def _field(raw: str, low: int, high: int, names: dict[str, int],
           label: str) -> frozenset[int]:
    raw = raw.strip().upper()
    values: set[int] = set()

    for part in raw.split(","):
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise CronError(f"{label}: 間隔が数値ではありません / step is not a number: {step_text!r}") from None
            if step < 1:
                raise CronError(f"{label}: 間隔は1以上にしてください / step must be at least 1")

        if part in ("*", ""):
            start, end = low, high
        elif "-" in part[1:] or (part.startswith("-") is False and "-" in part):
            start_text, _, end_text = part.partition("-")
            start = _value(start_text, names, low, high, label)
            end = _value(end_text, names, low, high, label)
            if start > end:
                raise CronError(
                    f"{label}: 範囲が逆です / range runs backwards: {part!r}"
                )
        else:
            start = end = _value(part, names, low, high, label)

        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"{label}: 値がありません / no values: {raw!r}")
    return frozenset(values)


def _value(text: str, names: dict[str, int], low: int, high: int,
           label: str) -> int:
    text = text.strip()
    if text in names:
        return names[text]
    try:
        number = int(text)
    except ValueError:
        allowed = f" 使える名前 / names: {', '.join(sorted(names))}" if names else ""
        raise CronError(f"{label}: 解釈できません / cannot parse {text!r}.{allowed}") from None
    if not low <= number <= high:
        raise CronError(
            f"{label}: {low}〜{high} の範囲外です / outside {low}-{high}: {number}"
        )
    return number


def next_fire(cron: Cron, after: datetime, tz) -> datetime | None:
    """次に起動する時刻を返す（UTC）。

    探索は UTC で1分ずつ進め、判定だけを現地時刻で行う。
    現地時刻のまま進めると、夏時間の切り替えで
    存在しない時刻や重複する時刻を跨いだときに壊れる。

    Stepping happens in UTC and only the comparison is done in local time.
    Stepping in local time breaks across a daylight-saving transition, where a
    wall-clock time may not exist at all or may occur twice.

    夏時間で飛ばされた時刻は、その日は起動しない。
    重複する時刻は、同じ分を二度実行しない仕組み（スケジューラ側の記録）で
    抑える。ここで判断すると、どちらの1時間を正とするか決められない。

    A wall-clock time skipped by a spring-forward simply does not fire that
    day. A time that occurs twice in an autumn fallback is suppressed by the
    scheduler's record of what has already run, because this function has no
    basis for choosing which of the two hours is the real one.
    """
    moment = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
    moment += timedelta(minutes=1)

    limit = moment + timedelta(days=MAX_LOOKAHEAD_DAYS)
    while moment < limit:
        local = moment.astimezone(tz)

        if cron.matches(local):
            return moment

        # 日付が合わない日は、1分ずつ辿らずその日を飛ばす。
        # 4年先まで探すことがあるので、1分刻みのままでは数百万回になる。
        #
        # 飛び先は「現地時刻の翌日 0 時」を UTC に直したもの。UTC の時刻境界に
        # 揃えると、UTC との差が 30 分や 45 分の地域 (インド、ネパール) で
        # 現地 0 時台を飛び越してしまう。
        #
        # Days whose date cannot match are skipped whole: the search may run
        # four years ahead, which minute-by-minute would be millions of steps.
        # The jump targets local midnight converted to UTC — aligning to a UTC
        # hour boundary would skip the local midnight hour in zones offset by
        # 30 or 45 minutes (India, Nepal).
        if not _date_matches(cron, local):
            next_local_midnight = datetime.combine(
                (local + timedelta(days=1)).date(), time.min, tzinfo=tz)
            # 必ず前へ進める。夏時間で存在しない現地 0 時に当たっても止まらない。
            # Always moves forward, even if that local midnight does not exist.
            moment = max(next_local_midnight.astimezone(timezone.utc),
                         moment + timedelta(minutes=1))
            continue

        moment += timedelta(minutes=1)

    return None


def _date_matches(cron: Cron, local: datetime) -> bool:
    """時刻を無視して、日付だけで合否を見る / date only, ignoring the clock."""
    if local.month not in cron.months:
        return False

    weekday = (local.weekday() + 1) % 7
    if cron.day_restricted and cron.weekday_restricted:
        return local.day in cron.days or weekday in cron.weekdays
    if cron.day_restricted:
        return local.day in cron.days
    if cron.weekday_restricted:
        return weekday in cron.weekdays
    return True
