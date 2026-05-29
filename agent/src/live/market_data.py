"""Market data bus for real-time A-share quotes.

Defines the Quote dataclass and abstract MarketDataProvider interface.

Three concrete providers:
  - MootdxTencentProvider (primary): mootdx TCP (fast, no IP ban) for
    OHLCV + bid/ask + Tencent HTTP for PE/PB/turnover/market cap
  - TencentOnlyProvider (fallback): Tencent HTTP only, no mootdx required
  - AKShareMarketDataProvider (legacy): polls stock_zh_a_spot_em()
"""

from __future__ import annotations

import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import pandas as pd

from .calendar import TradingCalendar


@dataclass(frozen=True)
class Quote:
    """Real-time market quote for a single A-share symbol.

    Provides all fields needed by ChinaAEngineV2:
    - can_execute: pct_chg, pre_close (for limit check)
    - can_fill: amount, pct_chg (for seal strength)
    - apply_slippage: amount (for ADV estimation)
    """
    symbol: str
    timestamp: datetime
    name: str = ""
    last_price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0
    volume: int = 0          # shares
    amount: float = 0.0      # CNY
    pct_chg: float = 0.0     # decimal (0.05 = 5%)
    bid_prices: tuple[float, ...] = ()
    bid_volumes: tuple[int, ...] = ()
    ask_prices: tuple[float, ...] = ()
    ask_volumes: tuple[int, ...] = ()
    turnover_rate: float = 0.0  # percentage (3.5 = 3.5%)
    pe_ttm: float = 0.0         # PE (TTM), from Tencent
    pb: float = 0.0             # PB, from Tencent
    market_cap: float = 0.0     # total market cap in CNY (yi), from Tencent
    limit_up: float = 0.0       # limit-up price, from Tencent
    limit_down: float = 0.0     # limit-down price, from Tencent

    @property
    def limit_up_price(self) -> float:
        """Limit-up price: use Tencent value if available, fall back to
        calculated 10%."""
        if self.limit_up > 0:
            return self.limit_up
        return round(self.pre_close * 1.10, 2)

    @property
    def limit_down_price(self) -> float:
        """Limit-down price: use Tencent value if available, fall back to
        calculated 10%."""
        if self.limit_down > 0:
            return self.limit_down
        return round(self.pre_close * 0.90, 2)

    def to_bar_series(self) -> pd.Series:
        """Convert to a bar-like pandas Series for V2 engine consumption."""
        return pd.Series({
            "open": self.open,
            "close": self.last_price,
            "high": self.high,
            "low": self.low,
            "pre_close": self.pre_close,
            "amount": self.amount,
            "volume": self.volume,
            "pct_chg": self.pct_chg * 100.0,  # V2 engine expects percentage form
        })


class MarketDataProvider(ABC):
    """Abstract market data interface.

    Concrete implementations: AKShareMarketDataProvider (polling),
    future: EastMoney WebSocket, QMT native quotes, etc.
    """

    @abstractmethod
    def get_realtime_quote(self, symbol: str) -> Quote | None:
        """Get a single real-time quote."""
        ...

    @abstractmethod
    def get_batch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for multiple symbols at once."""
        ...

    @abstractmethod
    def subscribe(
        self, symbols: list[str], callback: Callable[[Quote], None]
    ) -> None:
        """Subscribe to quote updates for the given symbols."""
        ...

    @abstractmethod
    def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from quote updates."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Clean up resources (connections, schedulers)."""
        ...


# ── Tencent Finance helpers ──────────────────────────────────

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _code_to_prefix(code: str) -> str:
    """6-digit code → market prefix."""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


