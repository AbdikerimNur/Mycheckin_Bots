import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Tuple
from zoneinfo import ZoneInfo

# --- Constants (kept from your working version) ---
BASE_URL = "https://progressiveliving.mitc.cloud/MyMITC/2"
LOGIN_POST = f"{BASE_URL}/rd_lo.asp?gnd=l"
SESSION_PROBE = f"{BASE_URL}/rd_wa.asp?gnd=chkalerts"
CHECKIN_PAGE = f"{BASE_URL}/ci_checkin.asp"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://progressiveliving.mitc.cloud",
    "Referer": f"{BASE_URL}/newlogon.asp",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
}

CST_TZ = ZoneInfo("America/Chicago")


def _stamp() -> str:
    # Example: [02:30:10 AM CDT]
    return datetime.now(CST_TZ).strftime("[%I:%M:%S %p %Z]")


def perform_check_in(username: str, password: str, job_id: str | None = None) -> Tuple[bool, str]:
    """
    Performs HTTP login + check-in on MITC.
    Returns: (success, human_message)
    """
    if not job_id:
        job_id = os.getenv("CJ", "1409")

    session = requests.Session()

    try:
        # --- Login ---
        login_payload = {"u": username, "p": password, "n": "true", "r": "o"}
        r1 = session.post(LOGIN_POST, data=login_payload, headers=HEADERS, timeout=15)
        r1.raise_for_status()
        if "Invalid User Name or password" in r1.text:
            return False, f"{_stamp()} ❌ Login failed: Invalid credentials."

        # --- Validate session ---
        r2 = session.post(SESSION_PROBE, headers=HEADERS, timeout=15)
        if r2.status_code not in (200, 302):
            return False, f"{_stamp()} ⚠️ Session validation failed."

        # --- Load check-in form ---
        r3 = session.get(CHECKIN_PAGE, headers=HEADERS, allow_redirects=True, timeout=15)
        r3.raise_for_status()
        soup = BeautifulSoup(r3.text, "html.parser")
        try:
            ciid = soup.find("input", {"name": "ciid"}).get("value")
            num = soup.find("input", {"name": "NUM"}).get("value")
        except Exception:
            return False, f"{_stamp()} ⚠️ Missing form fields."

        # --- Submit check-in ---
        payload = {
            "tI": "",
            "ciid": ciid,
            "CJ": job_id,
            "B1": "Check In",
            "ACT": "CI",
            "NUM": num,
            "gpslocation": "44.148966,-94.04925350",
            "gpsvalid": "1",
            "n": "true",
            "r": "o",
        }
        r4 = session.post(CHECKIN_PAGE, data=payload, headers=HEADERS, allow_redirects=False, timeout=15)
        location = r4.headers.get("Location", "") or ""

        # Direct success via redirect
        if r4.status_code == 302 and "ci_report.asp" in location:
            return True, f"{_stamp()} ✅ Successfully checked in!"

        # Follow page if not redirected to the report explicitly
        try:
            r5 = session.get(location if location.startswith("http") else CHECKIN_PAGE,
                             headers=HEADERS, allow_redirects=True, timeout=15)
            body = r5.text.lower()
        except Exception:
            body = ""

        if ("check out" in body) or ("successfully" in body):
            return True, f"{_stamp()} ✅ Successfully checked in!"
        if "already checked in" in body:
            return True, f"{_stamp()} ℹ️ Already checked in."

        return False, f"{_stamp()} ❌ Unknown response."

    except requests.RequestException as e:
        return False, f"{_stamp()} 🌐 Network error: {e}"
    except Exception as e:
        return False, f"{_stamp()} 💥 Unexpected error: {e}"
