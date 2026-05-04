"""StockPool — 可交易股票池 + 幸存者偏差处理

职责：
1. 在任意历史时点，返回当时可交易（已上市、未退市、非停牌）的股票列表
2. 查询单只股票在任意日期是否可交易
3. 提供 DuckDB 表初始化 + Parquet 数据回填工具

幸存者偏差根源：回测只选当前存续股票，会漏掉历史上退市的标的。
StockPool 通过 list_date/delist_date 正确处理历史可交易性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


def _date_from_int(v: int | None) -> Optional[date]:
    """INTEGER(YYYYMMDD) -> date 或 None"""
    if v is None:
        return None
    s = str(v)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _int_from_date(d: date) -> int:
    """date -> INTEGER(YYYYMMDD)"""
    return d.year * 10000 + d.month * 100 + d.day


@dataclass
class StockPool:
    """
    可交易股票池。

    list_date/delist_date 格式为 INT(YYYYMMDD)，与 DuckDB schema 一致。
    若 DuckDB 无数据则回退到 Parquet 推断（取每只股票最早/最晚交易日）。

    Usage:
        pool = StockPool.from_duckdb(conn)
        pool.is_tradeable("000001.SZ", date(2020, 6, 1))  # True/False
        pool.get_tradeable_pool(date(2020, 6, 1))  # List[str]
    """

    # symbol -> INT list_date / delist_date
    _list_dates: Dict[str, int] = field(default_factory=dict)
    _delist_dates: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_duckdb(cls, conn) -> "StockPool":
        """从 DuckDB 加载 symbol_meta 表构建 StockPool。"""
        df = conn.execute(
            "SELECT symbol, list_date, delist_date FROM symbol_meta"
        ).df()

        pool = cls()
        for _, row in df.iterrows():
            sym = row["symbol"]
            pool._list_dates[sym] = int(row["list_date"]) if pd.notna(row["list_date"]) else None
            pool._delist_dates[sym] = int(row["delist_date"]) if pd.notna(row["delist_date"]) else None

        return pool

    @classmethod
    def from_parquet(cls, duckdb_path: str, parquet_path: str = "data/parquet/stock") -> "StockPool":
        """
        从 Parquet 数据推断 list_date/delist_date（当 DuckDB 无数据时使用）。

        使用 DuckDB LEFT JOIN index_meta 过滤掉指数符号，只保留股票。

        Args:
            duckdb_path: DuckDB 数据库路径（用于访问 index_meta 表）
            parquet_path: Parquet 文件根目录
        """
        import duckdb

        conn = duckdb.connect(duckdb_path)
        try:
            df = conn.execute(f"""
                SELECT p.symbol,
                       MIN(p.trade_date) AS list_date,
                       MAX(p.trade_date) AS delist_date
                FROM parquet_scan('{parquet_path}/**/*.parquet') p
                LEFT JOIN index_meta i ON p.symbol = i.symbol
                WHERE i.symbol IS NULL
                GROUP BY p.symbol
            """).df()
        finally:
            conn.close()

        pool = cls()
        for _, row in df.iterrows():
            sym = row["symbol"]
            pool._list_dates[sym] = int(row["list_date"]) if pd.notna(row["list_date"]) else None
            pool._delist_dates[sym] = int(row["delist_date"]) if pd.notna(row["delist_date"]) else None

        return pool

    def is_tradeable(self, symbol: str, trade_date: date) -> bool:
        """
        判断 symbol 在 trade_date 是否可交易。

        可交易条件：
          1. symbol 在池中
          2. list_date <= trade_date <= delist_date（或 delist_date 为 NULL）
        """
        list_int = self._list_dates.get(symbol)
        delist_int = self._delist_dates.get(symbol)

        if list_int is None and delist_int is None:
            return False

        trade_int = _int_from_date(trade_date)

        if list_int is not None and trade_int < list_int:
            return False
        if delist_int is not None and trade_int > delist_int:
            return False

        return True

    def get_tradeable_pool(self, trade_date: date) -> List[str]:
        """返回指定日期可交易的所有 symbol 列表。"""
        trade_int = _int_from_date(trade_date)
        result = []

        for sym, list_int in self._list_dates.items():
            delist_int = self._delist_dates.get(sym)

            if list_int is not None and trade_int < list_int:
                continue
            if delist_int is not None and trade_int > delist_int:
                continue

            result.append(sym)

        return result

    def list_date(self, symbol: str) -> Optional[date]:
        """返回 symbol 的上市日期。"""
        return _date_from_int(self._list_dates.get(symbol))

    def delist_date(self, symbol: str) -> Optional[date]:
        """返回 symbol 的退市日期。"""
        return _date_from_int(self._delist_dates.get(symbol))


# ---------------------------------------------------------------------------
# DuckDB 回填工具
# ---------------------------------------------------------------------------


def backfill_symbol_meta_from_parquet(
    duckdb_path: str,
    parquet_path: str = "data/parquet/stock",
) -> int:
    """
    从 Parquet 数据回填 symbol_meta 的 list_date/delist_date。

    适用于：Sprint 0 阶段 DuckDB 表存在但日期字段为空。
    返回：更新的 symbol 数量。
    """
    import duckdb

    conn = duckdb.connect(duckdb_path)

    df = conn.execute(f"""
        SELECT p.symbol,
               MIN(p.trade_date) AS list_date,
               MAX(p.trade_date) AS delist_date
        FROM parquet_scan('{parquet_path}/**/*.parquet') p
        LEFT JOIN index_meta i ON p.symbol = i.symbol
        WHERE i.symbol IS NULL
        GROUP BY p.symbol
    """).df()

    updated = 0
    for _, row in df.iterrows():
        sym = row["symbol"]
        list_d = int(row["list_date"]) if pd.notna(row["list_date"]) else None
        delist_d = int(row["delist_date"]) if pd.notna(row["delist_date"]) else None

        conn.execute(
            """
            UPDATE symbol_meta
            SET list_date = COALESCE(list_date, ?),
                delist_date = COALESCE(delist_date, ?)
            WHERE symbol = ?
            """,
            [list_d, delist_d, sym],
        )
        updated += 1

    conn.close()
    return updated
