"""Broker adapter interface for real A-share trading.

Defines the abstract BrokerAdapter that all broker SDK integrations must implement.
Includes DummyBrokerAdapter for testing without a real broker connection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable


@dataclass(frozen=True)
class BrokerOrder:
    """Order as reported by the broker."""
    broker_order_id: str
    client_order_id: str = ""
    symbol: str = ""
    side: str = ""          # "buy" | "sell"
    order_type: str = ""     # "limit" | "market"
    price: float = 0.0
    qty: int = 0
    filled_qty: int = 0
    status: str = "pending"  # "pending", "submitted", "partial", "filled", "cancelled", "rejected"
    avg_fill_price: float = 0.0
    commission: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class BrokerPosition:
    """Position as reported by the broker."""
    symbol: str
    total_qty: int = 0
    available_qty: int = 0  # broker's own T+1 tracking
    avg_cost: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class BrokerAccount:
    """Account summary as reported by the broker."""
    account_id: str
    total_assets: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    positions: tuple[BrokerPosition, ...] = field(default_factory=tuple)
    market_value: float = 0.0
    total_pnl: float = 0.0


class BrokerAdapter(ABC):
    """Abstract broker interface.

    Each broker SDK (QMT, XTP, EasyTrade, etc.) implements this.
    The OMS operates through this interface, never touching broker SDKs directly.
    """

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to broker. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close broker connection gracefully."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if broker connection is active."""
        ...

    # ── Order management ───────────────────────────────────────

    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float = 0.0,
        order_type: str = "limit",
    ) -> BrokerOrder:
        """Submit an order to the broker."""
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a pending order. Returns True if cancellation was sent."""
        ...

    @abstractmethod
    def query_order(self, broker_order_id: str) -> BrokerOrder | None:
        """Query a single order's status from the broker."""
        ...

    @abstractmethod
    def query_orders(self, status_filter: str | None = None) -> list[BrokerOrder]:
        """Query all orders, optionally filtered by status."""
        ...

    # ── Position and account ───────────────────────────────────

    @abstractmethod
    def query_positions(self) -> list[BrokerPosition]:
        """Query current positions from the broker."""
        ...

    @abstractmethod
    def query_account(self) -> BrokerAccount:
        """Query account summary from the broker."""
        ...

    # ── Optional: broker-native quote stream ───────────────────

    def subscribe_quotes(
        self, symbols: list[str], callback: Callable[[dict], None]
    ) -> None:
        """Optional: subscribe to broker-native real-time quotes."""
        raise NotImplementedError

    def unsubscribe_quotes(self, symbols: list[str]) -> None:
        """Optional: unsubscribe from broker-native quotes."""
        raise NotImplementedError


class DummyBrokerAdapter(BrokerAdapter):
    """Reference broker adapter for testing. All orders auto-fill.

    Simulates a broker without requiring a real broker SDK.
    """

    def __init__(self):
        self._connected = False
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._account = BrokerAccount(account_id="dummy", total_assets=1_000_000.0, available_cash=1_000_000.0)
        self._counter = 0

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def submit_order(self, symbol: str, side: str, qty: int,
                     price: float = 0.0, order_type: str = "limit") -> BrokerOrder:
        self._counter += 1
        order_id = f"dummy_{self._counter}"
        now = datetime.now().isoformat()
        order = BrokerOrder(
            broker_order_id=order_id,
            client_order_id="",
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            qty=qty,
            filled_qty=0,
            status="submitted",
            created_at=now,
            updated_at=now,
        )
        self._orders[order_id] = order
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        if broker_order_id not in self._orders:
            return False
        order = self._orders[broker_order_id]
        if order.status in ("pending", "submitted", "partial"):
            self._orders[broker_order_id] = BrokerOrder(
                **{**order.__dict__, "status": "cancelled",
                   "updated_at": datetime.now().isoformat()}
            )
            return True
        return False

    def query_order(self, broker_order_id: str) -> BrokerOrder | None:
        return self._orders.get(broker_order_id)

    def query_orders(self, status_filter: str | None = None) -> list[BrokerOrder]:
        orders = list(self._orders.values())
        if status_filter:
            orders = [o for o in orders if o.status == status_filter]
        return orders

    def query_positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    def query_account(self) -> BrokerAccount:
        return self._account

    # ── Test helpers ───────────────────────────────────────────

    def simulate_fill(self, broker_order_id: str, fill_price: float,
                      fill_qty: int | None = None) -> BrokerOrder | None:
        """Simulate a fill for testing. Updates internal state."""
        order = self._orders.get(broker_order_id)
        if order is None:
            return None
        actual_qty = fill_qty or order.qty
        new_filled = min(order.filled_qty + actual_qty, order.qty)
        status = "filled" if new_filled >= order.qty else "partial"
        updated = BrokerOrder(
            **{**order.__dict__,
               "filled_qty": new_filled,
               "avg_fill_price": fill_price,
               "status": status,
               "updated_at": datetime.now().isoformat()}
        )
        self._orders[broker_order_id] = updated

        # Update position
        symbol = order.symbol
        if symbol not in self._positions:
            self._positions[symbol] = BrokerPosition(symbol=symbol)
        pos = self._positions[symbol]
        new_qty = pos.total_qty + (actual_qty if order.side == "buy" else -actual_qty)
        new_cost = ((pos.avg_cost * pos.total_qty + fill_price * actual_qty) / new_qty) if new_qty > 0 else 0
        self._positions[symbol] = BrokerPosition(
            symbol=symbol,
            total_qty=new_qty,
            available_qty=new_qty,
            avg_cost=new_cost,
        )
        return updated
