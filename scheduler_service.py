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
    """Return a timestamp like [02:30:10 AM CDT]."""
    return datetime.now(CST_TZ).strftime("[%I:%M:%S %p %Z]")


@dataclass
class ActiveSet:
    chat_id: str
    username: str
    password: str
    window_start: str
    window_end: str
    jobs: List  # PTB Job objects


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
        self.active: Dict[str, ActiveSet] = {}  # active schedules by chat_id

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

    def has_active_job(self, chat_id: str) -> bool:
        return chat_id in self.active and len(self.active[chat_id].jobs) > 0

    def get_active_job_info(self, chat_id: str) -> Optional[dict]:
        aset = self.active.get(chat_id)
        if not aset:
            return None
        return {"window": (aset.window_start, aset.window_end)}

    async def _send(self, chat_id: str, text: str) -> None:
        """Send both to Telegram and terminal."""
        print(text)
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            print(f"[Scheduler SendError] {e}")

    # ----------------------- Public API -----------------------
    async def cancel_jobs(self, chat_id: str) -> None:
        """Cancel all active jobs for a chat."""
        aset = self.active.pop(chat_id, None)
        if not aset:
            await self._send(chat_id, "ℹ️ No active check-ins to cancel.")
            return

        for j in aset.jobs:
            try:
                j.schedule_removal()
            except Exception:
                pass
        await self._send(chat_id, "🛑 Cancelled any existing schedules.")

    async def schedule_user(
        self,
        chat_id: str,
        username: str,
        password: str,
        start_time: str,
        end_time: str,
        restore_mode: bool = False,
    ) -> None:
        """Schedule all jobs for a single user."""
        if not self.app or not self.jq:
            raise RuntimeError("SchedulerService.set_app(app) must be called first.")

        # Cancel previous jobs if any
        if self.has_active_job(chat_id):
            await self.cancel_jobs(chat_id)

        # Parse user-entered times
        try:
            start_t = self._parse_time_h12(start_time)
            end_t = self._parse_time_h12(end_time)
        except ValueError as e:
            await self._send(chat_id, f"❌ {e}")
            return

        # Compute actual datetimes
        start_dt = self._mk_today_time(start_t)
        end_dt = self._mk_today_time(end_t)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)  # overnight window

        now = datetime.now(CST_TZ)
        if restore_mode and end_dt <= now:
            start_dt += timedelta(days=1)
            end_dt += timedelta(days=1)

        # Human-readable
        human_start = start_dt.strftime("%I:%M %p")
        human_end = end_dt.strftime("%I:%M %p")

        await self._send(
            chat_id,
            f"🕓 Scheduling {username} from {human_start} → {human_end} ({CST_TZ.key.split('/')[-1]}).",
        )

        jobs: List = []

        # --- Start marker ---
        async def mark_start(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            await self._send(chat_id, f"{_stamp()} 🚀 Start window for {username}")

        jobs.append(self.jq.run_once(mark_start, when=start_dt))

        # --- Repeating check-in ---
        async def do_checkin(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            ok, msg = perform_check_in(username, password)
            await self._send(chat_id, msg)

        jobs.append(
            self.jq.run_repeating(
                do_checkin,
                interval=30 * 60,  # every 30 min
                first=start_dt,
                last=end_dt,
            )
        )

        # --- End marker ---
        async def mark_end(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            await self._send(
                chat_id,
                f"🌅 All check-ins are complete for {human_start}–{human_end}.\n"
                f"✅ Have a great day ahead!"
            )

        jobs.append(self.jq.run_once(mark_end, when=end_dt))

        # Save to active registry
        self.active[chat_id] = ActiveSet(
            chat_id=chat_id,
            username=username,
            password=password,
            window_start=human_start,
            window_end=human_end,
            jobs=jobs,
        )

    async def restore_from_dict(self, users_dict: Dict[str, dict]) -> int:
        """Recreate schedules from saved users.json."""
        restored = 0
        for cid, u in users_dict.items():
            if all(k in u for k in ("username", "password", "start_time", "end_time")):
                await self.schedule_user(
                    cid,
                    u["username"],
                    u["password"],
                    u["start_time"],
                    u["end_time"],
                    restore_mode=True,
                )
                print(f"♻️ Restored schedule for chat_id={cid}")
                restored += 1
        return restored
