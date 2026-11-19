"""macOS scheduling: one launchd job (a LaunchAgent) per entry in
scripts/schedule.json, each running `python -m tradingbot.run_task <name>`.

    python -m tradingbot.macos register   # create or replace every job (install.sh, update.sh)
    python -m tradingbot.macos status     # each job's state and next run
    python -m tradingbot.macos remove     # unload and delete every job
    python -m tradingbot.macos wake       # print the pmset command for the weekday wake-up

schedule.json's times are New York time. Each becomes this Mac's wall-clock
time for the weekday it falls on (so a Mac in another time zone gets the
right local day too). launchd runs a job that was due while the Mac slept as
soon as it wakes. If New York and this Mac change clocks on different dates,
the control server re-registers the jobs at its 8am start when a time is off.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
SCHEDULE = REPO / "scripts" / "schedule.json"
LABEL_PREFIX = "com.tradingbots."
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WAKE_NY = "07:50"   # weekday wake-up, before the 8am premarket snapshot
SERVER_TASK = "BotControlServer"
# launchd starts jobs with a bare PATH; git (Xcode tools or Homebrew) and the
# claude CLI (~/.local/bin) must be found by the bots and the daily review.
PATH = ":".join([str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def agents_dir() -> Path:
    return Path(os.environ.get("TB_LAUNCH_AGENTS") or Path.home() / "Library" / "LaunchAgents")


def load_schedule(path: Path = SCHEDULE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def label(name: str) -> str:
    return LABEL_PREFIX + name


def _local_tz():
    return datetime.now().astimezone().tzinfo


def local_slots(ny: str, days: list[str], today: date | None = None, tz=None) -> list[dict]:
    """launchd StartCalendarInterval entries ({Weekday, Hour, Minute}, local
    time, Weekday 0 = Sunday) for `ny` (HH:MM New York) on each of `days`,
    using the offsets of the week starting `today`."""
    tz = tz or _local_tz()
    today = today or date.today()
    hh, mm = map(int, ny.split(":"))
    monday = today - timedelta(days=today.weekday())
    out = []
    for name in days:
        d = monday + timedelta(days=DAY_NAMES.index(name))
        t = datetime.combine(d, time(hh, mm), ET).astimezone(tz)
        out.append({"Weekday": (t.weekday() + 1) % 7, "Hour": t.hour, "Minute": t.minute})
    return out


def plist_for(task: dict, days: list[str], repo: Path = REPO, python: str | None = None,
              today: date | None = None, tz=None, dry_run: bool = False) -> dict:
    """The LaunchAgent for one schedule.json task."""
    python = python or str(repo / ".venv" / "bin" / "python")
    args = [python, "-m", "tradingbot.run_task", task["name"]]
    env = {"PATH": PATH, "PYTHONUNBUFFERED": "1"}
    if dry_run:
        env["TB_RUN_TASK_DRY"] = "1"   # tests: the job only logs what it would run
    p = {
        "Label": label(task["name"]),
        "ProgramArguments": args,
        "WorkingDirectory": str(repo),
        "StartCalendarInterval": local_slots(task["ny"], days, today, tz),
        "EnvironmentVariables": env,
        "StandardOutPath": str(repo / "logs" / f"launchd_{task['name']}.log"),
        "StandardErrorPath": str(repo / "logs" / f"launchd_{task['name']}.log"),
        "ProcessType": "Standard",
    }
    if task.get("at_logon"):
        # The control server: also at log-in (it only serves Mon-Fri 8am-6pm),
        # restarted if it crashes, and keeps the Mac awake while it runs.
        p["ProgramArguments"] = ["/usr/bin/caffeinate", "-i", *args]
        p["RunAtLoad"] = True
        p["KeepAlive"] = {"SuccessfulExit": False}
        p["ThrottleInterval"] = 60
    return p


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _domain() -> str:
    return f"gui/{_uid()}"


def _launchctl(*args: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def loaded_jobs(run=subprocess.run) -> dict[str, int | None]:
    """{label: pid or None} for our loaded jobs (`launchctl list`)."""
    out = _launchctl("list", run=run).stdout
    jobs = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith(LABEL_PREFIX):
            jobs[parts[2]] = int(parts[0]) if parts[0].strip().isdigit() else None
    return jobs


def disabled_labels(run=subprocess.run) -> set[str]:
    """Our labels marked disabled (`launchctl print-disabled`): lines like
    '"com.tradingbots.X" => disabled' (or '=> true' on older macOS)."""
    out = _launchctl("print-disabled", _domain(), run=run).stdout
    found = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(f'"{LABEL_PREFIX}') and ("=> disabled" in line or "=> true" in line):
            found.add(line.split('"')[1])
    return found


def register_all(repo: Path = REPO, python: str | None = None, run=subprocess.run, skip_running: bool = False,
                 dry_run: bool = False, today: date | None = None, tz=None) -> list[str]:
    """Write and (re)load every job; remove jobs no longer in schedule.json.
    `skip_running`: leave a job that is running now alone (reloading it
    would stop it), e.g. the control server re-registering at 8am."""
    cfg = load_schedule(repo / "scripts" / "schedule.json")
    folder = agents_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (repo / "logs").mkdir(exist_ok=True)
    running = {lb for lb, pid in loaded_jobs(run).items() if pid} if skip_running else set()
    wanted, lines = set(), []
    for task in cfg["tasks"]:
        lb = label(task["name"])
        wanted.add(lb)
        if lb in running:
            lines.append(f"{task['name']:<22} running now - left as it is")
            continue
        path = folder / f"{lb}.plist"
        p = plist_for(task, cfg["days"], repo, python, today, tz, dry_run)
        with open(path, "wb") as f:
            plistlib.dump(p, f)
        _launchctl("bootout", f"{_domain()}/{lb}", run=run)            # not loaded yet: fine
        _launchctl("enable", f"{_domain()}/{lb}", run=run)
        r = _launchctl("bootstrap", _domain(), str(path), run=run)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap {lb} failed: {(r.stderr or r.stdout).strip()}")
        first = p["StartCalendarInterval"][0]
        lines.append(f"{task['name']:<22} {task['ny']} New York = {first['Hour']:02d}:{first['Minute']:02d} on this Mac")
    for path in folder.glob(f"{LABEL_PREFIX}*.plist"):
        if path.stem not in wanted:
            _launchctl("bootout", f"{_domain()}/{path.stem}", run=run)
            path.unlink()
            lines.append(f"{path.stem[len(LABEL_PREFIX):]:<22} removed (no longer in schedule.json)")
    return lines


def remove_all(run=subprocess.run) -> list[str]:
    out = []
    for path in agents_dir().glob(f"{LABEL_PREFIX}*.plist"):
        _launchctl("bootout", f"{_domain()}/{path.stem}", run=run)
        path.unlink()
        out.append(path.stem)
    return out


def next_run(slots: list[dict], now: datetime | None = None) -> datetime | None:
    """The next local wall-clock time any of `slots` fires after `now` (naive, local)."""
    now = now or datetime.now()
    best = None
    for s in slots:
        wd = (s["Weekday"] - 1) % 7                 # Python weekday, Monday = 0
        d = now.date() + timedelta(days=(wd - now.weekday()) % 7)
        t = datetime.combine(d, time(s["Hour"], s["Minute"]))
        if t <= now:
            t += timedelta(days=7)
        best = t if best is None or t < best else best
    return best


def task_info(names: list[str], now: datetime | None = None, run=subprocess.run) -> dict[str, dict]:
    """{name: {"state": Ready|Running|Disabled|Missing, "next_at": ISO local or ""}}."""
    jobs, off = loaded_jobs(run), disabled_labels(run)
    data = {}
    for name in names:
        lb = label(name)
        path = agents_dir() / f"{lb}.plist"
        if lb not in jobs or not path.exists():
            data[name] = {"state": "Missing", "next_at": ""}
            continue
        if lb in off:
            data[name] = {"state": "Disabled", "next_at": ""}
            continue
        try:
            with open(path, "rb") as f:
                slots = plistlib.load(f).get("StartCalendarInterval", [])
        except (OSError, plistlib.InvalidFileException):
            slots = []
        nxt = next_run(slots if isinstance(slots, list) else [slots], now)
        data[name] = {"state": "Running" if jobs[lb] else "Ready",
                      "next_at": nxt.isoformat(timespec="seconds") if nxt else ""}
    return data


def start_task(name: str, run=subprocess.run) -> bool:
    """Run a job now (the dashboard's Start). Does nothing if it's already running."""
    r = _launchctl("kickstart", f"{_domain()}/{label(name)}", run=run)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"kickstart {name} failed")
    return True


def wake_args(today: date | None = None, tz=None) -> list[str]:
    """`pmset repeat wakeorpoweron <days> <HH:MM:SS>`: wake at WAKE_NY New York
    on weekdays, in this Mac's time and weekdays (needs administrator rights).
    Replaces any other repeating pmset schedule on this Mac."""
    letters = {0: "M", 1: "T", 2: "W", 3: "R", 4: "F", 5: "S", 6: "U"}
    slots = local_slots(WAKE_NY, DAY_NAMES[:5], today, tz)
    days = "".join(letters[(s["Weekday"] - 1) % 7] for s in slots)
    s = slots[0]
    return ["pmset", "repeat", "wakeorpoweron", days, f"{s['Hour']:02d}:{s['Minute']:02d}:00"]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "register":
        for line in register_all(skip_running="--skip-running" in argv, dry_run="--dry-run-jobs" in argv):
            print("   OK  " + line)
        return 0
    if cmd == "remove":
        for lb in remove_all():
            print("   removed " + lb)
        return 0
    if cmd == "wake":
        print(" ".join(wake_args()))
        return 0
    if cmd == "status":
        names = [t["name"] for t in load_schedule()["tasks"]]
        for name, info in task_info(names).items():
            print(f"{name:<22} {info['state']:<9} {info['next_at']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
