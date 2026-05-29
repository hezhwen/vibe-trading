"""Integration tests for live trading module.

Tests the full stack: Calendar -> Checkpoint -> Broker -> OMS -> PositionBook -> RiskEngine.
Uses DummyBrokerAdapter (no real broker needed).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.live.broker import DummyBrokerAdapter
from src.live.calendar import TradingCalendar, TradingSessionPhase
from src.live.checkpoint import CheckpointManager, OrdersSnapshot
from src.live.oms import OrderManager, OrderState
from src.live.position_book import PositionBook
from src.live.risk import RiskEngine, RiskLimits


class TestTradingCalendar:
    def test_is_trading_day_weekday(self):
        cal = TradingCalendar()
        d = date(2025, 1, 6)  # Monday
        assert cal.is_trading_day(d)

    def test_is_trading_day_weekend(self):
        cal = TradingCalendar()
        d = date(2025, 1, 4)  # Saturday
        assert not cal.is_trading_day(d)

    def test_session_phase_continuous(self):
        cal = TradingCalendar()
        phase = cal.current_session_phase()
        assert phase in (
            TradingSessionPhase.CONTINUOUS_AUCTION,
            TradingSessionPhase.LUNCH_BREAK,
            TradingSessionPhase.CLOSED,
            TradingSessionPhase.PRE_OPENING,
        )

    def test_next_trading_day(self):
        cal = TradingCalendar()
        d = date(2025, 6, 9)  # Monday
        next_d = cal.next_trading_day(d)
        assert next_d > d
        assert cal.is_trading_day(next_d)


class TestCheckpointManager:
    def test_session_lifecycle(self, tmp_path):
        cm = CheckpointManager(str(tmp_path))
        sid = cm.init_session("test")
        assert sid == "test"

        cm.append_wal("order_create", {"symbol": "600519"})
        assert cm.sequence == 1

        snap = OrdersSnapshot(
            session_id=sid, sequence=1, timestamp="2025-01-01T00:00:00",
            orders=[{"order_id": "1"}], positions=[],
            cash_available=1000000.0, total_equity=1000000.0,
        )
        cm.write_snapshot(snap)

        recovered = cm.recover()
        assert recovered is not None
        assert recovered.session_id == "test"

    def test_replay_wal(self, tmp_path):
        cm = CheckpointManager(str(tmp_path))
        cm.init_session("test")
        cm.append_wal("order_create", {"order_id": "1"})
        cm.append_wal("order_fill", {"order_id": "1"})

        entries = cm.replay_wal(from_sequence=1)
        assert len(entries) == 1  # only sequence 2


class TestDummyBroker:
    def test_submit_and_fill(self):
        broker = DummyBrokerAdapter()
        broker.connect()
        assert broker.is_connected()

        order = broker.submit_order("600519", "buy", 100, 1800.0)
        assert order.status == "submitted"

        filled = broker.simulate_fill(order.broker_order_id, 1802.0)
        assert filled.status == "filled"
        assert filled.avg_fill_price == 1802.0

        positions = broker.query_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "600519"

    def test_cancel_order(self):
        broker = DummyBrokerAdapter()
        broker.connect()
        order = broker.submit_order("000001", "sell", 200, 15.0)
        assert broker.cancel_order(order.broker_order_id)
        cancelled = broker.query_order(order.broker_order_id)
        assert cancelled.status == "cancelled"


class TestPositionBook:
    def test_buy_and_unfreeze(self):
        pb = PositionBook(initial_cash=1_000_000)
        pb.on_fill("600519", "buy", 100, 1800.0, 5.0, datetime.now())

        pos = pb.get_position("600519")
        assert pos.total_qty == 100
        assert pos.available_qty == 0  # T+1 frozen
        assert pos.frozen_qty == 100

        pb.on_new_day(date.today())
        pos = pb.get_position("600519")
        assert pos.available_qty == 100  # unfrozen

    def test_sell_reduces_position(self):
        pb = PositionBook(initial_cash=1_000_000)
        pb.on_fill("600519", "buy", 200, 1800.0, 5.0, datetime.now())
        pb.on_new_day(date.today())
        pb.on_fill("600519", "sell", 100, 1850.0, 10.0, datetime.now())

        pos = pb.get_position("600519")
        assert pos.total_qty == 100

    def test_cash_tracking(self):
        pb = PositionBook(initial_cash=1_000_000)
        pb.on_fill("000001", "buy", 100, 10.0, 5.0, datetime.now())
        assert pb.get_available_cash() < 1_000_000  # spent money

    def test_trade_history(self):
        pb = PositionBook(initial_cash=1_000_000)
        pb.on_fill("600519", "buy", 100, 1800.0, 5.0, datetime.now())
        history = pb.get_trade_history()
        assert len(history) == 1
        assert history[0].symbol == "600519"


class TestRiskEngine:
    def test_pre_trade_allows_normal_order(self):
        limits = RiskLimits()
        re_ = RiskEngine(limits=limits)
        allowed, reason = re_.pre_trade_check("600519", "buy", 100, 1800.0)
        assert allowed

    def test_pre_trade_rejects_large_order(self):
        limits = RiskLimits(max_order_value=100_000)
        re_ = RiskEngine(limits=limits)
        allowed, reason = re_.pre_trade_check("600519", "buy", 10000, 1800.0)
        assert not allowed
        assert "超过上限" in reason

    def test_rate_limiting(self):
        limits = RiskLimits(min_order_interval_ms=500)
        re_ = RiskEngine(limits=limits)
        re_.pre_trade_check("600519", "buy", 100, 1800.0)
        allowed, reason = re_.pre_trade_check("000001", "sell", 100, 15.0)
        assert not allowed
        assert "频率" in reason

    def test_breach_blocks_orders(self):
        re_ = RiskEngine()
        re_._breached = True
        re_._breach_reason = "test breach"
        allowed, reason = re_.pre_trade_check("600519", "buy", 100, 1800.0)
        assert not allowed
        assert "熔断" in reason

    def test_restricted_symbols(self):
        limits = RiskLimits(restricted_symbols=("000001",))
        re_ = RiskEngine(limits=limits)
        allowed, reason = re_.pre_trade_check("000001", "buy", 100, 10.0)
        assert not allowed


class TestOrderManager:
    def test_create_and_submit(self, tmp_path):
        broker = DummyBrokerAdapter()
        broker.connect()
        cm = CheckpointManager(str(tmp_path))
        cm.init_session("test_oms")
        pb = PositionBook(initial_cash=1_000_000)

        oms = OrderManager(broker=broker, checkpoint=cm, position_book=pb)
        order = oms.create_order("600519", "buy", 100, 1800.0)
        assert order.status == "pending"

        submitted = oms.submit_order(order.order_id)
        assert submitted.status == "submitted"
        assert submitted.broker_order_id

    def test_cannot_submit_twice(self, tmp_path):
        broker = DummyBrokerAdapter()
        broker.connect()
        cm = CheckpointManager(str(tmp_path))
        cm.init_session("test")
        oms = OrderManager(broker=broker, checkpoint=cm)

        order = oms.create_order("600519", "buy", 100, 1800.0)
        oms.submit_order(order.order_id)
        with pytest.raises(ValueError):
            oms.submit_order(order.order_id)

    def test_reject_with_risk(self, tmp_path):
        broker = DummyBrokerAdapter()
        broker.connect()
        cm = CheckpointManager(str(tmp_path))
        cm.init_session("test")
        limits = RiskLimits(max_order_value=10_000)
        re_ = RiskEngine(limits=limits)
        oms = OrderManager(broker=broker, risk_engine=re_, checkpoint=cm)

        order = oms.create_order("600519", "buy", 10000, 1800.0)
        rejected = oms.submit_order(order.order_id)
        assert rejected.status == "rejected"

    def test_cancel_order(self, tmp_path):
        broker = DummyBrokerAdapter()
        broker.connect()
        cm = CheckpointManager(str(tmp_path))
        cm.init_session("test")
        oms = OrderManager(broker=broker, checkpoint=cm)

        order = oms.create_order("600519", "buy", 100, 1800.0)
        oms.submit_order(order.order_id)
        cancelled = oms.cancel_order(order.order_id)
        assert cancelled.status == "cancelled"