def _fetch_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """Batch-fetch Tencent Finance real-time quotes.

    Returns dict[code, fields] with PE/PB/market cap/turnover rate
    and limit prices.  Also supports indices and ETFs.
    """
    if not codes:
        return {}
    prefixed = [f"{_code_to_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception:
        return {}

    result: dict[str, dict] = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        try:
            result[code] = {
                "name": vals[1],
                "price": float(vals[3]) if vals[3] else 0.0,
                "last_close": float(vals[4]) if vals[4] else 0.0,
                "open": float(vals[5]) if vals[5] else 0.0,
                "change_pct": float(vals[32]) if vals[32] else 0.0,
                "high": float(vals[33]) if vals[33] else 0.0,
                "low": float(vals[34]) if vals[34] else 0.0,
                "amount_wan": float(vals[37]) if vals[37] else 0.0,
                "turnover_pct": float(vals[38]) if vals[38] else 0.0,
                "pe_ttm": float(vals[39]) if vals[39] else 0.0,
                "mcap_yi": float(vals[44]) if vals[44] else 0.0,
                "pb": float(vals[46]) if vals[46] else 0.0,
                "limit_up": float(vals[47]) if vals[47] else 0.0,
                "limit_down": float(vals[48]) if vals[48] else 0.0,
            }
        except (ValueError, IndexError):
            continue
    return result


# ── mootdx helpers ────────────────────────────────────────────


def _create_mootdx_client():
    """Create a mootdx Quotes client (standard market)."""
    from mootdx.quotes import Quotes
    return Quotes.factory(market="std")


def _mootdx_batch_quotes(symbols: list[str]) -> dict[str, dict]:
    """Fetch quotes for a batch of symbols via mootdx TCP.

    Returns dict[code, fields] with OHLCV + bid/ask (5 levels).
    """
    if not symbols:
        return {}
    try:
        client = _create_mootdx_client()
        raw = client.quotes(symbol=symbols)
    except Exception:
        return {}

    result: dict[str, dict] = {}
    if raw is None:
        return result

    # mootdx returns a DataFrame; iterate rows
    for _, row in raw.iterrows() if hasattr(raw, "iterrows") else []:
        try:
            code = str(row.get("code", "") or row.get("market", ""))
            if not code:
                continue
            # Normalize code — mootdx may return with market prefix
            if len(code) > 6:
                code = code[-6:] if code[-6:].isdigit() else code

            bid_prices = []
            bid_volumes = []
            ask_prices = []
            ask_volumes = []
            for i in range(1, 6):
                bp = row.get(f"bid{i}")
                bv = row.get(f"bid_vol{i}")
                ap = row.get(f"ask{i}")
                av = row.get(f"ask_vol{i}")
                if bp and float(bp) > 0:
                    bid_prices.append(float(bp))
                    bid_volumes.append(int(bv) if bv else 0)
                if ap and float(ap) > 0:
                    ask_prices.append(float(ap))
                    ask_volumes.append(int(av) if av else 0)

            result[code] = {
                "name": str(row.get("name", "")),
                "price": float(row.get("price", 0) or 0),
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "last_close": float(row.get("last_close", 0) or 0),
                "vol": int(row.get("vol", 0) or 0),
                "amount": float(row.get("amount", 0) or 0),
                "bid_prices": tuple(bid_prices),
                "bid_volumes": tuple(bid_volumes),
                "ask_prices": tuple(ask_prices),
                "ask_volumes": tuple(ask_volumes),
            }
        except (ValueError, KeyError):
            continue
    return result


# ── Concrete providers ────────────────────────────────────────


class MootdxTencentProvider(MarketDataProvider):
    """Primary market data provider: mootdx TCP + Tencent HTTP.

    Uses mootdx for fast OHLCV + bid/ask (5 levels) via TCP 7709,
    and Tencent Finance for PE/PB/market cap/turnover rate/limit prices.

    Falls back to Tencent-only if mootdx is unavailable.
    Requires optional dependency: ``pip install mootdx``
    """

    def __init__(self, poll_interval: float = 3.0):
        import logging
        self._logger = logging.getLogger(__name__)
        self._poll_interval = max(poll_interval, 1.0)
        self._callbacks: dict[str, list[Callable]] = {}
        self._scheduler = None
        self._cache: dict[str, Quote] = {}
        self._running = False
        self._mootdx_available = True

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler(daemon=True)
            self._scheduler.add_job(
                self._poll, "interval",
                seconds=self._poll_interval,
                id="mootdx_poll",
            )
            self._scheduler.start()
            self._running = True
        except ImportError:
            self._logger.warning("apscheduler not available; manual poll only")

    def stop(self) -> None:
        self._running = False
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def close(self) -> None:
        self.stop()

    # ── Public API ─────────────────────────────────────────────

    def get_realtime_quote(self, symbol: str) -> Quote | None:
        if symbol in self._cache:
            return self._cache[symbol]
        quotes = self._fetch_batch([symbol])
        return quotes.get(symbol)

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return self._fetch_batch(symbols)

    def subscribe(
        self, symbols: list[str], callback: Callable[[Quote], None]
    ) -> None:
        for sym in symbols:
            self._callbacks.setdefault(sym, []).append(callback)

    def unsubscribe(self, symbols: list[str]) -> None:
        for sym in symbols:
            self._callbacks.pop(sym, None)

    # ── Internal ───────────────────────────────────────────────

    def _poll(self) -> None:
        if not self._callbacks:
            return
        try:
            symbols = list(self._callbacks)
            quotes = self._fetch_batch(symbols)
            self._cache.update(quotes)
            for sym, callbacks in list(self._callbacks.items()):
                quote = quotes.get(sym)
                if quote is not None:
                    for cb in callbacks:
                        try:
                            cb(quote)
                        except Exception:
                            pass
        except Exception:
            pass

    def _fetch_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch quotes via mootdx + Tencent, merged."""
        now = datetime.now()

        # Tier 1: mootdx (OHLCV + bid/ask)
        mootdx_data: dict[str, dict] = {}
        if self._mootdx_available:
            mootdx_data = _mootdx_batch_quotes(symbols)
            if not mootdx_data:
                self._mootdx_available = False

        # Tier 2: Tencent (PE/PB/turnover/limit + fallback OHLCV)
        tencent_data = _fetch_tencent_quotes(symbols)

        # Merge
        result: dict[str, Quote] = {}
        for code in symbols:
            md = mootdx_data.get(code, {})
            td = tencent_data.get(code, {})

            # Prefer mootdx for price fields, fall back to Tencent
            price = md.get("price") or td.get("price", 0.0)
            open_ = md.get("open") or td.get("open", 0.0)
            high = md.get("high") or td.get("high", 0.0)
            low = md.get("low") or td.get("low", 0.0)
            pre_close = md.get("last_close") or td.get("last_close", 0.0)

            # Volume/amount: mootdx only (Tencent amount is in wan)
            volume = md.get("vol", 0)
            amount = md.get("amount", 0.0)
            if amount == 0 and td.get("amount_wan"):
                amount = td["amount_wan"] * 10000

            # pct_chg: calculate from prices, or use Tencent's value
            pct_chg = 0.0
            if pre_close > 0 and price > 0:
                pct_chg = (price - pre_close) / pre_close
            elif td.get("change_pct"):
                pct_chg = td["change_pct"] / 100.0

            name = md.get("name") or td.get("name", "")
            turnover = td.get("turnover_pct", 0.0)
            pe_ttm = td.get("pe_ttm", 0.0)
            pb = td.get("pb", 0.0)
            mcap = td.get("mcap_yi", 0.0)
            limit_up = td.get("limit_up", 0.0)
            limit_down = td.get("limit_down", 0.0)

            bid_p = md.get("bid_prices", ())
            bid_v = md.get("bid_volumes", ())
            ask_p = md.get("ask_prices", ())
            ask_v = md.get("ask_volumes", ())

            quote = Quote(
                symbol=code,
                timestamp=now,
                name=name,
                last_price=price,
                open=open_,
                high=high,
                low=low,
                pre_close=pre_close,
                volume=volume,
                amount=amount,
                pct_chg=pct_chg,
                bid_prices=bid_p,
                bid_volumes=bid_v,
                ask_prices=ask_p,
                ask_volumes=ask_v,
                turnover_rate=turnover,
                pe_ttm=pe_ttm,
                pb=pb,
                market_cap=mcap,
                limit_up=limit_up,
                limit_down=limit_down,
            )
            result[code] = quote
        return result


class TencentOnlyProvider(MarketDataProvider):
    """Tencent Finance provider — HTTP only, no mootdx required.

    Lightweight alternative when TCP access to TDX servers isn't available
    (e.g. overseas servers, restricted networks).
    """

    def __init__(self, poll_interval: float = 3.0):
        import logging
        self._logger = logging.getLogger(__name__)
        self._poll_interval = max(poll_interval, 1.0)
        self._callbacks: dict[str, list[Callable]] = {}
        self._scheduler = None
        self._cache: dict[str, Quote] = {}
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler(daemon=True)
            self._scheduler.add_job(
                self._poll, "interval",
                seconds=self._poll_interval,
                id="tencent_poll",
            )
            self._scheduler.start()
            self._running = True
        except ImportError:
            self._logger.warning("apscheduler not available; manual poll only")

    def stop(self) -> None:
        self._running = False
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def close(self) -> None:
        self.stop()

    def get_realtime_quote(self, symbol: str) -> Quote | None:
        quotes = self._fetch_batch([symbol])
        return quotes.get(symbol)

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return self._fetch_batch(symbols)

    def subscribe(
        self, symbols: list[str], callback: Callable[[Quote], None]
    ) -> None:
        for sym in symbols:
            self._callbacks.setdefault(sym, []).append(callback)

    def unsubscribe(self, symbols: list[str]) -> None:
        for sym in symbols:
            self._callbacks.pop(sym, None)

    def _poll(self) -> None:
        if not self._callbacks:
            return
        try:
            symbols = list(self._callbacks)
            quotes = self._fetch_batch(symbols)
            self._cache.update(quotes)
            for sym, callbacks in list(self._callbacks.items()):
                quote = quotes.get(sym)
                if quote is not None:
                    for cb in callbacks:
                        try:
                            cb(quote)
                        except Exception:
                            pass
        except Exception:
            pass

    def _fetch_batch(self, symbols: list[str]) -> dict[str, Quote]:
        now = datetime.now()
        tencent_data = _fetch_tencent_quotes(symbols)
        result: dict[str, Quote] = {}
        for code in symbols:
            td = tencent_data.get(code)
            if td is None:
                continue
            price = td.get("price", 0.0)
            pre_close = td.get("last_close", 0.0)
            pct_chg = td.get("change_pct", 0.0) / 100.0 if td.get("change_pct") else 0.0
            result[code] = Quote(
                symbol=code,
                timestamp=now,
                name=td.get("name", ""),
                last_price=price,
                open=td.get("open", 0.0),
                high=td.get("high", 0.0),
                low=td.get("low", 0.0),
                pre_close=pre_close,
                volume=0,  # Tencent doesn't provide share volume
                amount=td.get("amount_wan", 0.0) * 10000,
                pct_chg=pct_chg,
                turnover_rate=td.get("turnover_pct", 0.0),
                pe_ttm=td.get("pe_ttm", 0.0),
                pb=td.get("pb", 0.0),
                market_cap=td.get("mcap_yi", 0.0),
                limit_up=td.get("limit_up", 0.0),
                limit_down=td.get("limit_down", 0.0),
            )
        return result


class AKShareMarketDataProvider(MarketDataProvider):
    """AKShare-based market data provider — legacy, kept as fallback.

    Uses stock_zh_a_spot_em() which returns all A-share spot quotes in
    one call.  Slower and less stable than mootdx+Tencent.
    """

    def __init__(self, poll_interval: float = 3.0):
        import logging
        self._logger = logging.getLogger(__name__)
        self._poll_interval = max(poll_interval, 1.0)
        self._callbacks: dict[str, list[Callable]] = {}
        self._scheduler = None
        self._cache: dict[str, Quote] = {}
        self._calendar = TradingCalendar()
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler(daemon=True)
            self._scheduler.add_job(
                self._poll, "interval",
                seconds=self._poll_interval,
                id="market_data_poll",
            )
            self._scheduler.start()
            self._running = True
        except ImportError:
            self._logger.warning("apscheduler not available; manual poll only")

    def stop(self) -> None:
        self._running = False
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def close(self) -> None:
        self.stop()

    def get_realtime_quote(self, symbol: str) -> Quote | None:
        if symbol in self._cache:
            return self._cache[symbol]
        quotes = self._fetch_all()
        return quotes.get(symbol)

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        all_quotes = self._fetch_all()
        return {s: all_quotes[s] for s in symbols if s in all_quotes}

    def subscribe(
        self, symbols: list[str], callback: Callable[[Quote], None]
    ) -> None:
        for sym in symbols:
            if sym not in self._callbacks:
                self._callbacks[sym] = []
            self._callbacks[sym].append(callback)

    def unsubscribe(self, symbols: list[str]) -> None:
        for sym in symbols:
            self._callbacks.pop(sym, None)

    def _poll(self) -> None:
        if not self._callbacks:
            return
        try:
            quotes = self._fetch_all()
            self._cache = quotes
            for sym, callbacks in list(self._callbacks.items()):
                quote = quotes.get(sym)
                if quote is not None:
                    for cb in callbacks:
                        try:
                            cb(quote)
                        except Exception:
                            pass
        except Exception:
            pass

    def _fetch_all(self) -> dict[str, Quote]:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            result: dict[str, Quote] = {}
            now = datetime.now()
            for _, row in df.iterrows():
                try:
                    code = str(row.get("代码", ""))
                    if not code:
                        continue
                    quote = Quote(
                        symbol=code,
                        timestamp=now,
                        name=str(row.get("名称", "")),
                        last_price=float(row.get("最新价", 0) or 0),
                        open=float(row.get("今开", 0) or 0),
                        high=float(row.get("最高", 0) or 0),
                        low=float(row.get("最低", 0) or 0),
                        pre_close=float(row.get("昨收", 0) or 0),
                        volume=int(row.get("成交量", 0) or 0),
                        amount=float(row.get("成交额", 0) or 0),
                        pct_chg=float(row.get("涨跌幅", 0) or 0) / 100.0,
                        turnover_rate=float(row.get("换手率", 0) or 0),
                    )
                    result[code] = quote
                except (ValueError, KeyError):
                    continue
            return result
        except ImportError:
            return {}
        except Exception:
            return {}
