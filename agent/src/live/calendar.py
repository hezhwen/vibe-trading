"""Trading calendar and session state machine for A-shares.

Uses exchange_calendars as primary source with AKShare fallback for holiday lists.
All times in Asia/Shanghai (UTC+8). No DST.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum

TZ_SHANGHAI = "Asia/Shanghai"

# Session time windows (Shanghai time)
MORNING_OPEN = time(9, 15)   # 集合竞价开始
MORNING_START = time(9, 30)  # 连续竞价开始
MORNING_END = time(11, 30)   # 上午收盘
AFTERNOON_START = time(13, 0)  # 下午开盘
AFTERNOON_END = time(15, 0)   # 下午收盘


class TradingSessionPhase(Enum):
    PRE_OPENING = "pre_opening"            # 09:15-09:25 集合竞价
    CONTINUOUS_AUCTION = "continuous"       # 09:30-11:30, 13:00-15:00 连续竞价
    LUNCH_BREAK = "lunch_break"            # 11:30-13:00 午休
    CLOSED = "closed"                      # 非交易时段/非交易日


class TradingCalendar:
    """A-share trading calendar with session phase awareness."""

    def __init__(self):
        self._holidays: set[date] | None = None
        self._calendar = None
        try:
            import exchange_calendars as xcals
            self._calendar = xcals.get_calendar("XSHG")
        except Exception:
            self._calendar = None

    # ── Holiday data (lazy, cached) ────────────────────────────

    def _load_holidays(self) -> set[date]:
        if self._holidays is not None:
            return self._holidays

        holidays: set[date] = set()

        # Primary: exchange_calendars
        if self._calendar is not None:
            try:
                # Get a wide range to build the holiday set
                import pandas as pd
                all_days = pd.date_range("2000-01-01", "2099-12-31", freq="D")
                trading_sessions = self._calendar.sessions
                trading_set = set(d.date() for d in trading_sessions if d >= pd.Timestamp("2000-01-01"))
                for d in all_days:
                    day = d.date()
                    if day.weekday() < 5 and day not in trading_set:
                        holidays.add(day)
            except Exception:
                pass

        # Fallback: simple weekday check (no holidays)
        if not holidays:
            self._holidays = holidays
            return holidays

        self._holidays = holidays
        return holidays

    # ── Public API ─────────────────────────────────────────────

    def is_trading_day(self, d: date) -> bool:
        """Check if date is a trading day."""
        if d.weekday() >= 5:  # Saturday/Sunday
            return False
        return d not in self._load_holidays()

    def next_trading_day(self, d: date, offset: int = 1) -> date:
        """Get the Nth next trading day (offset can be negative)."""
        step = 1 if offset > 0 else -1
        count = 0
        target = abs(offset)
        current = d
        while count < target:
            current += timedelta(days=step)
            if self.is_trading_day(current):
                count += 1
        return current

    def trading_days_between(self, start: date, end: date) -> list[date]:
        """List all trading days in [start, end]."""
        days = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    @staticmethod
    def current_session_phase(now: datetime | None = None) -> TradingSessionPhase:
        """Determine the current trading session phase."""
        from datetime import timezone, timedelta as td

        if now is None:
            from datetime import datetime as dt
            tz = timezone(td(hours=8))
            now = dt.now(tz)

        t = now.time()

        if t < MORNING_OPEN:
            return TradingSessionPhase.CLOSED
        if t < MORNING_START:
            return TradingSessionPhase.PRE_OPENING
        if t < MORNING_END:
            return TradingSessionPhase.CONTINUOUS_AUCTION
        if t < AFTERNOON_START:
            return TradingSessionPhase.LUNCH_BREAK
        if t < AFTERNOON_END:
            return TradingSessionPhase.CONTINUOUS_AUCTION
        return TradingSessionPhase.CLOSED

    def is_market_open(self, now: datetime | None = None) -> bool:
        """Check if the market is currently in continuous auction."""
        if now is None:
            from datetime import datetime as dt, timezone, timedelta as td
            tz = timezone(td(hours=8))
            now = dt.now(tz)
        if not self.is_trading_day(now.date()):
            return False
        phase = self.current_session_phase(now)
        return phase == TradingSessionPhase.CONTINUOUS_AUCTION

    def time_to_next_phase(self, now: datetime | None = None) -> timedelta:
        """Time until the next session phase boundary."""
        from datetime import timezone, timedelta as td

        if now is None:
            from datetime import datetime as dt
            tz = timezone(td(hours=8))
            now = dt.now(tz)

        t = now.time()
        today = now.date()

        # If not a trading day, find next trading day's pre-opening
        if not self.is_trading_day(today):
            next_td = self.next_trading_day(today)
            next_dt = datetime.combine(next_td, MORNING_OPEN, tzinfo=now.tzinfo)
            return next_dt - now

        # Boundaries in order
        boundaries = [
            MORNING_OPEN,
            MORNING_START,
            MORNING_END,
            AFTERNOON_START,
            AFTERNOON_END,
        ]
        for b in boundaries:
            if t < b:
                dt_boundary = datetime.combine(today, b, tzinfo=now.tzinfo)
                return dt_boundary - now

        # After market close → next trading day morning
        next_td = self.next_trading_day(today)
        next_dt = datetime.combine(next_td, MORNING_OPEN, tzinfo=now.tzinfo)
        return next_dt - now

    def session_time_windows(self, d: date) -> list[tuple[time, time]]:
        """Return trading session windows for a given date."""
        if not self.is_trading_day(d):
            return []
        return [
            (MORNING_START, MORNING_END),
            (AFTERNOON_START, AFTERNOON_END),
        ]
