"""Data refresh scheduler for ETL pipelines.

Uses APScheduler to schedule daily and intraday ETL runs.
Only executes on trading days. Does NOT rewrite ETL pipelines, only schedules them.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import time
from pathlib import Path

from .calendar import TradingCalendar


class DataScheduler:
    """APScheduler-based ETL refresh manager.

    Managed jobs:
      - Daily (17:00): northbound flow, sector flow, turnover rate
      - Intraday (5min): optionally refresh sector/north flow during market hours
    """

    def __init__(self, calendar: TradingCalendar | None = None):
        self._calendar = calendar or TradingCalendar()
        self._scheduler = None
        self._jobs: dict[str, dict] = {}
        self._project_root = self._find_project_root()

    @staticmethod
    def _find_project_root() -> Path:
        """Find the project root directory."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "pyproject.toml").exists():
                return parent
        return Path.cwd()

    def start(self) -> None:
        """Start the scheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler(daemon=True)
            self._scheduler.start()
        except ImportError:
            import logging
            logging.getLogger(__name__).warning(
                "apscheduler not available; DataScheduler will not run"
            )

    def stop(self) -> None:
        """Stop the scheduler gracefully."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # ── Job scheduling ─────────────────────────────────────────

    def add_daily_refresh(
        self,
        name: str,
        script: str,
        args: list[str] | None = None,
        run_time: time = time(17, 0),
    ) -> None:
        """Schedule a daily ETL job that only runs on trading days.

        Args:
            name: Job identifier.
            script: Python module path (e.g., "scripts.northflow.etl").
            args: CLI arguments to pass to the script.
            run_time: Time of day to run (Shanghai time, default 17:00).
        """

        def _run():
            if not self._calendar.is_trading_day(
                __import__("datetime").date.today()
            ):
                return
            cmd = [sys.executable, "-m", script] + (args or [])
            try:
                subprocess.run(cmd, cwd=str(self._project_root),
                               capture_output=True, timeout=600)
            except Exception:
                pass

        if self._scheduler:
            from apscheduler.triggers.cron import CronTrigger
            self._scheduler.add_job(
                _run,
                CronTrigger(hour=run_time.hour, minute=run_time.minute,
                            timezone="Asia/Shanghai"),
                id=name,
                name=name,
            )

        self._jobs[name] = {"script": script, "args": args, "run_time": str(run_time)}

    def add_intraday_refresh(
        self,
        name: str,
        script: str,
        args: list[str] | None = None,
        interval_minutes: int = 5,
    ) -> None:
        """Schedule an intraday refresh during market hours.

        The job itself checks if the market is open before running.
        """

        import logging
        logger = logging.getLogger(__name__)

        def _run():
            if self._calendar.is_market_open():
                cmd = [sys.executable, "-m", script] + (args or [])
                try:
                    subprocess.run(cmd, cwd=str(self._project_root),
                                   capture_output=True, timeout=300)
                except Exception as e:
                    logger.warning(f"Intraday job {name} failed: {e}")

        if self._scheduler:
            from apscheduler.triggers.interval import IntervalTrigger
            self._scheduler.add_job(
                _run,
                IntervalTrigger(minutes=interval_minutes),
                id=name,
                name=name,
            )

        self._jobs[name] = {"script": script, "args": args, "interval_m": interval_minutes}

    def trigger_manual_refresh(self, name: str) -> bool:
        """Manually trigger a scheduled job by name. Returns True if triggered."""
        if name not in self._jobs:
            return False
        job = self._jobs[name]
        cmd = [sys.executable, "-m", job["script"]] + (job.get("args") or [])
        try:
            subprocess.run(cmd, cwd=str(self._project_root),
                           capture_output=True, timeout=600)
            return True
        except Exception:
            return False

    def get_job_status(self, name: str) -> dict | None:
        """Get job metadata."""
        return self._jobs.get(name)

    def list_jobs(self) -> list[str]:
        return list(self._jobs.keys())

    @property
    def is_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running
