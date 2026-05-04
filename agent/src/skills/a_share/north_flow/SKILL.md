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

## ⚠️ Walk-Forward 验证注意事项

**⚠️ 北向资金数据状态**：
- DuckDB `north_flow_industry` 表当前为空（待从 AKShare 填充）
- live 模式可调用 AKShare，但仅支持近期数据
- 回测模式需要先通过 AKShare 填充历史数据

**⚠️ T+1 数据约束**：
- 2024年5月起北向资金盘中停止披露，所有"今日"数据均为T-1收盘数据
- 回测时需使用前一日的北向数据

## 核心概念

### Stock Connect 架构

| 渠道 | 方向 | 含义 |
|------|------|------|
| 沪股通北向 | 外资 → A股(沪市) | 境外机构配置沪市 |
| 深股通北向 | 外资 → A股(深市) | 境外机构配置深市（成长偏好）|
| 南向（港股通）| 境内 → HK | 境内投资者配置港股 |

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
```

### 拐点识别

```
拐点信号（卖出参考）：
  - 连续5日净买入后首次流出 → 可能反转
  - A股不能做空：看空信号仅用于减仓
```

## 工具调用

1. `get_north_flow(trade_date)` — 获取北向资金汇总（⚠️ T+1数据）

## 信号输出

```python
{
    "signal": "bullish" | "neutral" | "bearish",
    "total_net_inflow": 8.5,       # 亿元
    "cum_20d_net": 120,            # 20日累计亿元
    "cum_5d_net": 15,
    "trend": "sustained_accumulation",
    "sh_preference": 0.6,
    "sz_preference": 0.4,
    "top_sectors": ["白酒", "银行"],
    "inflection_point": false,
    "confidence": 0.0-1.0,
    "note": "⚠️ T+1数据；需walk-forward验证择时效果"
}
```

## 注意事项

- ⚠️ 阈值（5亿/10亿/20亿）未经 walk-forward 验证
- ⚠️ 北向资金数据需先填充 DuckDB 才能用于历史回测
- 外资配置偏好（消费/金融/科技）是长期结构，非择时信号
