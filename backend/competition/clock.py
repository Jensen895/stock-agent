"""When a competition day happens: the closing bell, not the opening one.

The app refreshes at 9:30 ET because a suggestion is worth most at the start of
the session you might act on it in. This harness is the mirror image — it
reports on a day that has finished, so its trigger is 16:00 ET plus a few
minutes for the last prints to settle.

Holidays are ignored, exactly as they are in ``ai_advisor``'s market clock and
for the same reason: on a closed Monday the run scores against unchanged prices
and books no trades worth speaking of, which costs a line in the ledger and
nothing else. Adding a holiday calendar would mean shipping and maintaining one
to avoid an outcome that is already harmless.

Everything here is keyed off a **session date** — the ``YYYY-MM-DD`` of a
trading day in Eastern time. That string is the competition's primary key: the
ledger is unique on it, which is what makes running the day twice a no-op
rather than a double set of trades.
"""

from datetime import datetime, timedelta, timezone

try:  # stdlib on Python 3.9+, used only for US market-hours math
    from zoneinfo import ZoneInfo

    _EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tz data is unavailable
    _EASTERN = None

# 16:00 ET. Without tz data we can't know when that is, so fall back to 21:00
# UTC — exactly the close in summer (EDT) and an hour after it in winter (EST).
# Late rather than early is the safe direction: firing before the bell would
# mark the book at an intraday price and call it a close.
_CLOSE_HOUR, _CLOSE_MINUTE = 16, 0
_FALLBACK_CLOSE_HOUR, _FALLBACK_CLOSE_MINUTE = 21, 0

# How long after the bell to wait before running. Yahoo's last print lags the
# close by a minute or two, and a mark taken at 16:00:00 sharp can still be the
# 15:59 quote. Five minutes costs nothing and removes the question.
SETTLE_MINUTES = 5

# A weekend is at most two days, so five days either way always spans a
# weekday.
_SEARCH_DAYS = 5


def _local(now_utc: datetime) -> datetime:
    """``now_utc`` in market-local terms — Eastern when tz data is available,
    UTC as the documented approximation when it isn't."""
    return now_utc.astimezone(_EASTERN) if _EASTERN is not None else now_utc


def _close_on(local_day: datetime) -> datetime:
    """The UTC instant the market closes on ``local_day``'s calendar date."""
    hour, minute = (
        (_CLOSE_HOUR, _CLOSE_MINUTE)
        if _EASTERN is not None
        else (_FALLBACK_CLOSE_HOUR, _FALLBACK_CLOSE_MINUTE)
    )
    return local_day.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def market_date(when: datetime = None) -> str:
    """The Eastern calendar date of ``when``, as ``YYYY-MM-DD``."""
    return _local(when or now_utc()).strftime("%Y-%m-%d")


def is_trading_day(when: datetime = None) -> bool:
    """True on Mon-Fri in Eastern time. Holidays are ignored — see the module
    note."""
    return _local(when or now_utc()).weekday() < 5


def last_close(when: datetime = None, settle: int = SETTLE_MINUTES):
    """The most recent closing bell (plus ``settle``) at or before ``when``.

    Returns a UTC datetime, or None only if no weekday could be found within
    the search window — which is impossible with tz data present.
    """
    when = when or now_utc()
    local = _local(when)
    for back in range(_SEARCH_DAYS):
        day = local - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        closed = _close_on(day) + timedelta(minutes=settle)
        if closed <= when:
            return closed
    return None


def next_close(when: datetime = None, settle: int = SETTLE_MINUTES):
    """The next closing bell (plus ``settle``) strictly after ``when``, in UTC."""
    when = when or now_utc()
    local = _local(when)
    for ahead in range(_SEARCH_DAYS):
        day = local + timedelta(days=ahead)
        if day.weekday() >= 5:
            continue
        closes = _close_on(day) + timedelta(minutes=settle)
        if closes > when:
            return closes
    return None


def session_date(when: datetime = None, settle: int = SETTLE_MINUTES):
    """The trading date this moment belongs to, or None before the first close.

    At 17:00 ET on a Wednesday this is Wednesday. At 09:00 on the Thursday it is
    still Wednesday, because Thursday's session hasn't finished. All weekend it
    is Friday.

    This is what makes the harness catch up rather than skip: a laptop that was
    asleep at 16:05 on Wednesday and wakes on Thursday morning asks for the
    session date, gets Wednesday, finds no Wednesday row in the ledger and runs
    it — against Wednesday's closing prices, which is exactly what it would
    have used at the time. Wake it up on Friday instead and Thursday is simply
    lost; the harness does not walk backwards through days it missed, because
    the reasoning behind those trades would have to be invented after the fact.
    """
    closed = last_close(when, settle)
    return market_date(closed) if closed else None


def seconds_until_next_close(when: datetime = None,
                             settle: int = SETTLE_MINUTES) -> float:
    """How long to sleep before the next run is due. Never negative."""
    when = when or now_utc()
    upcoming = next_close(when, settle)
    if upcoming is None:  # pragma: no cover - needs tz data to be missing
        return 3600.0
    return max(0.0, (upcoming - when).total_seconds())
