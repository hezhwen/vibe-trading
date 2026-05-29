"""Order Management System.

State machine: PENDING -> SUBMITTED -> ACKNOWLEDGED -> PARTIAL -> FILLED
Any non-terminal state can transition to CANCELLED or REJECTED.

Every state transition writes a WAL entry for crash recovery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OrderState(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# Valid state transitions
VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.PENDING: {OrderState.SUBMITTED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.SUBMITTED: {OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED,
                           OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.ACKNOWLEDGED: {OrderState.PARTIAL, OrderState.FILLED,
                              OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.PARTIAL: {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.FILLED: set(),       # Terminal
    OrderState.CANCELLED: set(),    # Terminal
    OrderState.REJECTED: set(),     # Terminal
}


@dataclass
class OrderRecord:
    """Internal order representation in the OMS."""
    order_id: str
    broker_order_id: str = ""
    symbol: str = ""
    side: str = "buy"
    qty: int = 0
    price: float = 0.0
    order_type: str = "limit"       # "limit" | "market"
    status: str = "pending"
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    strategy_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "order_type": self.order_type,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "commission": self.commission,
            "strategy_id": self.strategy_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OrderRecord":
        return cls(**{k: d.get(k, "") for k in [
            "order_id", "broker_order_id", "symbol", "side", "qty", "price",
            "order_type", "status", "filled_qty", "avg_fill_price", "commission",
            "strategy_id", "created_at", "updated_at", "error_message",
        ]})


class OrderManager:
    """OMS: order state machine + routing + WAL persistence."""

    def __init__(
        self,
        broker=None,  # BrokerAdapter
        risk_engine=None,  # RiskEngine
        checkpoint=None,  # CheckpointManager
        position_book=None,  # PositionBook
    ):
        self._broker = broker
        self._risk = risk_engine
        self._checkpoint = checkpoint
        self._position_book = position_book
        self._orders: dict[str, OrderRecord] = {}

    # ── Order lifecycle ────────────────────────────────────────

    def create_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float = 0.0,
        order_type: str = "limit",
        strategy_id: str = "",
    ) -> OrderRecord:
        """Create a new order in PENDING state."""
        order_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        # Lot rounding for A-shares
        qty = max(qty // 100 * 100, 100)

        order = OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_type=order_type,
            status="pending",
            strategy_id=strategy_id,
            created_at=now,
            updated_at=now,
        )
        self._orders[order_id] = order
        self._write_wal("order_create", order.to_dict())
        return order

    def submit_order(self, order_id: str) -> OrderRecord:
        """Submit order to broker. Transitions PENDING -> SUBMITTED.

        Risk checks are performed inline before submission.
        """
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")

        if not self._can_transition(order, OrderState.SUBMITTED):
            raise ValueError(f"Cannot submit order in state {order.status}")

        # Risk check (inline, synchronous)
        if self._risk is not None:
            allowed, reason = self._risk.pre_trade_check(
                order.symbol, order.side, order.qty, order.price
            )
            if not allowed:
                return self._reject(order_id, reason)

        # Submit to broker
        if self._broker is not None:
            try:
                broker_order = self._broker.submit_order(
                    order.symbol, order.side, order.qty,
                    order.price, order.order_type,
                )
                order.broker_order_id = broker_order.broker_order_id
            except Exception as e:
                return self._reject(order_id, str(e))

        return self._transition(order_id, OrderState.SUBMITTED)

    def cancel_order(self, order_id: str) -> OrderRecord:
        """Cancel an order."""
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")

        current = OrderState(order.status)
        if current in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED):
            raise ValueError(f"Cannot cancel order in terminal state {order.status}")

        # Cancel at broker
        if self._broker is not None and order.broker_order_id:
            try:
                self._broker.cancel_order(order.broker_order_id)
            except Exception:
                pass

        return self._transition(order_id, OrderState.CANCELLED)

    def on_fill(self, broker_order_id: str, fill_price: float, fill_qty: int) -> OrderRecord | None:
        """Handle a fill notification from the broker."""
        # Find the local order by broker_order_id
        order = None
        for o in self._orders.values():
            if o.broker_order_id == broker_order_id:
                order = o
                break
        if order is None:
            return None

        now = datetime.now().isoformat()
        order.filled_qty += fill_qty
        order.avg_fill_price = (
            (order.avg_fill_price * (order.filled_qty - fill_qty) + fill_price * fill_qty)
            / order.filled_qty
        )
        order.updated_at = now

        if order.filled_qty >= order.qty:
            self._transition(order.order_id, OrderState.FILLED)
        else:
            self._transition(order.order_id, OrderState.PARTIAL)

        # Update position book
        if self._position_book is not None:
            commission = 0.0
            if hasattr(self._position_book, '_calc_commission'):
                pass  # commission handled elsewhere
            try:
                self._position_book.on_fill(
                    symbol=order.symbol,
                    side=order.side,
                    qty=fill_qty,
                    price=fill_price,
                    commission=commission,
                    timestamp=datetime.now(),
                    signal_source=order.strategy_id,
                )
            except Exception:
                pass

        self._write_wal("order_fill", {
            **order.to_dict(),
            "fill_price": fill_price,
            "fill_qty": fill_qty,
        })
        return order

    def sync_from_broker(self) -> None:
        """Periodic sync: reconcile order state with broker."""
        if self._broker is None:
            return
        try:
            broker_orders = self._broker.query_orders()
            for bo in broker_orders:
                # Find or update local order
                for o in self._orders.values():
                    if o.broker_order_id == bo.broker_order_id:
                        if bo.status != o.status:
                            o.status = bo.status
                            o.filled_qty = bo.filled_qty
                            o.avg_fill_price = bo.avg_fill_price
                            o.commission = bo.commission
                            o.updated_at = datetime.now().isoformat()
                        break
        except Exception:
            pass

    # ── Query ──────────────────────────────────────────────────

    def get_order(self, order_id: str) -> OrderRecord | None:
        return self._orders.get(order_id)

    def get_orders(self, status: str | None = None) -> list[OrderRecord]:
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status]
        return orders

    def get_open_orders(self) -> list[OrderRecord]:
        return [o for o in self._orders.values()
                if o.status in ("pending", "submitted", "acknowledged", "partial")]

    # ── Internal helpers ───────────────────────────────────────

    def _transition(self, order_id: str, new_state: OrderState) -> OrderRecord:
        order = self._orders[order_id]
        order.status = new_state.value
        order.updated_at = datetime.now().isoformat()
        self._write_wal(f"order_{new_state.value}", order.to_dict())
        return order

    def _reject(self, order_id: str, reason: str) -> OrderRecord:
        order = self._orders[order_id]
        order.status = "rejected"
        order.error_message = reason
        order.updated_at = datetime.now().isoformat()
        self._write_wal("order_rejected", order.to_dict())
        return order

    @staticmethod
    def _can_transition(order: OrderRecord, target: OrderState) -> bool:
        current = OrderState(order.status)
        return target in VALID_TRANSITIONS.get(current, set())

    def _write_wal(self, operation: str, payload: dict) -> None:
        if self._checkpoint is not None:
            try:
                self._checkpoint.append_wal(operation, payload)
            except Exception:
                pass
