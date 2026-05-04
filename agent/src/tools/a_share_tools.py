"""A-share data tools: limit up, sector flow, north flow, ST status, suspended.

backtest mode: queries DuckDB (local Parquet data)
live mode: queries AKShare (real-time, only recent data)
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

# ─── DuckDB connection helper ────────────────────────────────────────


def _get_duckdb_path() -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[4]
    return str(root / "data" / "duckdb" / "china_a.duckdb")


def _duckdb_query(sql: str, params: list = None) -> List[Dict[str, Any]]:
    try:
        import duckdb
        db_path = _get_duckdb_path()
        conn = duckdb.connect(db_path, read_only=True)
        try:
            rows = conn.execute(sql, params).fetchall() if params else conn.execute(sql).fetchall()
            cols = [d[0] for d in conn.description]
            return [dict(zip(cols, row)) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("DuckDB query failed: %s", exc)
        return []


def _date_to_int(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


# ─── get_limit_up_stocks ─────────────────────────────────────────────

def get_limit_up_stocks_impl(
    trade_date: date,
    market: str = "all",
    mode: str = "backtest",
) -> List[Dict[str, Any]]:
    td_int = _date_to_int(trade_date)
    if mode == "backtest":
        market_filter = ""
        if market == "sh":
            market_filter = " AND s.symbol LIKE '600%' "
        elif market == "sz":
            market_filter = " AND (s.symbol LIKE '000%' OR s.symbol LIKE '001%') "
        elif market == "bj":
            market_filter = " AND s.symbol LIKE '8%' "
        sql = f"""
            SELECT s.symbol, s.name, s.board,
                   lh.close, lh.prev_close, lh.amount, lh.seal_strength, lh.limit_up_type
            FROM limit_up_history lh
            JOIN symbol_meta s ON s.symbol = lh.symbol
            WHERE lh.trade_date = ?
              {market_filter}
            ORDER BY lh.amount DESC
            LIMIT 200
        """
        rows = _duckdb_query(sql, [td_int])
        for r in rows:
            r["trade_date"] = td_int
        return rows
    try:
        import akshare as ak
        df = ak.stock_zh_a_alarm_sina()
        results = []
        for _, row in df.iterrows():
            sym = str(row.get("symbol", ""))
            if market == "sh" and not sym.startswith("sh"):
                continue
            if market == "sz" and not sym.startswith("sz"):
                continue
            if market == "bj" and not sym.startswith(("bj", "8")):
                continue
            pct = float(row.get("percent", 0))
            if pct <= 9.5:
                continue
            results.append({
                "symbol": sym,
                "name": str(row.get("name", "")),
                "close": float(row.get("close", 0)),
                "limit_up_pct": pct / 100,
                "amount": float(row.get("amount", 0)),
                "seal_strength": None,
            })
        return results
    except Exception as exc:
        logger.warning("AKShare limit_up failed: %s", exc)
        return []


class GetLimitUpStocksTool(BaseTool):
    name = "get_limit_up_stocks"
    description = (
        "Get list of limit-up (涨停) stocks for a given date. "
        "backtest mode: queries local DuckDB (fast, historical). "
        "live mode: queries AKShare (real-time, only recent data)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "trade_date": {"type": "string", "description": "Trading date (YYYY-MM-DD)"},
            "market": {"type": "string", "enum": ["all", "sh", "sz", "bj"], "default": "all"},
            "mode": {"type": "string", "enum": ["backtest", "live"], "default": "backtest"},
        },
        "required": ["trade_date"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs) -> str:
        trade_date = date.fromisoformat(kwargs["trade_date"])
        market = kwargs.get("market", "all")
        mode = kwargs.get("mode", "backtest")
        results = get_limit_up_stocks_impl(trade_date, market, mode)
        return json.dumps({
            "status": "ok", "trade_date": str(trade_date),
            "market": market, "mode": mode, "count": len(results), "data": results,
        }, ensure_ascii=False)


# ─── get_sector_flow ────────────────────────────────────────────────

def get_sector_flow_impl(trade_date: date, top_n: int = 10, mode: str = "backtest") -> List[Dict[str, Any]]:
    td_int = _date_to_int(trade_date)
    if mode == "backtest":
        sql = """
            SELECT board_code, board_name, turnover, flow_pct, limit_up_count
            FROM sector_daily_flow
            WHERE trade_date = ?
            ORDER BY turnover DESC
            LIMIT ?
        """
        return _duckdb_query(sql, [td_int, top_n])
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        df = df.sort_values("成交额", ascending=False).head(top_n)
        results = []
        for _, row in df.iterrows():
            results.append({
                "board_code": str(row.get("板块代码", "")),
                "board_name": str(row.get("板块名称", "")),
                "turnover": float(row.get("成交额", 0)),
                "limit_up_count": int(row.get("涨停家数", 0)),
            })
        return results
    except Exception as exc:
        logger.warning("AKShare sector_flow failed: %s", exc)
        return []


class GetSectorFlowTool(BaseTool):
    name = "get_sector_flow"
    description = (
        "Get sector capital flow ranked by turnover. "
        "backtest mode: queries DuckDB (historical). "
        "live mode: queries AKShare (recent only)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "trade_date": {"type": "string", "description": "Trading date (YYYY-MM-DD)"},
            "top_n": {"type": "integer", "default": 10},
            "mode": {"type": "string", "enum": ["backtest", "live"], "default": "backtest"},
        },
        "required": ["trade_date"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs) -> str:
        trade_date = date.fromisoformat(kwargs["trade_date"])
        results = get_sector_flow_impl(trade_date, kwargs.get("top_n", 10), kwargs.get("mode", "backtest"))
        return json.dumps({"status": "ok", "trade_date": str(trade_date), "count": len(results), "data": results}, ensure_ascii=False)


# ─── get_north_flow ─────────────────────────────────────────────────

def get_north_flow_impl(trade_date: date, mode: str = "backtest") -> Dict[str, Any]:
    td_int = _date_to_int(trade_date)
    if mode == "backtest":
        sql = """
            SELECT trade_date, industry_code, industry_name, net_inflow, net_inflow_pct
            FROM north_flow_industry
            WHERE trade_date = ?
            ORDER BY net_inflow DESC
        """
        rows = _duckdb_query(sql, [td_int])
        total_net = sum(r.get("net_inflow", 0) or 0 for r in rows)
        return {"trade_date": td_int, "total_net_inflow": total_net, "note": "⚠️ T+1数据", "data": rows}
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_inflow_em(symbol="北向资金")
        if df is None or df.empty:
            return {"status": "ok", "data": [], "note": "⚠️ 无数据"}
        latest = df.iloc[-1]
        return {
            "trade_date": str(latest.get("日期", "")),
            "total_net_inflow": float(latest.get("北向资金净流入", 0)),
            "sh_net_inflow": float(latest.get("沪股通净流入", 0)),
            "sz_net_inflow": float(latest.get("深股通净流入", 0)),
            "note": "⚠️ 2024-05后为T+1数据",
            "data": [],
        }
    except Exception as exc:
        logger.warning("AKShare north_flow failed: %s", exc)
        return {"status": "ok", "data": [], "error": str(exc)}


class GetNorthFlowTool(BaseTool):
    name = "get_north_flow"
    description = (
        "Get northbound capital flow (北向资金) — foreign capital into A-shares. "
        "⚠️ Data is T+1. backtest: DuckDB. live: AKShare."
    )
    parameters = {
        "type": "object",
        "properties": {
            "trade_date": {"type": "string", "description": "Trading date (YYYY-MM-DD)"},
            "mode": {"type": "string", "enum": ["backtest", "live"], "default": "backtest"},
        },
        "required": ["trade_date"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs) -> str:
        trade_date = date.fromisoformat(kwargs["trade_date"])
        result = get_north_flow_impl(trade_date, kwargs.get("mode", "backtest"))
        result["status"] = "ok"
        return json.dumps(result, ensure_ascii=False, default=str)


# ─── get_st_status ──────────────────────────────────────────────────

def get_st_status_impl(symbol: str, trade_date: date, mode: str = "backtest") -> Dict[str, Any]:
    td_int = _date_to_int(trade_date)
    if mode == "backtest":
        sql = """
            SELECT st_type, event_date FROM st_status_events
            WHERE symbol = ? AND event_date <= ?
            ORDER BY event_date DESC LIMIT 1
        """
        rows = _duckdb_query(sql, [symbol, td_int])
        if rows:
            st = rows[0]["st_type"]
            return {
                "symbol": symbol, "st_type": st,
                "status": "正常" if st == 0 else "ST" if st == 1 else "*ST" if st == 2 else "退市整理",
                "event_date": rows[0]["event_date"],
            }
        return {"symbol": symbol, "st_type": 0, "status": "正常"}
    try:
        import akshare as ak
        df = ak.stock_zh_a_st_em()
        code = symbol.split(".")[0]
        match = df[df["代码"] == code]
        if match.empty:
            return {"symbol": symbol, "st_type": 0, "status": "正常"}
        name = str(match.iloc[0].get("名称", ""))
        if name.startswith("*ST"):
            st_type = 2
        elif name.startswith("ST"):
            st_type = 1
        else:
            st_type = 0
        return {"symbol": symbol, "st_type": st_type, "status": "正常" if st_type == 0 else "*ST" if st_type == 2 else "ST"}
    except Exception as exc:
        logger.warning("AKShare st_status failed: %s", exc)
        return {"symbol": symbol, "st_type": 0, "status": "正常", "error": str(exc)}


class GetStStatusTool(BaseTool):
    name = "get_st_status"
    description = (
        "Get ST/*ST status for a stock. "
        "Returns st_type: 0=正常, 1=ST, 2=*ST, 3=退市整理. "
        "backtest: DuckDB. live: AKShare."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock symbol (e.g. 000001.SZ)"},
            "trade_date": {"type": "string", "description": "Trading date (YYYY-MM-DD)"},
            "mode": {"type": "string", "enum": ["backtest", "live"], "default": "backtest"},
        },
        "required": ["symbol", "trade_date"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs) -> str:
        result = get_st_status_impl(kwargs["symbol"], date.fromisoformat(kwargs["trade_date"]), kwargs.get("mode", "backtest"))
        result["status"] = "ok"
        return json.dumps(result, ensure_ascii=False, default=str)


# ─── get_suspended ─────────────────────────────────────────────────

def get_suspended_impl(symbol: str, start_date: Optional[date] = None, end_date: Optional[date] = None, mode: str = "backtest") -> List[Dict[str, Any]]:
    if mode != "backtest":
        return []
    sql = "SELECT suspend_date, resume_date, suspend_type, reason FROM suspension_records WHERE symbol = ?"
    params = [symbol]
    if start_date:
        sql += " AND suspend_date >= ?"
        params.append(_date_to_int(start_date))
    if end_date:
        sql += " AND suspend_date <= ?"
        params.append(_date_to_int(end_date))
    sql += " ORDER BY suspend_date DESC"
    return _duckdb_query(sql, params)


class GetSuspendedTool(BaseTool):
    name = "get_suspended"
    description = (
        "Get suspension (停牌) records for a stock. "
        "backtest: DuckDB. live: not well supported."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock symbol (e.g. 000001.SZ)"},
            "start_date": {"type": "string", "description": "Start date YYYY-MM-DD (optional)"},
            "end_date": {"type": "string", "description": "End date YYYY-MM-DD (optional)"},
            "mode": {"type": "string", "enum": ["backtest", "live"], "default": "backtest"},
        },
        "required": ["symbol"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs) -> str:
        s = date.fromisoformat(kwargs["start_date"]) if kwargs.get("start_date") else None
        e = date.fromisoformat(kwargs["end_date"]) if kwargs.get("end_date") else None
        results = get_suspended_impl(kwargs["symbol"], s, e, kwargs.get("mode", "backtest"))
        return json.dumps({"status": "ok", "symbol": kwargs["symbol"], "count": len(results), "data": results}, ensure_ascii=False, default=str)
