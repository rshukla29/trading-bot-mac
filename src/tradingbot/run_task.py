"""What each launchd job runs on macOS (see macos.py): one entry per task in
scripts/schedule.json. Output goes to logs/<name>_<date>.log, the same files
the Windows version writes.

    python -m tradingbot.run_task ORBTradingBot
    python -m tradingbot.run_task DailyReviewBot 2026-10-05   # re-run a past day's review

TB_RUN_TASK_DRY=1 (tests): log what would run, run nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
SERVER_START, SERVER_STOP = time(8, 0), time(18, 0)

# task -> [(log name, python -m arguments)], run one after another.
TASKS: dict[str, list[tuple[str, list[str]]]] = {
    "SnapshotPremarketBot": [("snapshot_premarket", ["tradingbot.run_snapshot", "premarket"])],
    "StepStopTradingBot": [("step_stop", ["tradingbot.run_step_stop_bot"])],
    "ORBTradingBot": [("orb", ["tradingbot.run_orb_bot"])],
    "StockOrbTradingBot": [("stock_orb", ["tradingbot.run_stock_orb_bot"])],
    "TrendJoinTradingBot": [("trend_join", ["tradingbot.run_trend_join_bot"])],
    "CreditOrbTradingBot": [("credit_orb", ["tradingbot.run_credit_orb_bot"])],
    "IronCondorTradingBot": [("iron_condor", ["tradingbot.run_iron_condor_bot"])],
    "ExpiryGuardBot": [("expiry_guard", ["tradingbot.run_expiry_guard"])],
    "SP500ScanBot": [("sp500_scan", ["tradingbot.sp500_scanner", "--top", "20", "--save"])],
    # MA crossover right after RSI(2), in the same job: both use IBKR client
    # ID 1, and two connections on one ID fail (error 326). Runs even if RSI(2) failed.
    "RSI2TradingBot": [("rsi2", ["tradingbot.run_rsi2_bot"]), ("ma_crossover", ["tradingbot.run_ibkr_bot"])],
    "SnapshotPostmarketBot": [("snapshot_postmarket", ["tradingbot.run_snapshot", "postmarket"])],
}
SPECIAL = ("BotControlServer", "DailyReviewBot")
REVIEW_TOOLS = ["--allowedTools", "Read", "Glob", "Grep",
                "--disallowedTools", "Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]


def _dry() -> bool:
    return os.environ.get("TB_RUN_TASK_DRY") == "1"


def log_path(name: str, day: str | None = None) -> Path:
    folder = REPO / "logs"
    folder.mkdir(exist_ok=True)
    return folder / f"{name}_{day or date.today().isoformat()}.log"


def _note(log: Path, text: str) -> None:
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def run_module(args: list[str], log: Path, run=subprocess.run) -> int:
    """python -m <args>, stdout and stderr appended to `log`."""
    if _dry():
        _note(log, "would run python -m " + " ".join(args))
        return 0
    with open(log, "ab") as f:
        return run([sys.executable, "-m", *args], cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT,
                   stdin=subprocess.DEVNULL).returncode


def run_steps(task: str, run=subprocess.run) -> int:
    """Every step of a plain task; the exit code is the first failure's."""
    first_fail = 0
    for name, args in TASKS[task]:
        code = run_module(args, log_path(name), run)
        first_fail = first_fail or code
    return first_fail


def control_server(now: datetime | None = None, run=subprocess.run) -> int:
    """The dashboard's Start / Pause / Stop buttons: only Mon-Fri 8am-6pm New
    York. It rebuilds the dashboard, opens it, and stops itself at 6pm."""
    now = now or datetime.now(ET)
    log = log_path("control_server")
    if now.weekday() >= 5 or not SERVER_START <= now.time() < SERVER_STOP:
        _note(log, "Outside Mon-Fri 8am-6pm New York - not starting the control server")
        return 0
    return run_module(["tradingbot.run_control_server", "--open", "--until", SERVER_STOP.strftime("%H:%M")], log, run)


def _claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.exists() else None


def daily_review(day: str | None = None, run=subprocess.run) -> int:
    """4:50pm: gather the day's facts, have Claude Code (read-only tools) write
    the review, save it, commit it locally, rebuild the dashboard."""
    day = day or date.today().isoformat()
    log = log_path("daily_review", day)
    run_module(["tradingbot.run_daily_review", day], log, run)
    if _dry():
        _note(log, "would ask Claude for the review and commit it")
        return 0
    y, m, d = day.split("-")
    review_dir = REPO / "analysis" / "daily" / y / m / d
    prompt = ((REPO / "scripts" / "daily_review_prompt.md").read_text(encoding="utf-8")
              .replace("{DATE}", day).replace("{YEAR}", y).replace("{MONTH}", m).replace("{DAY}", d))
    claude = _claude()
    if claude is None:
        _note(log, "Claude review failed (Claude Code isn't installed)")
    else:
        with open(log, "ab") as errlog:
            out = run([claude, "-p", prompt, *REVIEW_TOOLS], cwd=str(REPO), stdout=subprocess.PIPE, stderr=errlog,
                      stdin=subprocess.DEVNULL)
        review = (out.stdout or b"").decode("utf-8", errors="replace").strip()
        if out.returncode == 0 and review:
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / f"review_{day}.md").write_text(review + "\n", encoding="utf-8")
        else:
            _note(log, f"Claude review failed (exit {out.returncode})")
    # Commit locally (no push), like the daily snapshots.
    with open(log, "ab") as f:
        run(["git", "add", f"analysis/daily/{y}/{m}/{d}"], cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT)
        run(["git", "commit", "-m", f"Daily review: {day}"], cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT)
    run_module(["tradingbot.run_dashboard"], log, run)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in (*TASKS, *SPECIAL):
        print(__doc__)
        print("Tasks:", ", ".join([*TASKS, *SPECIAL]))
        return 2
    os.chdir(REPO)
    task = argv[0]
    if task == "BotControlServer":
        return control_server()
    if task == "DailyReviewBot":
        return daily_review(argv[1] if len(argv) > 1 else None)
    return run_steps(task)


if __name__ == "__main__":
    sys.exit(main())
