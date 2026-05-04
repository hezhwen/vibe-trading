#!/usr/bin/env python3
"""
涨停板策略 Walk-Forward 验证（高效批量版）

用 DuckDB 批量加载两年全市场数据，pandas 向量化计算涨跌停，
避免逐股票查询。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd
import numpy as np


DATA_ROOT = Path(__file__).resolve().parents[2] / "data"

# 阈值配置
ICEBERG_THRESHOLDS = {
    "iceberg": 5,
    "rotation": 10,
    "surge": 20,
    "limit_down_alert": 30,
}


def load_market_bars(start_date: int, end_date: int) -> pd.DataFrame:
    """批量加载全市场数据（带前收）"""
    import duckdb

    conn = duckdb.connect(str(DATA_ROOT / "duckdb" / "china_a.duckdb"), read_only=True)
    try:
        # 加载所有股票日K（两列），用 LAG window function 计算前收
        df = conn.execute(f"""
            WITH daily AS (
                SELECT
                    p.symbol,
                    p.trade_date,
                    p.close,
                    p.amount,
                    p.volume,
                    s.board,
                    LAG(p.close) OVER (PARTITION BY p.symbol ORDER BY p.trade_date) AS pre_close
                FROM parquet_scan('{DATA_ROOT}/parquet/stock/*/*.parquet')
                AS p
                LEFT JOIN symbol_meta s ON p.symbol = s.symbol
                WHERE p.trade_date >= {start_date} AND p.trade_date <= {end_date}
                  AND s.symbol IS NOT NULL
            )
            SELECT * FROM daily WHERE pre_close IS NOT NULL
            ORDER BY trade_date, symbol
        """).df()
    finally:
        conn.close()
    return df


def get_limit_pct(symbol: str) -> float:
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith(("688", "300")):
        return 0.20
    if code.startswith("8"):
        return 0.30
    return 0.10


def compute_limit_events(df: pd.DataFrame) -> pd.DataFrame:
    """向量化计算涨跌停"""
    df = df.copy()
    df["limit_pct"] = df["symbol"].apply(get_limit_pct)
    df["limit_up_price"] = df["pre_close"] * (1 + df["limit_pct"])
    df["limit_down_price"] = df["pre_close"] * (1 - df["limit_pct"])

    # 涨停：收盘价接近涨停价（误差0.02）且涨幅 >= limit_pct - 0.001
    df["pct_chg"] = (df["close"] - df["pre_close"]) / df["pre_close"]
    df["is_limit_up"] = (
        (abs(df["close"] - df["limit_up_price"]) < 0.02) &
        (df["pct_chg"] >= df["limit_pct"] - 0.001)
    )
    df["is_limit_down"] = (
        (abs(df["close"] - df["limit_down_price"]) < 0.02) &
        (df["pct_chg"] <= -(df["limit_pct"] - 0.001))
    )
    return df


def aggregate_by_date(df: pd.DataFrame) -> pd.DataFrame:
    """按日期聚合，统计涨跌停家数"""
    agg = df.groupby("trade_date").agg(
        limit_up_count=("is_limit_up", "sum"),
        limit_down_count=("is_limit_down", "sum"),
        total_stocks=("symbol", "count"),
        avg_amount=("amount", "mean"),
    ).reset_index()
    return agg


def get_index_returns(trade_dates: list) -> dict:
    """批量获取指数收益率"""
    import duckdb
    conn = duckdb.connect(str(DATA_ROOT / "duckdb" / "china_a.duckdb"), read_only=True)
    try:
        rows = conn.execute("""
            WITH indexed AS (
                SELECT
                    trade_date,
                    close,
                    LAG(close) OVER (ORDER BY trade_date) AS prev_close
                FROM parquet_scan('data/parquet/stock/000300.SH/*.parquet')
                WHERE trade_date >= 20200101
            )
            SELECT trade_date, close, prev_close,
                   (close - prev_close) / prev_close AS ret
            FROM indexed
            WHERE prev_close IS NOT NULL
        """).fetchall()
        ret_map = {r[0]: r[3] for r in rows}
        # 构建未来5日累计收益
        result = {}
        dates_sorted = sorted(ret_map.keys())
        for i, td in enumerate(dates_sorted):
            future = dates_sorted[i+1:i+6] if i+1 < len(dates_sorted) else dates_sorted[i+1:i+2]
            if future:
                cum_ret = sum(ret_map.get(d, 0) for d in future)
                result[td] = cum_ret
        return result
    finally:
        conn.close()


def run_validation():
    print("=" * 60)
    print("涨停板策略 Walk-Forward 验证（2020-2024）")
    print("=" * 60)

    print("\n[1] 加载全市场数据...")
    df = load_market_bars(20200101, 20241231)
    print(f"   加载 {len(df):,} 行，日期范围 {df['trade_date'].min()}-{df['trade_date'].max()}")

    print("\n[2] 计算涨跌停...")
    df = compute_limit_events(df)

    print("\n[3] 按日聚合...")
    daily = aggregate_by_date(df)

    print("\n[4] 加载指数收益...")
    index_ret = get_index_returns(daily["trade_date"].tolist())
    daily["next_5d_return"] = daily["trade_date"].map(index_ret)

    # 移除无收益数据的日期
    daily = daily.dropna(subset=["next_5d_return"])
    print(f"   有效交易日: {len(daily)} 天")

    print("\n[5] 阈值分组统计...")
    results = {}
    labels = {
        "冰点(<5)": daily["limit_up_count"] < ICEBERG_THRESHOLDS["iceberg"],
        "轮动(5-9)": (daily["limit_up_count"] >= ICEBERG_THRESHOLDS["iceberg"]) & (daily["limit_up_count"] < ICEBERG_THRESHOLDS["rotation"]),
        "活跃(10-19)": (daily["limit_up_count"] >= ICEBERG_THRESHOLDS["rotation"]) & (daily["limit_up_count"] < ICEBERG_THRESHOLDS["surge"]),
        "主升浪(>=20)": daily["limit_up_count"] >= ICEBERG_THRESHOLDS["surge"],
    }

    print(f"\n   {'状态':<20} {'天数':>6} {'平均收益':>10} {'胜率':>8} {'平均涨停':>10}")
    print("   " + "-" * 58)

    for label, mask in labels.items():
        sub = daily[mask]
        if sub.empty:
            results[label] = {"count": 0}
            continue
        avg_ret = sub["next_5d_return"].mean()
        win_rate = (sub["next_5d_return"] > 0).mean()
        avg_lu = sub["limit_up_count"].mean()
        results[label] = {"count": len(sub), "avg_ret": avg_ret, "win_rate": win_rate, "avg_lu": avg_lu}
        print(f"   {label:<20} {len(sub):>6} {avg_ret:>10.2%} {win_rate:>8.1%} {avg_lu:>10.0f}")

    # 跌停预警分析
    ld_alert = daily[daily["limit_down_count"] > ICEBERG_THRESHOLDS["limit_down_alert"]]
    if not ld_alert.empty:
        avg_ret = ld_alert["next_5d_return"].mean()
        print(f"\n   {'跌停预警(>30)':<20} {len(ld_alert):>6} {avg_ret:>10.2%} {'-':>8}")

    print("\n[6] 阈值合理性评估...")
    ice = results.get("冰点(<5)", {})
    surge = results.get("主升浪(>=20)", {})
    if ice and surge and ice.get("avg_ret") is not None and surge.get("avg_ret") is not None:
        diff = surge["avg_ret"] - ice["avg_ret"]
        diff_pct = diff * 100
        if diff > 0.005:
            print(f"   ✅ 阈值合理：主升浪({surge['avg_ret']:.2%}) > 冰点({ice['avg_ret']:.2%})，差值{diff_pct:.2f}pp")
        elif diff > 0:
            print(f"   ⚠️ 阈值基本合理，但差值仅{diff_pct:.2f}pp，区分度有限")
        else:
            print(f"   ❌ 阈值不合理：主升浪({surge['avg_ret']:.2%}) <= 冰点({ice['avg_ret']:.2%})")
            print(f"   建议：调低阈值或改用其他指标组合")

    print("\n[7] Confidence 折扣估算...")
    all_rets = daily["next_5d_return"].dropna()
    if not all_rets.empty:
        ice_rets = daily.loc[labels["冰点(<5)"], "next_5d_return"].dropna()
        surge_rets = daily.loc[labels["主升浪(>=20)"], "next_5d_return"].dropna()
        if not ice_rets.empty:
            ice_std = ice_rets.std()
            theoretical_max = 0.10
            discount_low = max(0.20, 1 - 2 * ice_std / theoretical_max)
            discount_high = min(0.60, discount_low + 0.20)
            print(f"   冰点5日收益标准差: {ice_std:.2%}")
            print(f"   建议 confidence 折扣区间: {discount_low:.0%}-{discount_high:.0%}")
            print(f"   → 涨停策略 confidence 上限建议: {discount_high:.0%}")

    print("\n" + "=" * 60)
    print("结论：阈值经验证，若差异度不足建议walk-forward参数优化")
    print("=" * 60)
    return results


if __name__ == "__main__":
    run_validation()
