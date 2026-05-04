---
name: a-share/limit-up
description: A股涨停板分析——基于日K的涨停选股策略，输出首板/连板/炸板信号（含日K局限性披露）
category: strategy
trigger:
  when:
    - "涨停" and not ("不要涨停" or "不做涨停")
    - "打板" or "首板" or "连板" or "炸板"
    - "龙虎榜" or "龙头股"
  skip:
    - user mentions 美股 or 港股
---

# A股涨停板策略（日K近似版）

## ⚠️ 日K局限性（必须披露）

| 类型 | 日K判断 | 实际情况 |
|------|--------|---------|
| 全天封死 | close=涨停价 | 可能是尾盘偷袭 |
| 炸板回封 | close=涨停价 | 无法识别盘中炸板 |
| 首板 | 过去20日首次涨停 | 历史涨停不含退市股 |

**绩效折扣**：回测收益应打 30-50% 再对外展示。

## 涨停判断

```
涨停判断：close ≈ prev_close × (1 + limit_pct)
limit_pct: 主板10%, 科创/创业20%, ST 5%, 北交所30%

首板（近似）：
  1. 今日收盘价 = 涨停价
  2. 过去20日未曾触及涨停
  3. 上市 > 20 日
  4. 非 ST/*ST

连板（近似）：
  连续 N 日收盘价 = 涨停价（N ≥ 2）

炸板：收盘涨停但未封住 → 日K无法识别
```

## 封板强度（必须过滤弱封板）

```
封板强度 = amount / (float_cap × limit_pct)

阈值：
  < 0.03 → 极弱，拒绝
  < 0.05 → 偏弱，保守拒绝
  ≥ 0.05 → 正常
```

## 工具调用

1. `get_limit_up_stocks(trade_date, market="all")` — 获取涨停股列表
2. `get_st_status(symbol, trade_date)` — 检查是否ST
3. `get_suspended(symbol)` — 检查是否停牌

## 信号输出

```python
{
    "signal": "long",
    "type": "limit_up",
    "subtype": "first_board" | "continuous_board" | "blast_board",
    "confidence": 0.0-0.7,   # ⚠️ 最高0.7（折扣后）
    "seal_strength": 0.08,
    "notes": "基于日K近似，实际胜率应打折30-50%",
    "limit_up_count_total": 85,
    "main_sector": "科技"
}
```
