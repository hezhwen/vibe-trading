---
name: a-share/sector-rotation
description: A股板块轮动——基于申万行业分类的资金流向、动量排名、情绪周期判断，输出板块超配/低配建议
category: asset-class
trigger:
  when:
    - "板块轮动" or "主线" or "板块切换" or "什么板块最近最强"
    - "板块资金流向" or "主线确认"
    - "现在该配置什么行业"
  skip:
    - user mentions 美股 or 港股板块
---

# A股板块轮动分析

## 概述

基于申万行业分类体系，通过资金流向、动量排名、情绪周期四个维度进行板块轮动分析。

## 核心指标

### 板块资金流向（成交额占比）

```python
# 数据来源：get_sector_flow tool
# 主线确认阈值（初始值，待 walk-forward 验证）
MAIN_BOARD_THRESHOLDS = {
    "limit_up_count_min": 5,
    "turnover_share_min": 0.15,
}
```

### 情绪周期判断

| 状态 | 条件 | 仓位建议 |
|------|------|---------|
| 主升浪 | 涨停家数≥20 且主线明确 | 80-100% |
| 轮动期 | 涨停10-20，主线退潮 | 40-60% |
| 退潮期 | 涨停5-10，跌停增加 | 0-20% |
| 冰点 | 涨停<5 或 跌停>30 | 空仓 |

### 状态转换规则

```
冰点 → 轮动期：连续3日涨停家数≥10
冰点 → 主升浪：连续5日涨停家数≥20 或 北向连续净流入>10亿
轮动期 → 主升浪：主线成交额占比>均值×2.0 且 ≥5只连板
轮动期 → 退潮期：主线涨停家数连续2日下降
主升浪 → 退潮期：连续2日炸板 或 北向净流出>20亿/日
退潮期 → 冰点：连续3日涨停家数<5 或 跌停家数>30
```

## 工具调用

1. `get_sector_flow(trade_date, top_n=20)` — 获取板块成交额排名
2. `get_limit_up_stocks(trade_date, market="all")` — 获取涨停股分布
3. `get_north_flow(trade_date)` — 北向资金（判断主线）

## 信号输出

```python
{
    "signal": "overweight" | "neutral" | "underweight",
    "main_sectors": ["半导体", "白酒", "光伏"],
    "momentum_rank": [("半导体", 1), ("白酒", 2)],
    "risk_sectors": ["房地产", "教育"],
    "confidence": 0.0-1.0,
    "ice_point": true/false,
    "limit_up_count": 15,
    "total_amount": 850000000000,
    "note": "⚠️ 阈值未经 walk-forward 验证"
}
```

## 注意事项

- 概念板块（AI/算力/低空经济）是动态增删的，只能用于近1-2年回测
- 申万一级行业可用于5年以上长周期回测
- 所有阈值未经 walk-forward 验证，⚠️ 绩效打折扣
