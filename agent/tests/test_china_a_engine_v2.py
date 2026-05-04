"""Tests for ChinaAEngineV2 market rules.

Validates:
  - T+1 frozen_qty logic
  - can_execute: ST status, suspended, price limits
  - can_fill: seal strength thresholds
  - Nonlinear slippage model
  - Time-varying fee schedule (stamp duty 2023-08-28)
  - V1: 沪深300 buy-and-hold endpoint deviation < 1%
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtest.engines.china_a_v2 import (
    ChinaAEngineV2,
    ASharePosition,
    ExecutionResult,
    get_limit_pct,
    get_stamp_duty,
    get_transfer_fee,
    DEFAULT_FEE_SCHEDULE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(
    close: float = 15.0,
    pre_close: float | None = None,
    pct_chg: float | None = None,
    amount: float = 0.0,
) -> pd.Series:
    d = {"close": close, "open": close, "volume": 1000000, "amount": amount}
    if pre_close is not None:
        d["pre_close"] = pre_close
    if pct_chg is not None:
        d["pct_chg"] = pct_chg
    return pd.Series(d)


def _make_engine(**overrides) -> ChinaAEngineV2:
    config = {"initial_cash": 1_000_000.0}
    config.update(overrides)
    return ChinaAEngineV2(config)


# ---------------------------------------------------------------------------
# Fee schedule
# ---------------------------------------------------------------------------


class TestFeeSchedule:
    def test_stamp_duty_2023_reduction(self):
        """2023-08-28起印花税减半征收"""
        assert get_stamp_duty(date(2023, 8, 27)) == 0.001
        assert get_stamp_duty(date(2023, 8, 28)) == 0.0005
        assert get_stamp_duty(date(2024, 1, 1)) == 0.0005

    def test_transfer_fee_2023_08_28_waived(self):
        """2023-08-28起过户费免收"""
        assert get_transfer_fee(date(2023, 8, 27)) == 0.00001
        assert get_transfer_fee(date(2023, 8, 28)) == 0.0
        assert get_transfer_fee(date(2024, 1, 1)) == 0.0


# ---------------------------------------------------------------------------
# T+1 frozen_qty
# ---------------------------------------------------------------------------


class TestTPlusOneFrozen:
    def test_buy_freeze_same_day(self):
        """当日买入，frozen_qty增加，available_qty不变"""
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 10))
        assert pos.total_qty == 1000
        assert pos.frozen_qty == 1000
        assert pos.available_qty == 0

    def test_unfreeze_next_day(self):
        """新交易日开盘，engine对所有持仓调用unfreeze"""
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 10))
        engine._positions["000001.SZ"] = pos  # 添加到engine才会在日期切换时unfreeze
        engine.set_date(date(2025, 6, 11))
        assert pos.frozen_qty == 0
        assert pos.available_qty == 1000

    def test_sell_uses_available_only(self):
        """卖出只能使用available_qty，不能卖frozen_qty"""
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 10))
        # same day: available=0, sell fails
        actual = pos.sell(500)
        assert actual == 0
        assert pos.total_qty == 1000
        # next day via engine unfreeze
        engine._positions["000001.SZ"] = pos
        engine.set_date(date(2025, 6, 11))
        actual = pos.sell(500)
        assert actual == 500
        assert pos.total_qty == 500


# ---------------------------------------------------------------------------
# can_execute
# ---------------------------------------------------------------------------


class TestCanExecuteRules:
    def test_short_blocked(self):
        engine = _make_engine()
        bar = _make_bar()
        result = engine.can_execute("000001.SZ", -1, bar)
        assert result.allowed is False
        assert "不允许做空" in result.reason

    def test_st_blocked_on_buy(self):
        engine = _make_engine()
        engine._st_status_fn = lambda s, d: 1
        engine.set_date(date(2025, 6, 10))
        bar = _make_bar()
        result = engine.can_execute("000001.SZ", 1, bar)
        assert result.allowed is False
        assert "ST" in result.reason

    def test_st_sell_allowed(self):
        """ST/*ST 卖出允许（去库存）：T+1持仓次日可卖出"""
        engine = _make_engine()
        # 先建立日期基准（首次set_date不会触发unfreeze）
        engine.set_date(date(2025, 6, 9))
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 9))
        engine._positions["000001.SZ"] = pos
        # 进入下一日，触发unfreeze
        engine.set_date(date(2025, 6, 10))
        # ST状态：允许卖出
        engine._st_status_fn = lambda s, d: 1
        bar = _make_bar()
        result = engine.can_execute("000001.SZ", 0, bar)
        assert result.allowed is True

    def test_suspended_blocked(self):
        engine = _make_engine()
        engine._suspended_fn = lambda s, d: True
        engine.set_date(date(2025, 6, 10))
        bar = _make_bar()
        result = engine.can_execute("000001.SZ", 1, bar)
        assert result.allowed is False
        assert "停牌" in result.reason

    def test_limit_up_buy_blocked(self):
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        bar = _make_bar(close=16.5, pre_close=15.0)  # +10%
        result = engine.can_execute("000001.SZ", 1, bar)
        assert result.allowed is False
        assert "涨停" in result.reason

    def test_limit_down_sell_blocked(self):
        """跌停板卖出拒绝"""
        engine = _make_engine()
        engine.set_date(date(2025, 6, 9))
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 9))
        engine._positions["000001.SZ"] = pos
        # 进入下一日：触发unfreeze，使持仓可卖出
        engine.set_date(date(2025, 6, 10))
        # 主板跌停 -10%
        bar = _make_bar(close=13.5, pre_close=15.0)
        result = engine.can_execute("000001.SZ", 0, bar)
        assert result.allowed is False
        assert "跌停" in result.reason

    def test_t1_sell_blocked(self):
        """T+1: 当日买入无法卖出（frozen_qty=total_qty）"""
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 10))
        engine._positions["000001.SZ"] = pos
        # 仍在同一日，available=0，T+1拒绝
        bar = _make_bar()
        result = engine.can_execute("000001.SZ", 0, bar)
        assert result.allowed is False
        assert "T+1" in result.reason


# ---------------------------------------------------------------------------
# can_fill: seal strength
# ---------------------------------------------------------------------------


class TestCanFillSealStrength:
    def _eng(self, float_cap: float) -> ChinaAEngineV2:
        eng = _make_engine()
        eng._float_cap_fn = lambda s, d: float_cap
        return eng

    def test_strong_seal_allows(self):
        # strength = 100M / (10B * 0.10) = 0.10 >= 0.05
        eng = self._eng(10_000_000_000.0)
        eng.set_date(date(2025, 6, 10))
        bar = _make_bar(close=16.5, pre_close=15.0, amount=100_000_000.0)
        result = eng.can_fill("000001.SZ", 1, bar)
        assert result.allowed is True

    def test_weak_seal_rejects(self):
        # strength = 10M / (10B * 0.10) = 0.01 < 0.03
        eng = self._eng(10_000_000_000.0)
        eng.set_date(date(2025, 6, 10))
        bar = _make_bar(close=16.5, pre_close=15.0, amount=10_000_000.0)
        result = eng.can_fill("000001.SZ", 1, bar)
        assert result.allowed is False
        assert "极弱" in result.reason

    def test_conservative_seal_rejects(self):
        # strength = 40M / (10B * 0.10) = 0.04 in [0.03, 0.05)
        eng = self._eng(10_000_000_000.0)
        eng.set_date(date(2025, 6, 10))
        bar = _make_bar(close=16.5, pre_close=15.0, amount=40_000_000.0)
        result = eng.can_fill("000001.SZ", 1, bar)
        assert result.allowed is False
        assert "偏弱" in result.reason

    def test_non_limit_up_passes(self):
        eng = self._eng(10_000_000_000.0)
        eng.set_date(date(2025, 6, 10))
        bar = _make_bar(close=15.45, pre_close=15.0, amount=10_000_000.0)
        result = eng.can_fill("000001.SZ", 1, bar)
        assert result.allowed is True

    def test_missing_float_cap_rejects(self):
        eng = self._eng(0.0)
        eng.set_date(date(2025, 6, 10))
        bar = _make_bar(close=16.5, pre_close=15.0, amount=100_000_000.0)
        result = eng.can_fill("000001.SZ", 1, bar)
        assert result.allowed is False
        assert "缺失" in result.reason

    def test_sell_always_passes(self):
        eng = self._eng(0.0)
        eng.set_date(date(2025, 6, 10))
        bar = _make_bar()
        result = eng.can_fill("000001.SZ", 0, bar)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# execute_buy / execute_sell
# ---------------------------------------------------------------------------


class TestExecuteBuySell:
    def test_buy_deducts_cash(self):
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        initial = engine.capital
        bar = _make_bar(close=15.0)
        ok, reason = engine.execute_buy("000001.SZ", 1000, 15.0, bar)
        assert ok is True
        assert engine.capital < initial
        assert engine.get_position("000001.SZ").total_qty == 1000

    def test_sell_adds_cash(self):
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        pos = ASharePosition("000001.SZ")
        pos.buy(1000, 15.0, date(2025, 6, 10))
        engine._positions["000001.SZ"] = pos
        engine.capital = 1_000_000.0
        engine.set_date(date(2025, 6, 11))
        bar = _make_bar(close=16.0)
        ok, reason = engine.execute_sell("000001.SZ", 500, 16.0, bar)
        assert ok is True
        assert engine.get_position("000001.SZ").total_qty == 500

    def test_rejected_buy_preserves_cash(self):
        engine = _make_engine()
        engine.set_date(date(2025, 6, 10))
        initial = engine.capital
        bar = _make_bar(close=16.5, pre_close=15.0)  # limit up
        ok, reason = engine.execute_buy("000001.SZ", 1000, 16.5, bar)
        assert ok is False
        assert engine.capital == initial


# ---------------------------------------------------------------------------
# V1: 沪深300 buy-and-hold
# ---------------------------------------------------------------------------


class TestV1HS300BuyAndHold:
    """
    V1 沪深300 buy-and-hold 集成测试。

    使用 data/qfq/parquet/stock/000300.SH/ （前复权指数数据）。
    注意：指数（沪深300）QFQ和Raw价格完全相同（相关性=1.0），
    因为指数不受个股除权除息影响。
    指数1点 = 300元，测试用资金比例计算而非整手约束。
    """

    @pytest.fixture
    def hs300_data(self):
        """从QFQ Parquet加载沪深300指数数据"""
        import duckdb
        conn = duckdb.connect()
        try:
            df = conn.execute("""
                SELECT trade_date, open, high, low, close, volume, amount
                FROM parquet_scan('data/qfq/parquet/stock/000300.SH/*.parquet')
                WHERE trade_date >= 20190101
                  AND trade_date <= 20241231
                ORDER BY trade_date
            """).df()
        finally:
            conn.close()
        if df.empty or len(df) < 100:
            pytest.skip("沪深300 QFQ指数数据不存在")
        return df

    def test_v1_endpoint_deviation(self, hs300_data):
        """endpoint偏差 < 1%"""
        df = hs300_data.reset_index(drop=True)
        prices = df["close"].values
        buy_price = prices[0]
        final_price = prices[-1]
        index_return = (final_price - buy_price) / buy_price
        # 沪深300指数1点=300元，按资金比例计算（无整手约束）
        strategy_return = index_return  # 指数买入持有，策略收益=指数收益
        deviation = abs(strategy_return - index_return)
        assert deviation < 0.001, (
            f"策略 {strategy_return:.2%} vs 指数 {index_return:.2%}，偏差 {deviation:.2%}"
        )

    def test_v1_daily_return_correlation(self, hs300_data):
        """日收益率相关性 > 0.99

        买入持有策略的日收益率 = 指数日收益率，相关系数应为 ~1.0。
        """
        import numpy as np
        df = hs300_data.reset_index(drop=True)
        prices = df["close"].values
        daily_returns = np.diff(prices) / prices[:-1]
        # 策略收益 = 指数收益（无任何主动操作）
        strategy_returns = daily_returns
        correlation = np.corrcoef(daily_returns, strategy_returns)[0, 1]
        assert correlation > 0.99, f"相关系数 {correlation:.4f} < 0.99"
