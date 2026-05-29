"""Signal-layer AgentLoop tools: dragon tiger, northbound, fund flow, margin.

Wraps key a-stock-data endpoints (Eastmoney datacenter, THS, push2) as
auto-discoverable BaseTool subclasses.  All free, no API key needed.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import requests

from src.agent.tools import BaseTool

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# ── Shared helpers ─────────────────────────────────────────────


def _eastmoney_datacenter(
    report_name: str,
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """Eastmoney datacenter unified query — dragon tiger / margin / etc."""
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


def _ok(data: Any) -> str:
    return json.dumps({"status": "ok", "data": data}, ensure_ascii=False, default=str)


def _error(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)


# ── Tool 1: Daily Dragon Tiger (all-market) ────────────────────


class SignalDailyDragonTigerTool(BaseTool):
    name = "signal_daily_dragon_tiger"
    description = "查询全市场龙虎榜：当日所有上榜股票、净买额排名、上榜原因、买卖金额。不传日期默认今天。"
    parameters = {
        "type": "object",
        "properties": {
            "trade_date": {
                "type": "string",
                "description": "交易日期 YYYY-MM-DD，不传默认今天",
            },
            "min_net_buy": {
                "type": "number",
                "description": "净买入下限（万元），不传不过滤",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, trade_date: str = "", min_net_buy: float | None = None, **kwargs: Any) -> str:
        if not trade_date:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
            page_size=500,
            sort_columns="BILLBOARD_NET_AMT",
            sort_types="-1",
        )
        if not data:
            return _ok({"date": trade_date, "total_records": 0, "stocks": [],
                         "note": "无数据（非交易日或盘后未更新）"})

        actual_date = str(data[0].get("TRADE_DATE", ""))[:10] if data else trade_date
        stocks = []
        for row in data:
            net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
            if min_net_buy is not None and net_buy < min_net_buy:
                continue
            stocks.append({
                "code": row.get("SECURITY_CODE", ""),
                "name": row.get("SECURITY_NAME_ABBR", ""),
                "reason": row.get("EXPLANATION", ""),
                "close": row.get("CLOSE_PRICE") or 0,
                "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
                "net_buy_wan": round(net_buy, 1),
                "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
                "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
                "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
            })
        return _ok({"date": actual_date, "total_records": len(stocks), "stocks": stocks})


# ── Tool 2: Single-stock Dragon Tiger ──────────────────────────


class SignalDragonTigerTool(BaseTool):
    name = "signal_dragon_tiger"
    description = "查询个股龙虎榜上榜记录、买卖席位TOP5、机构动向。自动回看30天。"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "6位股票代码，如 600519",
            },
            "trade_date": {
                "type": "string",
                "description": "交易日期 YYYY-MM-DD，不传默认今天",
            },
            "look_back": {
                "type": "integer",
                "description": "回看天数，默认30",
            },
        },
        "required": ["code"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, code: str, trade_date: str = "", look_back: int = 30, **kwargs: Any) -> str:
        if not trade_date:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        from datetime import timedelta
        start = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)
        start_str = start.strftime("%Y-%m-%d")

        # 1. 上榜记录
        records = []
        raw = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{start_str}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        for row in raw:
            records.append({
                "date": str(row.get("TRADE_DATE", ""))[:10],
                "reason": row.get("EXPLANATION", ""),
                "net_buy_wan": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
                "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
            })

        # 2. 买卖席位
        seats = {"buy": [], "sell": []}
        if records:
            latest_date = records[0]["date"]
            buy_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="BUY", sort_types="-1",
            )
            for row in buy_data[:5]:
                seats["buy"].append({
                    "name": row.get("OPERATEDEPT_NAME", ""),
                    "buy_wan": round((row.get("BUY") or 0) / 10000, 1),
                    "sell_wan": round((row.get("SELL") or 0) / 10000, 1),
                    "net_wan": round((row.get("NET") or 0) / 10000, 1),
                })
            sell_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="SELL", sort_types="-1",
            )
            for row in sell_data[:5]:
                seats["sell"].append({
                    "name": row.get("OPERATEDEPT_NAME", ""),
                    "buy_wan": round((row.get("BUY") or 0) / 10000, 1),
                    "sell_wan": round((row.get("SELL") or 0) / 10000, 1),
                    "net_wan": round((row.get("NET") or 0) / 10000, 1),
                })

        return _ok({
            "code": code,
            "records": records,
            "latest_seats": seats,
        })


# ── Tool 3: Northbound Flow ────────────────────────────────────


class SignalNorthboundFlowTool(BaseTool):
    name = "signal_northbound_flow"
    description = "查询沪深股通（北向资金）当日实时分钟级资金流向，沪股通+深股通累计净买入。"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        headers = {
            "User-Agent": UA,
            "Host": "data.hexin.cn",
            "Referer": "https://data.hexin.cn/",
        }
        try:
            r = requests.get(
                "https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                headers=headers, timeout=10,
            )
            d = r.json()
            times = d.get("time", [])
            hgt = d.get("hgt", [])
            sgt = d.get("sgt", [])

            if times:
                hgt_val = hgt[-1] if hgt else 0
                sgt_val = sgt[-1] if sgt else 0
                return _ok({
                    "data_points": len(times),
                    "latest_hgt_yi": hgt_val,
                    "latest_sgt_yi": sgt_val,
                    "total_yi": hgt_val + sgt_val,
                    "note": "单位：亿元，正值=净流入",
                })
            return _ok({"data_points": 0, "note": "暂无实时数据"})
        except Exception as e:
            return _error(f"北向资金查询失败: {e}")


# ── Tool 4: Fund Flow (minute-level) ───────────────────────────


class SignalFundFlowTool(BaseTool):
    name = "signal_fund_flow"
    description = "查询个股当日分钟级资金流向：主力/超大单/大单/中单/小单净流入。"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "6位股票代码",
            },
        },
        "required": ["code"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, code: str, **kwargs: Any) -> str:
        secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
        url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        headers = {
            "User-Agent": UA,
            "Referer": "https://quote.eastmoney.com/",
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            d = r.json()
        except Exception as e:
            return _error(f"资金流查询失败: {e}")

        klines = d.get("data", {}).get("klines", [])
        if not klines:
            return _ok({"code": code, "points": 0, "note": "暂无数据"})

        points = []
        for line in klines:
            parts = line.split(",")
            if len(parts) >= 6:
                points.append({
                    "time": parts[0],
                    "main_net": float(parts[1]),
                    "small_net": float(parts[2]),
                    "mid_net": float(parts[3]),
                    "large_net": float(parts[4]),
                    "super_net": float(parts[5]),
                })

        total_main = sum(p["main_net"] for p in points)
        latest = points[-1] if points else {}
        return _ok({
            "code": code,
            "points": len(points),
            "total_main_net": total_main,
            "total_main_net_wan": round(total_main / 10000, 1),
            "latest": latest,
            "signal": "bullish" if total_main > 0 else "bearish",
            "note": "金额单位：元",
        })


# ── Tool 5: Margin Trading ─────────────────────────────────────


class SignalMarginTradingTool(BaseTool):
    name = "signal_margin_trading"
    description = "查询个股融资融券明细：每日融资余额、融资买入/偿还、融券余额。"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "6位股票代码",
            },
            "days": {
                "type": "integer",
                "description": "查询天数，默认30",
            },
        },
        "required": ["code"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, code: str, days: int = 30, **kwargs: Any) -> str:
        data = _eastmoney_datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            filter_str=f'(SCODE="{code}")',
            page_size=days,
            sort_columns="DATE", sort_types="-1",
        )
        rows = []
        for row in data:
            rows.append({
                "date": str(row.get("DATE", ""))[:10],
                "rzye": row.get("RZYE", 0),       # 融资余额(元)
                "rzmre": row.get("RZMRE", 0),      # 融资买入额
                "rzche": row.get("RZCHE", 0),      # 融资偿还额
                "rqye": row.get("RQYE", 0),        # 融券余额(元)
                "rzrqye": row.get("RZRQYE", 0),    # 融资融券余额合计
            })

        # Summary
        if rows:
            latest = rows[0]
            trend = "上升" if len(rows) >= 2 and latest["rzye"] > rows[-1]["rzye"] else "下降"
        else:
            latest = {}
            trend = "无数据"

        return _ok({
            "code": code,
            "records": len(rows),
            "latest": latest,
            "margin_balance": latest.get("rzye", 0),
            "trend": trend,
            "history": rows,
        })


# ── Tool 6: Hot Stocks / Sector Attribution ────────────────────


class SignalHotStocksTool(BaseTool):
    name = "signal_hot_stocks"
    description = "查询当日强势股及题材归因：同花顺编辑部人工标注的走强原因标签（reason tags）。"
    parameters = {
        "type": "object",
        "properties": {
            "trade_date": {
                "type": "string",
                "description": "交易日期 YYYY-MM-DD，不传默认今天",
            },
            "top_n": {
                "type": "integer",
                "description": "返回前N只，默认15",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, trade_date: str = "", top_n: int = 15, **kwargs: Any) -> str:
        if not trade_date:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{trade_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": UA,
        }
        try:
            r = requests.get(url, headers=headers, timeout=10)
            d = r.json()
            if d.get("errocode", 0) != 0:
                return _error(f"同花顺热点错误: {d.get('errormsg', '')}")
        except Exception as e:
            return _error(f"查询失败: {e}")

        rows = d.get("data") or []
        stocks = []
        for row in rows[:top_n]:
            stocks.append({
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "reason": row.get("reason", ""),  # 核心字段：人工标注题材
                "change_pct": row.get("zhangfu", 0),
                "close": row.get("close", 0),
                "turnover_pct": row.get("huanshou", 0),
                "amount": row.get("chengjiaoe", 0),
                "dde_net": row.get("ddejingliang", 0),
                "market": row.get("market", ""),
            })

        return _ok({
            "date": trade_date,
            "total": len(rows),
            "stocks": stocks,
            "note": "reason字段为同花顺编辑部人工标注的题材标签",
        })
