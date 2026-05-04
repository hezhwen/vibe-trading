"""Tests for ParquetReader and ParquetWriter."""

from __future__ import annotations

import os
import tempfile
from datetime import date

import pandas as pd
import pytest

from backtest.parquet_io import ParquetReader, ParquetWriter, PARQUET_COLS, DTYPES


@pytest.fixture
def tmp_root(tmp_path):
    return str(tmp_path / "parquet")


class TestParquetReader:
    @pytest.fixture
    def reader(self):
        return ParquetReader("/root/github-code/Vibe-Trading/data/parquet")

    def test_read_000001_sz_2020(self, reader):
        """2020年日K数据约243个交易日"""
        df = reader.read("000001.SZ", date(2020, 1, 1), date(2020, 12, 31))
        assert len(df) == 243, f"Expected 243, got {len(df)}"
        assert df["symbol"].iloc[0] == "000001.SZ"
        assert list(df.columns) == PARQUET_COLS

    def test_read_single_bar(self, reader):
        """read_single 返回单日bar"""
        bar = reader.read_single("000001.SZ", date(2020, 1, 2))
        assert bar is not None
        assert bar["close"] > 0

    def test_read_single_not_exists(self, reader):
        """不存在的日期返回None"""
        bar = reader.read_single("999999.SZ", date(2020, 1, 2))
        assert bar is None

    def test_read_sorted_by_date(self, reader):
        """结果按trade_date升序"""
        df = reader.read("000001.SZ", date(2020, 1, 1), date(2020, 3, 31))
        dates = pd.to_datetime(df["trade_date"]).dt.date.tolist()
        assert dates == sorted(dates)

    def test_read_trade_date_is_date_object(self, reader):
        """trade_date列是date对象，不是datetime"""
        df = reader.read("000001.SZ", date(2020, 1, 1), date(2020, 1, 10))
        assert isinstance(df["trade_date"].iloc[0], date)

    def test_symbols_with_data(self, reader):
        """symbol_with_data返回所有有数据的股票"""
        syms = reader.symbols_with_data()
        assert "000001.SZ" in syms
        assert len(syms) > 5000

    def test_read_unknown_symbol_empty(self, reader):
        """不存在的symbol返回空DataFrame"""
        df = reader.read("999999.SZ", date(2020, 1, 1), date(2020, 12, 31))
        assert df.empty


class TestParquetWriter:
    def test_write_single_record(self, tmp_root):
        """写入单条记录，文件创建成功"""
        writer = ParquetWriter(tmp_root)

        record = {
            "trade_date": 20200102,
            "symbol": "000001.SZ",
            "open": 16.0,
            "high": 16.5,
            "low": 15.8,
            "close": 16.2,
            "amount": 1000000.0,
            "volume": 6200,
        }
        writer.write(record)

        path = f"{tmp_root}/stock/000001.SZ/2020.parquet"
        assert os.path.isfile(path), "Parquet file not created"

        # Read back and verify
        df = pd.read_parquet(path)
        assert len(df) >= 1

    def test_write_deduplicates(self, tmp_root):
        """同一trade_date重复写入，保留最新一条"""
        writer = ParquetWriter(tmp_root)

        for i in range(3):
            writer.write({
                "trade_date": 20200102,
                "symbol": "000001.SZ",
                "open": 16.0,
                "high": 16.5,
                "low": 15.8,
                "close": 16.2 + i * 0.01,
                "amount": 1000000.0,
                "volume": 6200,
            })

        df = pd.read_parquet(f"{tmp_root}/stock/000001.SZ/2020.parquet")
        # Only one record for trade_date 20200102
        count = len(df[df["trade_date"] == 20200102])
        assert count == 1, f"Expected 1, got {count}"

    def test_write_dataframe(self, tmp_root):
        """write_dataframe批量写入"""
        writer = ParquetWriter(tmp_root)

        rows = []
        for d in range(20200102, 20200105):
            rows.append({
                "trade_date": d,
                "symbol": "000001.SZ",
                "open": 16.0,
                "high": 16.5,
                "low": 15.8,
                "close": 16.2,
                "amount": 1000000.0,
                "volume": 6200,
            })

        df = pd.DataFrame(rows, columns=PARQUET_COLS).astype(DTYPES)
        writer.write_dataframe(df)

        path = f"{tmp_root}/stock/000001.SZ/2020.parquet"
        result = pd.read_parquet(path)
        assert len(result) >= 3

    def test_year_partition(self, tmp_root):
        """不同年份写入不同文件"""
        writer = ParquetWriter(tmp_root)

        writer.write({
            "trade_date": 20190102,
            "symbol": "000001.SZ",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "amount": 500000.0,
            "volume": 5000,
        })
        writer.write({
            "trade_date": 20200102,
            "symbol": "000001.SZ",
            "open": 16.0,
            "high": 16.5,
            "low": 15.8,
            "close": 16.2,
            "amount": 1000000.0,
            "volume": 6200,
        })

        assert os.path.isfile(f"{tmp_root}/stock/000001.SZ/2019.parquet")
        assert os.path.isfile(f"{tmp_root}/stock/000001.SZ/2020.parquet")
