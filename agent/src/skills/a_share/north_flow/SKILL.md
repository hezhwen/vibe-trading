---
name: a-share/north-flow
description: 北向资金（沪深港通）分析——外资买卖A股的择时信号、板块配置、拐点识别
category: flow
trigger:
  when:
    - "北向资金" or "外资" or "北上" or "沪深港通"
    - "外资买了什么" or "北向流入" or "北向流出"
    - "外资怎么看A股"
  skip:
    - user mentions 美股 or 港股外资
---

# 北向资金分析

## 概述

分析通过沪深港通渠道进入A股的境外资金，作为市场情绪的同步/领先指标。

## 核心概念

### Stock Connect 架构

| 渠道 | 方向 | 含义 |
|------|------|------|
| 沪股通北向 | 外资 → A股(沪市) | 境外机构配置沪市 |
| 深股通北向 | 外资 → A股(深市) | 境外机构配置深市（成长偏好）|
| 沪市港股通南向 | 境内 → HK(沪) | 境内投资者配置港股 |
| 深市港股通南向 | 境内 → HK(深) | 境内投资者配置港股 |

### 信号框架

```python
def northbound_signal(daily_net_buy_billion):
    if daily_net_buy_billion > 10:
        return "strong_foreign_buying"
    elif daily_net_buy_billion > 5:
        return "moderate_foreign_buying"
    elif daily_net_buy_billion > 0:
        return "mild_foreign_buying"
    elif daily_net_buy_billion > -5:
        return "mild_foreign_selling"
    elif daily_net_buy_billion > -10:
        return "moderate_foreign_selling"
    else:
        return "strong_foreign_selling"  # Panic outflow

def northbound_trend(cum_20d, cum_5d):
    if cum_20d > 30 and cum_5d > 10:
        return "sustained_accumulation"   # Strong bullish
    elif cum_20d < -30 and cum_5d < -10:
        return "sustained_distribution"   # Bearish
    elif cum_20d > 0 and cum_5d < 0:
        return "accumulation_pausing"     # Watch for reversal
    elif cum_20d < 0 and cum_5d > 0:
        return "distribution_pausing"     # Possible bottom formation
```

### 拐点识别

```
拐点信号（强烈卖出参考）：
  - 连续5日净买入后首次流出 → 可能反转
  - 某日突然大幅流出 (>30亿) 但基本面无变化 → 假拐点，观察

A 股不能做空：看空信号仅用于减仓，不做空
```

## 重要约束

⚠️ **T+1 数据延迟**：2024年5月起北向资金盘中停止披露，所有"今日"数据均为T-1收盘数据。

## 工具调用

1. `get_north_flow(trade_date)` — 获取北向资金汇总（净流入/沪股通/深股通）

## 信号输出

```python
{
    "signal": "bullish" | "neutral" | "bearish",
    "total_net_inflow": 8.5,          # 亿元
    "cum_20d_net": 120,               # 20日累计亿元
    "cum_5d_net": 15,
    "trend": "sustained_accumulation",
    "sh_preference": 0.6,              # 沪股通占比
    "sz_preference": 0.4,             # 深股通占比
    "top_sectors": ["白酒", "银行", "电力设备"],
    "inflection_point": false,        # 拐点信号
    "confidence": 0.0-1.0,
    "note": "⚠️ T+1数据，仅供参考"
}
```
