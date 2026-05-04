"""Parquet storage layer — reader/writer for TDX .day data.

功能：
- ParquetReader: 按 symbol + date_range 查询，支持年度分区裁剪
- ParquetWriter: 追加写入单条记录，按 symbol/year 二级分区

Schema (TDX .day):
  trade_date: INT (YYYYMMDD)
  symbol:     VARCHAR (e.g. "000001.SZ")
  open/high/low/close: FLOAT
  amount:     FLOAT
  volume:     INT (hand, already ÷100)

Compression: zstd (write), auto (read)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

PARQUET_COLS = ["trade_date", "symbol", "open", "high", "low", "close", "amount", "volume"]
DTYPES = {
    "trade_date": "int32",
    "symbol": "object",
    "open": "float32",
    "high": "float32",
    "low": "float32",
    "close": "float32",
    "amount": "float32",
    "volume": "int32",
}


# ---------------------------------------------------------------------------
# ParquetReader
# ---------------------------------------------------------------------------

class ParquetReader:
    """
    按 symbol + date_range 读取 Parquet 日K数据。

    路径格式: {root}/stock/{symbol}/{year}.parquet

    Usage:
        reader = ParquetReader("data/parquet")
        df = reader.read("000001.SZ", date(2020, 1, 1), date(2020, 12, 31))
    """

    def __init__(self, root: str = "data/parquet"):
        self.root = Path(root)

    def _year_files(self, symbol: str, start: date, end: date) -> Iterator[Path]:
        """生成 date_range 覆盖的年份文件路径。"""
        stock_dir = self.root / "stock" / symbol
        if not stock_dir.is_dir():
            return

        for year in range(start.year, end.year + 1):
            path = stock_dir / f"{year}.parquet"
            if path.is_file():
                yield path

    def _date_filter(self, df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        """按 trade_date 过滤。"""
        if df.empty:
            return df
        start_int = start.year * 10000 + start.month * 100 + start.day
        end_int = end.year * 10000 + end.month * 100 + end.day
        return df[(df["trade_date"] >= start_int) & (df["trade_date"] <= end_int)]

    def read(
        self,
        symbol: str,
        start: date,
        end: date,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        读取 symbol 在 [start, end] 区间的所有日K数据。

        Args:
            symbol: 股票代码，如 "000001.SZ"
            start:  开始日期
            end:    结束日期
            columns: 可选，只读取指定列

        Returns:
            DataFrame，按 trade_date 升序排列
        """
        frames = []
        for path in self._year_files(symbol, start, end):
            try:
                df = pd.read_parquet(path, columns=columns or PARQUET_COLS)
                frames.append(df)
            except Exception:
                continue

        if not frames:
            return pd.DataFrame(columns=columns or PARQUET_COLS)

        result = pd.concat(frames, ignore_index=True)
        result = self._date_filter(result, start, end)
        result = result.sort_values("trade_date").reset_index(drop=True)

        # Convert trade_date int -> date
        result["trade_date"] = pd.to_datetime(
            result["trade_date"].astype(str), format="%Y%m%d"
        ).dt.date

        return result

    def read_single(self, symbol: str, trade_date: date) -> Optional[pd.Series]:
        """读取单日数据。"""
        df = self.read(symbol, trade_date, trade_date)
        if df.empty:
            return None
        return df.iloc[0]

    def exists(self, symbol: str, trade_date: date) -> bool:
        """检查某日是否有数据。"""
        return self.read_single(symbol, trade_date) is not None

    def symbols_with_data(self) -> List[str]:
        """返回有数据的所有 symbol。"""
        stock_dir = self.root / "stock"
        if not stock_dir.is_dir():
            return []
        return [d.name for d in stock_dir.iterdir() if d.is_dir()]


# ---------------------------------------------------------------------------
# ParquetWriter
# ---------------------------------------------------------------------------

class ParquetWriter:
    """
    按 symbol/year 二级分区追加写入 Parquet 文件。

    路径格式: {root}/stock/{symbol}/{year}.parquet
    Compression: zstd

    Usage:
        writer = ParquetWriter("data/parquet")
        writer.write({
            "trade_date": 20200102,
            "symbol": "000001.SZ",
            "open": 15.0,
            "high": 15.5,
            "low": 14.8,
            "close": 15.2,
            "amount": 1000000.0,
            "volume": 65800,
        })
    """

    def __init__(self, root: str = "data/parquet", compression: str = "zstd"):
        self.root = Path(root)
        self.compression = compression

    def _path(self, symbol: str, year: int) -> Path:
        return self.root / "stock" / symbol / f"{year}.parquet"

    def write(self, record: Dict) -> None:
        """
        追加写入单条记录到对应年份 Parquet 文件。

        如果文件不存在则创建（写入单行 DataFrame）。
        如果文件存在则读取-追加-重写。

        Args:
            record: dict with keys: trade_date(int), symbol, open, high, low,
                    close, amount, volume
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        symbol = record["symbol"]
        year = int(str(record["trade_date"])[:4])
        path = self._path(symbol, year)

        path.parent.mkdir(parents=True, exist_ok=True)

        new_row = pd.DataFrame([record], columns=PARQUET_COLS).astype(DTYPES)

        if path.is_file():
            existing = pd.read_parquet(path, columns=PARQUET_COLS)
            combined = pd.concat([existing, new_row], ignore_index=True)
            # Deduplicate by trade_date (keep last)
            combined = combined.drop_duplicates(subset=["trade_date"], keep="last")
            combined = combined.sort_values("trade_date").reset_index(drop=True)
        else:
            combined = new_row

        table = pa.Table.from_pandas(combined, preserve_index=False)
        pq.write_table(
            table,
            path,
            compression=self.compression,
            use_dictionary=True,
        )

    def write_batch(self, records: List[Dict]) -> None:
        """批量写入多条记录。"""
        for record in records:
            self.write(record)

    def write_dataframe(self, df: pd.DataFrame) -> None:
        """
        批量写入整个 DataFrame（按年份分组）。

        DataFrame 必须包含 trade_date, symbol, open, high, low, close, amount, volume
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        df = df.astype(DTYPES)

        for (sym, year), group in df.groupby([df["symbol"], df["trade_date"] // 10000]):
            path = self._path(sym, year)
            path.parent.mkdir(parents=True, exist_ok=True)

            group_sorted = group.sort_values("trade_date").reset_index(drop=True)

            if path.is_file():
                existing = pd.read_parquet(path, columns=PARQUET_COLS)
                combined = pd.concat([existing, group_sorted], ignore_index=True)
                combined = combined.drop_duplicates(subset=["trade_date"], keep="last")
                combined = combined.sort_values("trade_date").reset_index(drop=True)
            else:
                combined = group_sorted

            table = pa.Table.from_pandas(combined, preserve_index=False)
            pq.write_table(
                table,
                path,
                compression=self.compression,
                use_dictionary=True,
            )
