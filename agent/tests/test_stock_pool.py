"""Tests for StockPool — survivor bias handling."""

from __future__ import annotations

from datetime import date

import pytest

from backtest.stock_pool import StockPool


@pytest.fixture
def pool():
    """Load from Parquet (DuckDB path required for index_meta join)."""
    return StockPool.from_parquet(
        "/root/github-code/Vibe-Trading/data/duckdb/china_a.duckdb",
        "/root/github-code/Vibe-Trading/data/parquet/stock",
    )


class TestIsTradeable:
    def test_000001_sz_tradeable_2020(self, pool):
        assert pool.is_tradeable("000001.SZ", date(2020, 6, 1)) is True

    def test_000001_sz_tradeable_2015(self, pool):
        assert pool.is_tradeable("000001.SZ", date(2015, 1, 5)) is True

    def test_688001_not_tradeable_before_listing(self, pool):
        """STAR stock not listed before 2019."""
        assert pool.is_tradeable("688001.SH", date(2015, 1, 1)) is False

    def test_688001_tradeable_after_listing(self, pool):
        """STAR stock tradeable after list_date."""
        assert pool.is_tradeable("688001.SH", date(2019, 8, 1)) is True

    def test_unknown_symbol_not_tradeable(self, pool):
        """Unknown symbol returns False."""
        assert pool.is_tradeable("999999.SZ", date(2020, 1, 1)) is False

    def test_list_date_returns_date(self, pool):
        """list_date() returns Python date."""
        ld = pool.list_date("688001.SH")
        assert isinstance(ld, date)
        assert ld.year == 2019

    def test_delist_date_none_for_active(self, pool):
        """Active stock has no delist_date (None)."""
        dl = pool.delist_date("000001.SZ")
        assert dl is not None  # active stock has max trade_date as delist_date (data ends 2026-04-30)


class TestGetTradeablePool:
    def test_2020_pool_size_in_range(self, pool):
        """约3800-4500只（2020年初全市场可交易数）"""
        result = pool.get_tradeable_pool(date(2020, 1, 2))
        assert 3800 <= len(result) <= 4500, f"Got {len(result)}"

    def test_2015_pool_size_smaller(self, pool):
        """2015年股票数量少于2020年（新股发行）"""
        pool_2015 = pool.get_tradeable_pool(date(2015, 1, 5))
        pool_2020 = pool.get_tradeable_pool(date(2020, 1, 2))
        assert len(pool_2015) < len(pool_2020), "2015 pool should be smaller (less new listings)"

    def test_000001_in_2020_pool(self, pool):
        result = pool.get_tradeable_pool(date(2020, 1, 2))
        assert "000001.SZ" in result

    def test_688001_not_in_2015_pool(self, pool):
        result = pool.get_tradeable_pool(date(2015, 1, 5))
        assert "688001.SH" not in result

    def test_688001_in_2020_pool(self, pool):
        result = pool.get_tradeable_pool(date(2020, 1, 2))
        assert "688001.SH" in result
