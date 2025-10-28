from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.ext import Application, JobQueue, ContextTypes

from checkin_service import perform_check_in

CST_TZ = ZoneInfo("America/Chicago")


def _stamp() -> str:
    """Timestamp like [02:30:10 AM CDT]."""
    return datetime.now(CST_TZ).strftime("[%I:%M:%S %p %Z]")


@dataclass
class ActiveSet:
    chat_id: str
    username: str
    password: str
    window_start: str   # human-readable, e.g., "02:30 AM"
    window_end: str     # human-readable, e.g., "06:30 AM"
    end_dt: datetime    # REAL end datetime, used to auto-prune
    jobs: List          # PTB Job objects


class SchedulerService:
    """
    JobQueue-based scheduler for automated check-ins.

    Schedules:
      • a start marker (once at start_dt)
      • a repeating check-in every 30 minutes (from start_dt to end_dt)
      • an end marker (once at end_dt)
    """

    def __init__(self) -> None:
        self.app: Optional[Application] = None
        self.jq: Optional[JobQueue] = None
        self.bot: Optional[Bot] = None
        self.active: Dict[str, ActiveSet] = {}  # active schedules by chat_id (string key)

    # ----------------------- Wiring -----------------------
    def set_app(self, app: Application) -> None:
        """Bind Telegram app context."""
        self.app = app
        self.jq = app.job_queue
        self.bot = app.bot

    # ----------------------- Utilities -----------------------
    @staticmethod
    def _parse_time_h12(hhmm_am_pm: str) -> dtime:
        """Parse user-entered 12-hour time formats like 11:30PM or 6am."""
        s = hhmm_am_pm.strip().upper().replace(" ", "")
        for fmt in ("%I:%M%p", "%I:%M %p", "%I%p"):
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                continue
        raise ValueError(f"Invalid time format: {hhmm_am_pm!r} (e.g., 11:30PM)")

    @staticmethod
    def _mk_today_time(t: dtime) -> datetime:
        now = datetime.now(CST_TZ)
        return datetime(now.year, now.month, now.day, t.hour, t.minute, tzinfo=CST_TZ)

    def _prune_if_expired(self, chat_id: str) -> None:
        """Remove active entry if its end_dt is in the past."""
        aset = self.active.get(chat_id)
        if not aset:
            return
        now = datetime.now(CST_TZ)
        # small grace (1s) to avoid edge race
        if now > aset.end_dt + timedelta(seconds=1):
            # defensively remove any lingering jobs
            for j in aset.jobs:
                try:
                    j.schedule_removal()
                except Exception:
                    pass
            self.active.pop(chat_id, None)

    def has_active_job(self, chat_id: str | int) -> bool:
        """Check if a user has an active schedule registered (auto-prunes stale)."""
        key = str(chat_id)
        self._prune_if_expired(key)
        return key in self.active and len(self.active[key].jobs) > 0

    def get_active_job_info(self, chat_id: str | int) -> Optional[dict]:
        """Get details about the active schedule window."""
        aset = self.active.get(str(chat_id))
        if not aset:
            return None
        return {"window": (aset.window_start, aset.window_end)}

    async def _send(self, chat_id: str | int, text: str) -> None:
        """Send both to Telegram and terminal."""
        print(text)
        try:
            if self.bot:
                await self.bot.send_message(chat_id=chat_id, text=text)
            else:
                print("[Scheduler SendError] Bot instance not available.")
        except Exception as e:
            print(f"[Scheduler SendError] {e}")

    # ----------------------- Public API -----------------------
    async def cancel_jobs(self, chat_id: str | int, silent: bool = False) -> bool:
        """
        Cancel all active jobs for a chat_id. Returns True if jobs were cancelled.
        """
        chat_id_str = str(chat_id)
        aset = self.active.pop(chat_id_str, None)  # remove from active dict first

        if not aset:
            if not silent:
                await self._send(chat_id_str, "ℹ️ No active check-ins to cancel.")
            return False  # Nothing to cancel

        # Remove jobs
        for j in aset.jobs:
            try:
                j.schedule_removal()
            except Exception as e:
                print(f"[Scheduler CancelError] Failed to remove job: {e}")

        if not silent:
            await self._send(chat_id_str, "🛑 Cancelled any existing schedules.")
        return True

    async def schedule_user(
        self,
        chat_id: int,
        username: str,
        password: str,
        start_time: str,
        end_time: str,
        restore_mode: bool = False,
    ) -> None:
        """Schedule all jobs for a single user."""
        chat_id_str = str(chat_id)

        if not self.app or not self.jq:
            raise RuntimeError("SchedulerService.set_app(app) must be called first.")

        # If there’s anything stale, prune it; if there’s anything active, cancel it silently.
        self._prune_if_expired(chat_id_str)
        if self.has_active_job(chat_id):
            await self.cancel_jobs(chat_id, silent=True)

        # Parse times
        try:
            start_t = self._parse_time_h12(start_time)
            end_t = self._parse_time_h12(end_time)
        except ValueError as e:
            await self._send(chat_id_str, f"❌ {e}")
            return

        # Compute actual datetimes
        start_dt = self._mk_today_time(start_t)
        end_dt = self._mk_today_time(end_t)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)  # overnight window

        now = datetime.now(CST_TZ)
        # If restoring and the window is already in the past, shift to tomorrow
        if restore_mode and end_dt <= now:
            start_dt += timedelta(days=1)
            end_dt += timedelta(days=1)

        # Human-readable
        human_start = start_dt.strftime("%I:%M %p")
        human_end = end_dt.strftime("%I:%M %p")

        await self._send(
            chat_id_str,
            f"🕓 Scheduling {username} from {human_start} → {human_end} ({CST_TZ.key.split('/')[-1]}).",
        )

        jobs: List = []

        # --- Start marker ---
        async def mark_start(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            await self._send(chat_id_str, f"{_stamp()} 🚀 Start window for {username}")

        if start_dt >= now:
            jobs.append(self.jq.run_once(mark_start, when=start_dt, name=f"start_{chat_id}"))
        else:
            # If we’re already inside the window, send the start message immediately
            await self._send(chat_id_str, f"{_stamp()} 🚀 Start window for {username}")

        # --- Repeating check-in ---
        async def do_checkin(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            ok, msg = perform_check_in(username, password)
            await self._send(chat_id_str, msg)

        # Start at start_dt; PTB will handle the cadence. If already past start_dt, it will
        # fire on the next interval tick based on "first".
        jobs.append(
            self.jq.run_repeating(
                do_checkin,
                interval=30 * 60,  # every 30 min
                first=start_dt,
                last=end_dt,       # stop at end
                name=f"repeat_{chat_id}",
            )
        )

        # --- End marker ---
        async def mark_end(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            # Clean active state as soon as the window finishes
            self.active.pop(chat_id_str, None)
            await self._send(
                chat_id_str,
                f"🌅 All check-ins are complete for {human_start}–{human_end}.\n"
                f"✅ Have a great day ahead!"
            )

        if end_dt >= now:
            # schedule slightly after end_dt to ensure last tick completes
            jobs.append(self.jq.run_once(mark_end, when=end_dt + timedelta(seconds=1), name=f"end_{chat_id}"))
        else:
            # If end already passed (rare), ensure we’re clean
            self.active.pop(chat_id_str, None)
            await self._send(chat_id_str, f"ℹ️ The schedule window {human_start}–{human_end} has already ended.")
            return

        # Save to active registry
        self.active[chat_id_str] = ActiveSet(
            chat_id=chat_id_str,
            username=username,
            password=password,
            window_start=human_start,
            window_end=human_end,
            end_dt=end_dt,
            jobs=jobs,
        )

    async def restore_from_dict(self, users_dict: Dict[str, dict]) -> int:
        """Recreate schedules from saved user data."""
        restored = 0
        for cid_str, u in users_dict.items():
            try:
                cid_int = int(cid_str)
            except ValueError:
                print(f"[Restore Error] Invalid chat_id: {cid_str}")
                continue

            if all(k in u for k in ("username", "password", "start_time", "end_time")):
                await self.schedule_user(
                    cid_int,
                    u["username"],
                    u["password"],
                    u["start_time"],
                    u["end_time"],
                    restore_mode=True,
                )
                print(f"♻️ Restored schedule for chat_id={cid_str}")
                restored += 1
        return restored
