"""Live trading AgentLoop tools.

Provides 8 tools for AgentLoop to interact with the live trading system:
  - live_get_portfolio: Query current positions, cash, equity
  - live_get_orders: Query order status
  - live_place_order: Place a new order
  - live_cancel_order: Cancel a pending order
  - live_get_quotes: Get real-time quotes
  - live_get_risk_status: Query risk limits and circuit breaker state
  - live_get_trading_calendar: Check trading day, session phase
  - live_trigger_data_refresh: Manually trigger ETL data refresh

All tools are auto-discovered by build_registry() via BaseTool subclasses.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from src.agent.tools import BaseTool


# ── Helpers ────────────────────────────────────────────────────

def _get_ctx():
    """Get the LiveTradingContext singleton."""
    from src.live.context import LiveTradingContext
    return LiveTradingContext.get_instance()


def _ok(data: Any) -> str:
    return json.dumps({"status": "ok", "data": data}, ensure_ascii=False, default=str)


def _error(msg: str) -> str:
    return json.dumps({"status": "error", "message": msg}, ensure_ascii=False)


def _now_str() -> str:
    return datetime.now().isoformat()


# ── Tools ──────────────────────────────────────────────────────


class LiveGetPortfolioTool(BaseTool):
    """Query current portfolio state: positions, cash, equity."""

    name = "live_get_portfolio"
    description = "查询当前实盘（或模拟盘）持仓、现金和总权益。返回所有持仓的代码、数量、成本、市值和未实现盈亏。"
    parameters = {
        "type": "object",
        "properties": {},
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            pb = ctx.position_book
            positions = {}
            for sym, pos in pb.get_all_positions().items():
                positions[sym] = {
                    "symbol": pos.symbol,
                    "total_qty": pos.total_qty,
                    "available_qty": pos.available_qty,
                    "frozen_qty": pos.frozen_qty,
                    "avg_cost": round(pos.avg_cost, 4),
                    "market_value": round(pos.market_value, 2),
                    "unrealized_pnl": round(pos.unrealized_pnl, 2),
                    "realized_pnl": round(pos.realized_pnl, 2),
                    "today_buys": pos.today_buys,
                    "today_sells": pos.today_sells,
                }
            equity = pb.get_total_equity()
            return _ok({
                "cash": round(pb.get_available_cash(), 2),
                "equity": round(equity, 2),
                "positions": positions,
                "position_count": len(positions),
                "timestamp": _now_str(),
            })
        except Exception as e:
            return _error(str(e))


class LiveGetOrdersTool(BaseTool):
    """Query order status."""

    name = "live_get_orders"
    description = "查询所有订单状态（待成交、已成交、已取消）。可按状态过滤。"
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "按状态过滤（pending/submitted/partial/filled/cancelled/rejected），不传则返回全部",
            },
        },
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            pt = ctx.paper_trader
            status = kwargs.get("status")
            all_orders = pt.get_orders()
            if status:
                all_orders = [o for o in all_orders if o.get("status") == status]
            return _ok({
                "orders": all_orders,
                "count": len(all_orders),
                "timestamp": _now_str(),
            })
        except Exception as e:
            return _error(str(e))


class LivePlaceOrderTool(BaseTool):
    """Place a new order (paper or live)."""

    name = "live_place_order"
    description = "下单买入或卖出股票。返回订单ID和状态。下单前会通过风控引擎校验。"
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "股票代码（如 000001 或 600519）",
            },
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "description": "买入或卖出",
            },
            "qty": {
                "type": "integer",
                "description": "下单数量（股），自动取整到100股的整数倍",
            },
            "price": {
                "type": "number",
                "description": "限价（0表示市价单）",
            },
            "order_type": {
                "type": "string",
                "enum": ["limit", "market"],
                "description": "订单类型：limit(限价)或market(市价)",
            },
            "strategy_id": {
                "type": "string",
                "description": "策略标识（用于后续归因分析）",
            },
        },
        "required": ["symbol", "side", "qty"],
    }
    repeatable = False
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            symbol = str(kwargs["symbol"])
            side = str(kwargs["side"])
            qty = int(kwargs["qty"])
            price = float(kwargs.get("price", 0))
            order_type = str(kwargs.get("order_type", "limit"))
            strategy_id = str(kwargs.get("strategy_id", ""))

            pt = ctx.paper_trader
            order = pt.submit_order(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                order_type=order_type,
                signal_source=strategy_id,
            )
            return _ok(order.to_dict())
        except Exception as e:
            return _error(str(e))


class LiveCancelOrderTool(BaseTool):
    """Cancel a pending order."""

    name = "live_cancel_order"
    description = "撤销一个未成交的订单。"
    parameters = {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "要撤销的订单ID",
            },
        },
        "required": ["order_id"],
    }
    repeatable = False
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            order_id = str(kwargs["order_id"])
            pt = ctx.paper_trader
            ok = pt.cancel_order(order_id)
            if ok:
                return _ok({"order_id": order_id, "cancelled": True})
            return _error(f"无法撤销订单 {order_id}（可能已成交或不存在）")
        except Exception as e:
            return _error(str(e))


class LiveGetQuotesTool(BaseTool):
    """Get real-time quotes."""

    name = "live_get_quotes"
    description = "获取股票实时行情：最新价、涨跌幅、成交量、换手率等。支持批量查询。"
    parameters = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "股票代码列表（如 ['000001', '600519']）",
            },
        },
        "required": ["symbols"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            symbols = list(kwargs["symbols"])
            md = ctx.market_data
            quotes = md.get_batch_quotes(symbols)
            result = {}
            for sym, q in quotes.items():
                result[sym] = {
                    "symbol": q.symbol,
                    "name": q.name,
                    "last_price": q.last_price,
                    "open": q.open,
                    "high": q.high,
                    "low": q.low,
                    "pre_close": q.pre_close,
                    "pct_chg": round(q.pct_chg * 100, 2),
                    "volume": q.volume,
                    "amount": q.amount,
                    "turnover_rate": q.turnover_rate,
                    "timestamp": q.timestamp.isoformat(),
                }
            return _ok({
                "quotes": result,
                "count": len(result),
                "timestamp": _now_str(),
            })
        except Exception as e:
            return _error(str(e))


class LiveGetRiskStatusTool(BaseTool):
    """Query risk engine status."""

    name = "live_get_risk_status"
    description = "查询风控状态：是否触发熔断、当前回撤、峰值权益、日下单数等。"
    parameters = {
        "type": "object",
        "properties": {},
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            re_ = ctx.risk_engine
            return _ok(re_.get_status())
        except Exception as e:
            return _error(str(e))


class LiveGetTradingCalendarTool(BaseTool):
    """Check trading calendar and session phase."""

    name = "live_get_trading_calendar"
    description = "查询交易日历：今天是否是交易日、当前交易时段（集合竞价/连续竞价/午休/已收盘）、距离下一时段的时间。"
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "查询日期（YYYY-MM-DD格式，不传则今天）",
            },
        },
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            cal = ctx.calendar
            now = datetime.now()
            if "date" in kwargs:
                d = date.fromisoformat(kwargs["date"])
            else:
                d = now.date()

            phase = cal.current_session_phase(now)
            return _ok({
                "date": d.isoformat(),
                "is_trading_day": cal.is_trading_day(d),
                "is_market_open": cal.is_market_open(now),
                "current_phase": phase.value,
                "time_to_next_phase_seconds": int(cal.time_to_next_phase(now).total_seconds()),
                "next_trading_day": cal.next_trading_day(d).isoformat(),
                "timestamp": _now_str(),
            })
        except Exception as e:
            return _error(str(e))


class LiveTriggerDataRefreshTool(BaseTool):
    """Manually trigger ETL data refresh."""

    name = "live_trigger_data_refresh"
    description = "手动触发数据刷新（北向资金、板块资金流、换手率等ETL管线）。"
    parameters = {
        "type": "object",
        "properties": {
            "job_name": {
                "type": "string",
                "description": "刷新任务名称（不传则列出所有可用任务）",
            },
        },
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            ctx = _get_ctx()
            sched = ctx.scheduler
            job_name = kwargs.get("job_name")
            if job_name is None:
                return _ok({
                    "available_jobs": sched.list_jobs(),
                    "timestamp": _now_str(),
                })
            ok = sched.trigger_manual_refresh(str(job_name))
            if ok:
                return _ok({"job": job_name, "triggered": True})
            return _error(f"任务 {job_name} 不存在")
        except Exception as e:
            return _error(str(e))
