"""Session state persistence via WAL (write-ahead log) + periodic snapshots.

Follows the existing SessionStore JSONL pattern.
Directory structure:
    data/live_state/{session_id}/
        wal.jsonl        -- append-only write-ahead log
        snapshot.json    -- latest full snapshot
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WALEntry:
    """One entry in the write-ahead log."""
    sequence: int
    timestamp: str  # ISO 8601
    operation: str   # "order_create", "order_fill", "order_cancel", "position_update", "snapshot"
    payload: dict[str, Any]


@dataclass(frozen=True)
class OrdersSnapshot:
    """Full state snapshot for crash recovery."""
    session_id: str
    sequence: int
    timestamp: str  # ISO 8601
    orders: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    cash_available: float = 0.0
    total_equity: float = 0.0
    peak_equity: float = 0.0
    daily_pnl: float = 0.0


class CheckpointManager:
    """Manages WAL + snapshot persistence for live trading state.

    Recovery: load latest snapshot, replay WAL entries with higher sequence numbers.
    """

    SNAPSHOT_INTERVAL = 100  # Write snapshot every N WAL entries

    def __init__(self, base_dir: str | Path = "data/live_state"):
        self._base_dir = Path(base_dir)
        self._session_id: str | None = None
        self._wal_path: Path | None = None
        self._snapshot_path: Path | None = None
        self._counter: int = 0

    # ── Session management ─────────────────────────────────────

    def init_session(self, session_id: str | None = None) -> str:
        """Initialize a new session directory. Returns session_id."""
        if session_id is None:
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_id = session_id
        session_dir = self._base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._wal_path = session_dir / "wal.jsonl"
        self._snapshot_path = session_dir / "snapshot.json"
        self._counter = 0
        return session_id

    # ── WAL ────────────────────────────────────────────────────

    def append_wal(self, operation: str, payload: dict[str, Any]) -> WALEntry:
        """Append an entry to the WAL."""
        if self._wal_path is None:
            raise RuntimeError("Session not initialized. Call init_session() first.")
        self._counter += 1
        entry = WALEntry(
            sequence=self._counter,
            timestamp=datetime.now().isoformat(),
            operation=operation,
            payload=payload,
        )
        with open(self._wal_path, "a") as f:
            f.write(json.dumps({
                "sequence": entry.sequence,
                "timestamp": entry.timestamp,
                "operation": entry.operation,
                "payload": entry.payload,
            }, ensure_ascii=False) + "\n")
        return entry

    def replay_wal(self, from_sequence: int = 0) -> list[WALEntry]:
        """Replay WAL entries with sequence > from_sequence."""
        if self._wal_path is None or not self._wal_path.exists():
            return []
        entries = []
        with open(self._wal_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d["sequence"] > from_sequence:
                        entries.append(WALEntry(
                            sequence=d["sequence"],
                            timestamp=d["timestamp"],
                            operation=d["operation"],
                            payload=d["payload"],
                        ))
                except (json.JSONDecodeError, KeyError):
                    continue
        return entries

    # ── Snapshot ───────────────────────────────────────────────

    def write_snapshot(self, snapshot: OrdersSnapshot) -> None:
        """Write a full state snapshot."""
        if self._snapshot_path is None:
            raise RuntimeError("Session not initialized.")
        snap = snapshot if isinstance(snapshot, OrdersSnapshot) else snapshot
        with open(self._snapshot_path, "w") as f:
            json.dumps({
                "session_id": snap.session_id,
                "sequence": snap.sequence,
                "timestamp": snap.timestamp,
                "orders": snap.orders,
                "positions": snap.positions,
                "cash_available": snap.cash_available,
                "total_equity": snap.total_equity,
                "peak_equity": snap.peak_equity,
                "daily_pnl": snap.daily_pnl,
            }, ensure_ascii=False, indent=2)
            f.write(json.dumps({
                "session_id": snap.session_id,
                "sequence": snap.sequence,
                "timestamp": snap.timestamp,
                "orders": snap.orders,
                "positions": snap.positions,
                "cash_available": snap.cash_available,
                "total_equity": snap.total_equity,
                "peak_equity": snap.peak_equity,
                "daily_pnl": snap.daily_pnl,
            }, ensure_ascii=False, indent=2))

    def load_snapshot(self) -> OrdersSnapshot | None:
        """Load the latest snapshot."""
        if self._snapshot_path is None or not self._snapshot_path.exists():
            return None
        with open(self._snapshot_path) as f:
            d = json.load(f)
        return OrdersSnapshot(**d)

    # ── Recovery ───────────────────────────────────────────────

    def recover(self) -> OrdersSnapshot | None:
        """Load latest snapshot and replay WAL to recover full state.

        Returns None if no session exists.
        """
        snap = self.load_snapshot()
        if snap is None:
            return None

        # Replay WAL entries after the snapshot's sequence
        entries = self.replay_wal(from_sequence=snap.sequence)
        if not entries:
            return snap

        # Rebuild state from snapshot + WAL
        orders = {o.get("order_id"): o for o in snap.orders}
        positions = {p.get("symbol"): p for p in snap.positions}
        cash = snap.cash_available
        equity = snap.total_equity
        peak = snap.peak_equity
        daily_pnl = snap.daily_pnl

        for entry in entries:
            op = entry.operation
            p = entry.payload
            if op == "order_create":
                orders[p.get("order_id")] = p
            elif op == "order_fill":
                orders[p.get("order_id")] = p
                cash = p.get("cash_after", cash)
                pos = p.get("position_after")
                if pos:
                    positions[pos["symbol"]] = pos
            elif op == "order_cancel":
                if p.get("order_id") in orders:
                    orders[p["order_id"]] = {**orders[p["order_id"]], "status": "cancelled"}
            elif op == "snapshot":
                cash = p.get("cash_available", cash)
                equity = p.get("total_equity", equity)
                peak = p.get("peak_equity", peak)
                daily_pnl = p.get("daily_pnl", daily_pnl)

        return OrdersSnapshot(
            session_id=snap.session_id,
            sequence=entries[-1].sequence if entries else snap.sequence,
            timestamp=entries[-1].timestamp if entries else snap.timestamp,
            orders=list(orders.values()),
            positions=list(positions.values()),
            cash_available=cash,
            total_equity=equity,
            peak_equity=peak,
            daily_pnl=daily_pnl,
        )

    @property
    def sequence(self) -> int:
        return self._counter

    @property
    def session_id(self) -> str | None:
        return self._session_id
