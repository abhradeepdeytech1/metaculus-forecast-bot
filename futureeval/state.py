"""Write-ahead reservations. A failed push stops all subsequent external calls."""
from datetime import date, datetime, timedelta, timezone
import calendar
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import uuid

log = logging.getLogger(__name__)

# These outcomes guarantee that no forecast was sent, so a later run may try again.
# A claim older than the 20-minute job timeout belongs to a run that died.
RETRYABLE = {"claimed", "budget_exhausted", "changed_or_closed", "failed_before_submit"}
STALE_CLAIM = timedelta(minutes=30)
MAX_ATTEMPTS = 3


class BudgetExceeded(RuntimeError):
    pass


class StateError(RuntimeError):
    pass


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


class Ledger:
    def __init__(self, folder, publish=None, day=utc_day):
        self.folder = Path(folder)
        self.path = self.folder / "ledger.json"
        self.publish = publish
        self.day = day
        self.poisoned = False
        if not self.path.is_file():
            raise StateError("Missing durable ledger. Initialize the state branch once through Actions.")
        self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if self.data.get("version") != 1 or not isinstance(self.data.get("questions"), dict) or not isinstance(self.data.get("calls"), dict):
            raise StateError("Invalid ledger; refusing to reset state")
        for call in self.data["calls"].values():
            if (not isinstance(call, dict) or type(call.get("charged")) is not int
                    or type(call.get("reserved")) is not int or not 0 <= call["charged"] <= call["reserved"]
                    or not isinstance(call.get("question"), str) or not isinstance(call.get("day"), str)):
                raise StateError("Invalid cost record; refusing to reset state")
            try:
                date.fromisoformat(call["day"])
                days = call.get("days", [call["day"]])
                if not isinstance(days, list) or call["day"] not in days:
                    raise ValueError
                for charged_day in days:
                    date.fromisoformat(charged_day)
            except (ValueError, TypeError):
                raise StateError("Invalid accounting date; refusing to reset state") from None

    def save(self):
        if self.poisoned:
            raise StateError("State persistence previously failed")
        try:
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
            if self.publish:
                self.publish()
        except Exception:
            self.poisoned = True
            raise StateError("Durable state update failed; external operations stopped") from None

    def claimable(self, key):
        entry = self.data["questions"].get(key)
        if entry is None:
            return True
        if entry["status"] not in RETRYABLE or entry.get("attempts", 1) >= MAX_ATTEMPTS:
            return False
        if entry["status"] == "claimed" and "at" in entry:
            # The workflow's concurrency group already prevents overlapping runs; the age check is a second guard.
            return datetime.now(timezone.utc) - datetime.fromisoformat(entry["at"]) > STALE_CLAIM
        return True

    def claim(self, key):
        if not self.claimable(key):
            return False
        entry = self.data["questions"].get(key)
        attempts = 1 if entry is None else entry.get("attempts", 1) + 1
        self.data["questions"][key] = {"status": "claimed", "day": self.day(), "attempts": attempts,
                                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.save()
        return True

    def mark(self, key, status):
        self.data["questions"][key]["status"] = status
        self.save()

    def spent(self, key=None, day=None, month=None):
        return sum(c["charged"] for c in self.data["calls"].values()
                   if (key is None or c["question"] == key)
                   and (day is None or day in c.get("days", [c["day"]]))
                   and (month is None or any(d.startswith(month + "-") for d in c.get("days", [c["day"]]))))

    def effective_daily_cap(self, config, day=None, additional=0):
        current = date.fromisoformat(day or self.day())
        remaining_days = calendar.monthrange(current.year, current.month)[1] - current.day + 1
        remaining = max(0, config.monthly_cap - self.spent(month=current.strftime("%Y-%m")) - additional)
        return min(config.daily_cap, remaining // remaining_days)

    def check_budget(self, key, ceiling, config):
        if type(ceiling) is not int or ceiling < 0:
            raise ValueError("Invalid cost ceiling")
        day = self.day()
        days = [day, (date.fromisoformat(day) + timedelta(days=1)).isoformat()]
        if key is not None and self.spent(key=key) + ceiling > config.question_cap:
            raise BudgetExceeded("Question budget exhausted")
        for month in {d[:7] for d in days}:
            if self.spent(month=month) + ceiling > config.monthly_cap:
                raise BudgetExceeded("UTC monthly budget exhausted")
        for charged_day in days:
            # Check the allowance after this reservation so the formula stays
            # true even while a request is pending. Floor fractions of a microdollar.
            if self.spent(day=charged_day) + ceiling > self.effective_daily_cap(config, charged_day, additional=ceiling):
                raise BudgetExceeded("Effective UTC daily budget exhausted")
        return days

    def reserve(self, key, ceiling, config, kind):
        day = self.day()
        # A bounded request can finish after midnight. Hold funds on both dates
        # until its response identifies the actual completion date.
        days = self.check_budget(key, ceiling, config)
        call_id = uuid.uuid4().hex
        self.data["calls"][call_id] = {"question": key, "day": day, "days": days, "kind": kind,
                                           "reserved": ceiling, "charged": ceiling, "status": "pending"}
        self.save()
        return call_id

    def before_send(self, call_id):
        if self.poisoned:
            raise StateError("State persistence previously failed")
        if self.data["calls"][call_id]["day"] != self.day():
            raise BudgetExceeded("UTC day changed during persistence; request not sent")

    def settle(self, call_id, actual=None):
        call = self.data["calls"][call_id]
        if actual is not None and (actual < 0 or actual > call["reserved"]):
            self.poisoned = True
            raise StateError("Provider cost exceeded reservation; stop and check provider billing")
        if actual is not None:
            call["charged"] = actual
        call["days"] = sorted({call["day"], self.day()})
        call["status"] = "known" if actual is not None else "ceiling"
        self.save()


def git_publisher(folder, branch="bot-state", remote="origin", sleep=time.sleep):
    folder = Path(folder).resolve()

    def git(*args, attempts=1):
        for attempt in range(attempts):
            try:
                result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=45)
            except subprocess.TimeoutExpired:
                detail = "timeout"
            else:
                if not result.returncode:
                    return result.stdout.strip()
                # Git errors name refs and hosts only; the ledger holds no forecasts.
                lines = [line.strip() for line in result.stderr.splitlines()
                         if line.strip() and not line.startswith(("hint:", "To "))]
                detail = " | ".join(lines)[-300:] or "no stderr"
            log.warning("state_git_failed op=%s attempt=%s detail=%s", args[0], attempt + 1, detail)
            if attempt + 1 < attempts:
                sleep(5 * 3 ** attempt)
        raise StateError(f"State git {args[0]} failed")

    if git("branch", "--show-current") != branch:
        raise StateError("State checkout is on the wrong branch")

    def publish():
        git("add", "--", "ledger.json")
        git("-c", "user.name=forecast-bot", "-c", "user.email=forecast-bot@users.noreply.github.com",
            "commit", "--allow-empty", "-m", "Record autonomous operation")
        # A non-fast-forward push fails closed. Never force or merge competing ledgers.
        # Repeating the same push is safe: it either lands, finds the commit already
        # there, or is rejected again because another writer moved the branch.
        git("push", remote, f"HEAD:refs/heads/{branch}", attempts=4)
    return publish
