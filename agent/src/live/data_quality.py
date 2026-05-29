"""Pre-trade data quality validation for A-share market data.

Checks OHLC validity, data freshness, price continuity, and volume anomalies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .calendar import TradingCalendar, TradingSessionPhase
from .market_data import Quote


@dataclass(frozen=True)
class DataQualityResult:
    passed: bool
    check_name: str
    details: str
    severity: str  # "critical", "warning", "info"


class DataQualityChecker:
    """Validates market data quality before trading decisions."""

    def __init__(self, calendar: TradingCalendar | None = None):
        self._calendar = calendar or TradingCalendar()

    # ── Individual checks ──────────────────────────────────────

    def check_ohlc_validity(self, quote: Quote) -> DataQualityResult:
        """Verify OHLC consistency.

        Rules:
        - All prices must be positive
        - Low <= Open <= High
        - Low <= Close (last_price) <= High
        """
        prices = [quote.open, quote.high, quote.low, quote.last_price]
        if any(p <= 0 for p in prices):
            return DataQualityResult(False, "ohlc_validity",
                                     f"Negative or zero prices: O={quote.open} H={quote.high} L={quote.low} C={quote.last_price}",
                                     "critical")

        if quote.low > quote.high:
            return DataQualityResult(False, "ohlc_validity",
                                     f"Low ({quote.low}) > High ({quote.high})",
                                     "critical")

        if quote.open < quote.low or quote.open > quote.high:
            return DataQualityResult(False, "ohlc_validity",
                                     f"Open ({quote.open}) outside [L={quote.low}, H={quote.high}]",
                                     "warning")

        if quote.last_price < quote.low or quote.last_price > quote.high:
            return DataQualityResult(False, "ohlc_validity",
                                     f"Last ({quote.last_price}) outside [L={quote.low}, H={quote.high}]",
                                     "warning")

        return DataQualityResult(True, "ohlc_validity", "OHLC valid", "info")

    def check_freshness(self, symbol: str, quote: Quote,
                        max_age_seconds: int = 60) -> DataQualityResult:
        """Check that the quote is not stale.

        During market hours, quotes should be < max_age_seconds old.
        """
        now = datetime.now()
        age = (now - quote.timestamp).total_seconds()

        if not self._calendar.is_trading_day(now.date()):
            return DataQualityResult(True, "freshness",
                                     "Non-trading day, skipping freshness check", "info")

        phase = self._calendar.current_session_phase(now)
        if phase != TradingSessionPhase.CONTINUOUS_AUCTION:
            return DataQualityResult(True, "freshness",
                                     f"Market phase: {phase.value}, skipping freshness check", "info")

        if age > max_age_seconds:
            return DataQualityResult(False, "freshness",
                                     f"{symbol} quote is {age:.0f}s old (max {max_age_seconds}s)",
                                     "warning")
        return DataQualityResult(True, "freshness",
                                 f"{symbol} quote is {age:.0f}s old", "info")

    def check_price_continuity(self, symbol: str, quote: Quote,
                               prev_close: float | None = None) -> DataQualityResult:
        """Detect abnormal price gaps vs previous close.

        For A-shares:
        - Main board: > 10% gap is abnormal (outside limit)
        - Chinext/Star: > 20% gap is abnormal
        - Beijing: > 30% gap is abnormal
        """
        ref_close = prev_close or quote.pre_close
        if ref_close <= 0:
            return DataQualityResult(True, "price_continuity",
                                     "No previous close for comparison", "info")

        gap = abs(quote.last_price - ref_close) / ref_close

        # Determine expected max gap based on symbol prefix
        code = symbol.split(".")[0] if "." in symbol else symbol
        if code.startswith("8") and len(code) == 6:
            max_gap = 0.30  # Beijing
        elif code.startswith(("688", "300")):
            max_gap = 0.20  # Chinext/Star
        else:
            max_gap = 0.10  # Main board

        limit = max_gap * 1.5  # 50% buffer for new listings, etc.

        if gap > limit:
            return DataQualityResult(False, "price_continuity",
                                     f"{symbol}: gap {gap:.2%} exceeds limit {limit:.2%}",
                                     "warning")
        return DataQualityResult(True, "price_continuity",
                                 f"{symbol}: gap {gap:.2%} within limit", "info")

    def check_volume_anomaly(self, quote: Quote,
                             avg_volume: float | None = None) -> DataQualityResult:
        """Flag volume > 10x average as potential data error.

        A sudden 10x spike is more likely a data feed error than
        a genuine volume event (for most stocks).
        """
        if avg_volume is None or avg_volume <= 0:
            return DataQualityResult(True, "volume_anomaly",
                                     "No average volume for comparison", "info")

        if quote.volume <= 0:
            return DataQualityResult(False, "volume_anomaly",
                                     f"{quote.symbol}: zero volume reported", "warning")

        ratio = quote.volume / avg_volume
        if ratio > 10:
            return DataQualityResult(False, "volume_anomaly",
                                     f"{quote.symbol}: volume {ratio:.0f}x avg ({quote.volume} vs {avg_volume:.0f})",
                                     "warning")
        return DataQualityResult(True, "volume_anomaly",
                                 f"{quote.symbol}: volume {ratio:.1f}x avg", "info")

    # ── Batch check ────────────────────────────────────────────

    def run_all_checks(
        self,
        symbol: str,
        quote: Quote,
        prev_close: float | None = None,
        avg_volume: float | None = None,
        max_age_seconds: int = 60,
    ) -> list[DataQualityResult]:
        """Run all data quality checks for a quote."""
        results = [
            self.check_ohlc_validity(quote),
            self.check_freshness(symbol, quote, max_age_seconds),
            self.check_price_continuity(symbol, quote, prev_close),
            self.check_volume_anomaly(quote, avg_volume),
        ]
        return results

    def is_clean(self, quote: Quote, **kwargs) -> bool:
        """Quick check: returns True if no critical/warning issues."""
        results = self.run_all_checks(quote.symbol, quote, **kwargs)
        return all(r.passed or r.severity == "info" for r in results)
