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
具体折扣取决于封板强度：弱封板(seal<0.05)折扣更高(40-50%)。

## 涨停判断

```
涨停判断：abs(close - prev_close * (1+limit_pct)) < 0.02
limit_pct: 主板10%, 科创/创业20%, ST 5%, 北交所30%

首板（近似）：
  1. 今日收盘价 ≈ 涨停价（误差0.02）
  2. 过去20日未曾触及涨停
  3. 上市 > 20 日
  4. 非 ST/*ST

连板（近似）：
  连续 N 日收盘价 ≈ 涨停价（N ≥ 2）

炸板：收盘涨停但未封住 → 日K无法识别
```

## 封板强度（必须过滤弱封板）

```
封板强度 = amount / (float_cap × limit_pct)

阈值：
  < 0.03 → 极弱，拒绝（绩效折扣50%）
  < 0.05 → 偏弱，保守拒绝（绩效折扣40%）
  ≥ 0.05 → 正常（绩效折扣30%）
```

⚠️ 注意：因日K无法区分"全天封板"和"尾盘偷袭"，建议涨停板策略confidence不超过0.7。

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
    "confidence": 0.0-0.7,   # ⚠️ 最高0.7
    "seal_strength": 0.08,
    "performance_discount": "30-50%",  # 绩效折扣
    "notes": "基于日K近似，实际胜率应打折30-50%",
    "limit_up_count_total": 85,
    "main_sector": "科技"
}
```

## 注意事项

- 退市股偏差：历史涨停记录不含退市股，首板/连板识别存在偏差
- 封板强度必须用 `amount / (float_cap × limit_pct)` 过滤弱封板
- 换手率极值：新股/摘帽首日换手率 > 30%，计算时应剔除
- ⚠️ 涨停板策略confidence上限0.7（折扣后），不适合单独使用
