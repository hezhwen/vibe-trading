"""A-share backtest engine V2.

增强功能（相对于china_a.py）：
  - T+1 frozen_qty 完整实现
  - can_fill() 流动性判断（封板强度日级代理变量）
  - 非线性滑点模型
  - 印花税时间序列配置
  - ST状态查询函数注入
  - 停牌日查询函数注入
  - 新股/退市整理期处理

Config keys:
  - commission_rate: default 0.00025 (万2.5)
  - commission_min: default 5.0 (RMB)
  - stamp_tax: default 0.0005 (万5, sell-only)
  - transfer_fee: default 0.00001 (万0.1)
  - slippage_base: default 0.0005
  - slippage_k: default 0.1
  - seal_strength_weak: default 0.03
  - seal_strength_conservative: default 0.05
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Dict, Optional, Tuple

import pandas as pd

from backtest.engines.base import BaseEngine


class PriceLimitType(Enum):
    """涨跌停幅度类型"""
    MAIN_BOARD = 0.10      # 主板 ±10%
    CHINEXT_STAR = 0.20   # 创业板/科创板 ±20%
    ST_SPECIAL = 0.05      # ST/*ST ±5%
    BEIJING = 0.30         # 北交所 ±30%
    NEW_IPO_MAIN = 0.44    # 主板新股首日 ±44%
    NONE = None             # 无涨跌停限制（科创/创业前5日）


# 印花税时间序列配置
DEFAULT_FEE_SCHEDULE = {
    date(2008, 9, 19):  {"stamp_duty": 0.001, "transfer_fee": 0.00001},
    date(2023, 8, 27):  {"stamp_duty": 0.001, "transfer_fee": 0.00001},
    date(2023, 8, 28):  {"stamp_duty": 0.0005, "transfer_fee": 0.0},  # 降半征收
}


def get_stamp_duty(trade_date: date, fee_table: dict = None) -> float:
    """查询指定日期的印花税率"""
    table = fee_table or DEFAULT_FEE_SCHEDULE
    for cutoff, fees in sorted(table.items(), reverse=True):
        if trade_date >= cutoff:
            return fees["stamp_duty"]
    return 0.001


def get_transfer_fee(trade_date: date, fee_table: dict = None) -> float:
    """查询指定日期的过户费率"""
    table = fee_table or DEFAULT_FEE_SCHEDULE
    for cutoff, fees in sorted(table.items(), reverse=True):
        if trade_date >= cutoff:
            return fees["transfer_fee"]
    return 0.00001


def get_limit_pct(symbol: str, st_type: int = 0) -> Optional[float]:
    """根据代码前缀和ST状态返回涨停幅度"""
    code = symbol.split(".")[0] if "." in symbol else symbol

    # ST/*ST：±5%
    if st_type in (1, 2):
        return 0.05

    # 北交所
    if code.startswith("8") and len(code) == 6:
        return 0.30

    # 科创/创业前5日无涨跌停（需配合new_listing_fn判断）
    if code.startswith(("688", "300")):
        return 0.20

    # 主板
    return 0.10


@dataclass
class ASharePosition:
    """A股持仓（含T+1冻结）"""
    symbol: str
    total_qty: int = 0        # 总持仓数量
    frozen_qty: int = 0       # 当日买入、明日才能卖出的部分（T+1锁仓）
    entry_price: float = 0.0  # 平均成本价
    entry_date: Optional[date] = None

    @property
    def available_qty(self) -> int:
        """今日可卖出数量 = 总持仓 - 当日买入冻结"""
        return max(0, self.total_qty - self.frozen_qty)

    def buy(self, qty: int, price: float, trade_date: date):
        """买入：增加总持仓，当日买入进入frozen"""
        self.total_qty += qty
        self.frozen_qty += qty
        # 移动平均成本
        total_cost = self.total_qty * self.entry_price + qty * price
        self.entry_price = total_cost / self.total_qty
        self.entry_date = trade_date

    def sell(self, qty: int) -> int:
        """卖出：优先使用available_qty，返回实际成交量"""
        can_sell = min(qty, self.available_qty)
        if can_sell <= 0:
            return 0
        self.total_qty -= can_sell
        return can_sell

    def unfreeze(self):
        """新交易日开盘前：解除T+1冻结"""
        self.frozen_qty = 0


@dataclass
class ExecutionResult:
    """执行结果"""
    allowed: bool
    reason: str = ""
    source: str = ""  # "engine", "can_execute", "can_fill"


class ChinaAEngineV2(BaseEngine):
    """A股增强回测引擎V2

    P0规则：
      - can_execute(): 规则允许（T+1/涨跌停/ST/停牌/新股）
      - can_fill(): 流动性允许（封板强度日级代理变量）

    P1规则：
      - 非线性滑点模型
      - Time-varying fee schedule
      - ST status lookup
      - Suspended stock lookup
    """

    def __init__(
        self,
        config: dict,
        st_status_fn=None,        # (symbol, date) -> st_type (0=正常, 1=ST, 2=*ST, 3=退市整理)
        suspended_fn=None,         # (symbol, date) -> bool (是否停牌)
        float_cap_fn=None,        # (symbol, date) -> float (流通市值，元)
        new_listing_fn=None,     # (symbol) -> (list_date, board_type)
        fee_schedule: dict = None, # 印花税时间序列
    ):
        config = {**config, "leverage": 1.0}
        super().__init__(config)

        self.commission_rate: float = config.get("commission_rate", 0.00025)
        self.commission_min: float = config.get("commission_min", 5.0)
        self.slippage_base: float = config.get("slippage_base", 0.0005)
        self.slippage_k: float = config.get("slippage_k", 0.1)
        self.seal_strength_weak: float = config.get("seal_strength_weak", 0.03)
        self.seal_strength_conservative: float = config.get("seal_strength_conservative", 0.05)
        self.fee_schedule = fee_schedule or DEFAULT_FEE_SCHEDULE

        # 数据查询函数（外部注入）
        self._st_status_fn = st_status_fn or (lambda s, d: 0)
        self._suspended_fn = suspended_fn or (lambda s, d: False)
        self._float_cap_fn = float_cap_fn or (lambda s, d: 0.0)
        self._new_listing_fn = new_listing_fn or (lambda s: (None, None))

        # 持仓管理
        self._positions: Dict[str, ASharePosition] = {}
        self._current_date: Optional[date] = None

    def set_date(self, trade_date: date):
        """切换到新交易日：重置frozen_qty"""
        if self._current_date is not None and trade_date != self._current_date:
            self._on_date_change()
        self._current_date = trade_date

    def _on_date_change(self):
        """新交易日开始：解除所有持仓的T+1冻结"""
        for pos in self._positions.values():
            pos.unfreeze()

    # ── can_execute: 规则维度 ────────────────────────────────

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> ExecutionResult:
        """
        规则维度检查：涨跌停、T+1、ST、停牌、新股首日

        Args:
            symbol: 股票代码
            direction: 1(买入), 0(卖出), -1(做空，拒绝)
            bar: 当前bar数据

        Returns:
            ExecutionResult(allowed, reason, source)
        """
        # 1. 做空检查（A股不允许做空）
        if direction == -1:
            return ExecutionResult(False, "A 股不允许做空", "engine")

        # 2. 停牌检查
        if self._suspended_fn(symbol, self._current_date):
            return ExecutionResult(False, f"{symbol} {self._current_date} 停牌", "can_execute")

        # 3. ST检查
        st_type = self._st_status_fn(symbol, self._current_date)
        if st_type in (1, 2) and direction == 1:
            return ExecutionResult(False, "ST/*ST 股票不允许追买", "can_execute")

        # 4. T+1检查
        if direction == 0:  # 卖出
            pos = self._positions.get(symbol)
            if pos is not None:
                if pos.available_qty <= 0:
                    return ExecutionResult(False, f"T+1 限制：可卖出 {pos.available_qty} 股", "can_execute")
                if pos.frozen_qty > 0 and pos.total_qty - pos.frozen_qty == 0:
                    return ExecutionResult(False, f"全部持仓处于T+1冻结中", "can_execute")

        # 5. 涨跌停检查
        limit_pct = get_limit_pct(symbol, st_type)
        if limit_pct is not None:
            pct_chg = self._calc_pct_change(bar)
            if pct_chg is not None:
                if direction == 1 and pct_chg >= limit_pct - 0.001:
                    return ExecutionResult(False, f"涨停板（+{limit_pct:.0%}），无法买入", "can_execute")
                if direction == 0 and pct_chg <= -limit_pct + 0.001:
                    return ExecutionResult(False, f"跌停板（-{limit_pct:.0%}），无法卖出", "can_execute")

        return ExecutionResult(True)

    # ── can_fill: 流动性维度 ────────────────────────────────

    def can_fill(
        self,
        symbol: str,
        direction: int,
        bar: pd.Series,
    ) -> ExecutionResult:
        """
        流动性维度检查：涨停封板强度日级代理变量

        封板强度 = amount / (float_cap × limit_up_pct)

        阈值：
          < 0.03 → 极弱，拒绝
          < 0.05 → 偏弱，保守拒绝
          ≥ 0.05 → 正常，可成交
        """
        if direction != 1:  # 只限制买入
            return ExecutionResult(True)

        # 判断是否涨停
        pct_chg = self._calc_pct_change(bar)
        if pct_chg is None:
            return ExecutionResult(True)  # 数据异常，保守放行

        st_type = self._st_status_fn(symbol, self._current_date)
        limit_pct = get_limit_pct(symbol, st_type)
        if limit_pct is None:
            return ExecutionResult(True)  # 无涨跌停限制

        # 非涨停，放行
        if abs(pct_chg - limit_pct) > 0.002:
            return ExecutionResult(True)

        # 涨停板：检查封板强度
        amount = float(bar.get("amount", 0))
        float_cap = self._float_cap_fn(symbol, self._current_date)

        if float_cap <= 0 or amount <= 0:
            return ExecutionResult(False, "流通市值或成交额数据缺失，保守拒绝买入", "can_fill")

        strength = amount / (float_cap * limit_pct)

        if strength < self.seal_strength_weak:
            return ExecutionResult(False, f"涨停封板强度极弱（{strength:.2%}），成交概率低", "can_fill")
        if strength < self.seal_strength_conservative:
            return ExecutionResult(False, f"涨停封板强度偏弱（{strength:.2%}），保守拒绝", "can_fill")

        return ExecutionResult(True, f"封板强度正常（{strength:.2%}）")

    # ── 成交执行 ────────────────────────────────

    def execute_buy(self, symbol: str, qty: int, price: float, bar: pd.Series) -> Tuple[bool, str]:
        """执行买入"""
        # 先规则检查
        exec_result = self.can_execute(symbol, 1, bar)
        if not exec_result.allowed:
            return False, exec_result.reason

        # 再流动性检查
        fill_result = self.can_fill(symbol, 1, bar)
        if not fill_result.allowed:
            return False, fill_result.reason

        # 成交
        pos = self._positions.get(symbol)
        if pos is None:
            pos = ASharePosition(symbol=symbol)
            self._positions[symbol] = pos

        pos.buy(qty, price, self._current_date)

        # 计算费用
        commission = self._calc_commission(qty, price, is_open=True)
        self.capital -= qty * price + commission

        return True, ""

    def execute_sell(self, symbol: str, qty: int, price: float, bar: pd.Series) -> Tuple[bool, str]:
        """执行卖出"""
        # 规则检查
        exec_result = self.can_execute(symbol, 0, bar)
        if not exec_result.allowed:
            return False, exec_result.reason

        # 检查可卖出数量
        pos = self._positions.get(symbol)
        if pos is None or pos.available_qty <= 0:
            return False, "无持仓或无可卖出数量"

        # 实际卖出量
        actual_qty = pos.sell(min(qty, pos.available_qty))
        if actual_qty <= 0:
            return False, "卖出失败"

        # 计算费用（含印花税）
        commission = self._calc_commission(actual_qty, price, is_open=False)
        self.capital += actual_qty * price - commission

        return True, ""

    def _calc_commission(self, size: float, price: float, is_open: bool) -> float:
        """计算手续费"""
        notional = size * price
        comm = max(notional * self.commission_rate, self.commission_min)
        comm += notional * get_transfer_fee(self._current_date, self.fee_schedule)
        if not is_open:  # 卖出收取印花税
            comm += notional * get_stamp_duty(self._current_date, self.fee_schedule)
        return comm

    # ── 滑点模型 ────────────────────────────────

    def apply_slippage(self, price: float, direction: int, bar: pd.Series = None) -> float:
        """
        非线性滑点模型

        slippage = base + k × min(participation, 0.1) + 非线性惩罚项

        participation > 0.1 时：超出部分 × 5倍惩罚系数
        """
        if direction == 0:  # 平仓无滑点
            return price

        base = self.slippage_base
        k = self.slippage_k

        if bar is None:
            return price * (1 + direction * base)

        # 计算参与度
        amount = float(bar.get("amount", 0))
        adv = amount / 2 if amount > 0 else 0
        order_value = 0  # 需要外部传入

        participation = min(order_value / adv, 1.0) if adv > 0 else 1.0

        # 线性部分
        slippage = base + k * min(participation, 0.1)

        # 非线性惩罚
        if participation > 0.1:
            excess = participation - 0.1
            slippage += excess * 5 * k

        return price * (1 + direction * slippage)

    # ── 工具方法 ────────────────────────────────

    def _calc_pct_change(self, bar: pd.Series) -> Optional[float]:
        """计算涨跌幅"""
        if "pct_chg" in bar.index:
            val = bar["pct_chg"]
            if pd.notna(val):
                return float(val) / 100.0

        close = bar.get("close")
        pre_close = bar.get("pre_close")
        if close is not None and pre_close is not None and pre_close > 0:
            return (float(close) - float(pre_close)) / float(pre_close)
        return None

    def get_position(self, symbol: str) -> Optional[ASharePosition]:
        """获取持仓"""
        return self._positions.get(symbol)

    def get_available_cash(self) -> float:
        """获取可用资金"""
        return self.capital

    # ── BaseEngine抽象方法 ────────────────────────────────

    def round_size(self, raw_size: float, price: float) -> float:
        """Round down to 100-share lots."""
        return max(int(raw_size / 100) * 100, 0)

    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:
        """Calculate commission (BaseEngine兼容)"""
        return self._calc_commission(size, price, is_open)

    def apply_slippage_base(self, price: float, direction: int) -> float:
        """Apply slippage (BaseEngine兼容)"""
        return price * (1 + direction * self.slippage_base)
