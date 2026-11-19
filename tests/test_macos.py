"""macOS scheduling (macos.py) and the task runner (run_task.py), with
launchctl and subprocess faked so they run anywhere."""
import plistlib
import subprocess
from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from tradingbot import macos, run_task

NY = ZoneInfo("America/New_York")
WEEK = date(2026, 11, 18)          # a Wednesday, New York on EST (UTC-5)
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


class FakeLaunchctl:
    """Records launchctl calls; `jobs` = {label: pid or None} for `list`."""
    def __init__(self, jobs=None, disabled=()):
        self.calls, self.jobs, self.disabled = [], dict(jobs or {}), set(disabled)

    def __call__(self, args, **kw):
        self.calls.append(args[1:])
        sub = args[1]
        if sub == "list":
            rows = ["PID\tStatus\tLabel", "-\t0\tcom.apple.other"]
            rows += [f"{pid if pid else '-'}\t0\t{lb}" for lb, pid in self.jobs.items()]
            return NS(returncode=0, stdout="\n".join(rows), stderr="")
        if sub == "print-disabled":
            out = "disabled services = {\n" + "".join(f'\t"{lb}" => disabled\n' for lb in self.disabled)
            return NS(returncode=0, stdout=out + '\t"com.apple.x" => enabled\n}', stderr="")
        if sub == "bootstrap":
            with open(args[3], "rb") as f:
                self.jobs[plistlib.load(f)["Label"]] = None
        return NS(returncode=0, stdout="", stderr="")


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setenv("TB_LAUNCH_AGENTS", str(tmp_path / "LaunchAgents"))
    return tmp_path / "LaunchAgents"


# ---------------------------------------------------------------- times

def test_new_york_times_on_a_new_york_mac():
    slots = macos.local_slots("09:30", DAYS, WEEK, NY)
    assert slots == [{"Weekday": d, "Hour": 9, "Minute": 30} for d in (1, 2, 3, 4, 5)]


def test_times_in_another_zone_move_the_hour():
    slots = macos.local_slots("09:30", DAYS, WEEK, ZoneInfo("Europe/London"))
    assert {(s["Hour"], s["Minute"]) for s in slots} == {(14, 30)}


def test_a_time_past_midnight_lands_on_the_next_local_day():
    # 4:50pm New York (EST) is 3:20am the next day in India: Tuesday-Saturday there.
    slots = macos.local_slots("16:50", DAYS, WEEK, ZoneInfo("Asia/Kolkata"))
    assert [s["Weekday"] for s in slots] == [2, 3, 4, 5, 6]
    assert {(s["Hour"], s["Minute"]) for s in slots} == {(3, 20)}


def test_daylight_saving_week_uses_that_weeks_offset():
    summer = macos.local_slots("09:30", DAYS, date(2026, 7, 15), ZoneInfo("Europe/London"))
    assert summer[0]["Hour"] == 14   # both on summer time: still 5 hours apart
    march = macos.local_slots("09:30", DAYS, date(2026, 3, 18), ZoneInfo("Europe/London"))
    assert march[0]["Hour"] == 13    # New York already on summer time, London not yet


def test_weekday_wake_up():
    assert macos.wake_args(WEEK, NY) == ["pmset", "repeat", "wakeorpoweron", "MTWRF", "07:50:00"]
    nz = macos.wake_args(WEEK, ZoneInfo("Pacific/Auckland"))     # UTC+13: already the next day
    assert nz[3] == "TWRFS" and nz[4] == "01:50:00"


def test_next_run_is_the_next_slot_after_now():
    slots = macos.local_slots("09:30", DAYS, WEEK, NY)
    assert macos.next_run(slots, datetime(2026, 11, 18, 8, 0)) == datetime(2026, 11, 18, 9, 30)
    assert macos.next_run(slots, datetime(2026, 11, 18, 9, 30)) == datetime(2026, 11, 19, 9, 30)
    assert macos.next_run(slots, datetime(2026, 11, 20, 10, 0)) == datetime(2026, 11, 23, 9, 30)  # Fri -> Mon


# ---------------------------------------------------------------- jobs

def test_plist_runs_the_task_runner_from_the_repo(tmp_path):
    p = macos.plist_for({"name": "ORBTradingBot", "ny": "09:30"}, DAYS, tmp_path, "/py", WEEK, NY)
    assert p["Label"] == "com.tradingbots.ORBTradingBot"
    assert p["ProgramArguments"] == ["/py", "-m", "tradingbot.run_task", "ORBTradingBot"]
    assert p["WorkingDirectory"] == str(tmp_path) and "RunAtLoad" not in p
    assert "/opt/homebrew/bin" in p["EnvironmentVariables"]["PATH"]
    assert "TB_RUN_TASK_DRY" not in p["EnvironmentVariables"]


def test_control_server_job_keeps_the_mac_awake_and_restarts(tmp_path):
    p = macos.plist_for({"name": "BotControlServer", "ny": "08:00", "at_logon": True}, DAYS, tmp_path, "/py", WEEK, NY)
    assert p["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
    assert p["RunAtLoad"] is True and p["KeepAlive"] == {"SuccessfulExit": False}


def test_register_writes_and_loads_every_job_and_removes_old_ones(tmp_path, agents):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "schedule.json").write_text(
        '{"days": ["Monday","Tuesday","Wednesday","Thursday","Friday"], "tasks": ['
        '{"name": "ORBTradingBot", "ny": "09:30"}, {"name": "BotControlServer", "ny": "08:00", "at_logon": true}]}')
    agents.mkdir()
    (agents / "com.tradingbots.OldBot.plist").write_bytes(plistlib.dumps({"Label": "com.tradingbots.OldBot"}))
    fake = FakeLaunchctl()
    lines = macos.register_all(tmp_path, "/py", run=fake, today=WEEK, tz=NY, dry_run=True)
    assert sorted(p.name for p in agents.iterdir()) == ["com.tradingbots.BotControlServer.plist",
                                                       "com.tradingbots.ORBTradingBot.plist"]
    assert ["bootstrap", f"gui/{macos._uid()}", str(agents / "com.tradingbots.ORBTradingBot.plist")] in fake.calls
    assert any("ORBTradingBot" in ln and "09:30 New York = 09:30" in ln for ln in lines)
    assert any("OldBot" in ln and "removed" in ln for ln in lines)
    with open(agents / "com.tradingbots.ORBTradingBot.plist", "rb") as f:
        assert plistlib.load(f)["EnvironmentVariables"]["TB_RUN_TASK_DRY"] == "1"


def test_register_can_leave_a_running_job_alone(tmp_path, agents):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "schedule.json").write_text(
        '{"days": ["Monday"], "tasks": [{"name": "BotControlServer", "ny": "08:00", "at_logon": true}]}')
    fake = FakeLaunchctl(jobs={"com.tradingbots.BotControlServer": 4242})
    lines = macos.register_all(tmp_path, "/py", run=fake, skip_running=True, today=WEEK, tz=NY)
    assert "running now" in lines[0]
    assert not any(c[0] in ("bootout", "bootstrap") for c in fake.calls)


def test_task_info_states_and_next_run(tmp_path, agents):
    agents.mkdir()
    for name in ("ORBTradingBot", "RSI2TradingBot", "ExpiryGuardBot"):
        p = macos.plist_for({"name": name, "ny": "09:30"}, DAYS, tmp_path, "/py", WEEK, NY)
        (agents / f"{p['Label']}.plist").write_bytes(plistlib.dumps(p))
    fake = FakeLaunchctl(jobs={"com.tradingbots.ORBTradingBot": 777, "com.tradingbots.RSI2TradingBot": None,
                               "com.tradingbots.ExpiryGuardBot": None},
                         disabled={"com.tradingbots.ExpiryGuardBot"})
    info = macos.task_info(["ORBTradingBot", "RSI2TradingBot", "ExpiryGuardBot", "DailyReviewBot"],
                           now=datetime(2026, 11, 18, 8, 0), run=fake)
    assert info["ORBTradingBot"] == {"state": "Running", "next_at": "2026-11-18T09:30:00"}
    assert info["RSI2TradingBot"]["state"] == "Ready"
    assert info["ExpiryGuardBot"]["state"] == "Disabled"
    assert info["DailyReviewBot"]["state"] == "Missing"


def test_start_task_kickstarts_the_job():
    fake = FakeLaunchctl()
    assert macos.start_task("ORBTradingBot", run=fake)
    assert fake.calls[-1] == ["kickstart", f"gui/{macos._uid()}/com.tradingbots.ORBTradingBot"]


def test_the_real_schedule_registers(tmp_path, agents):
    lines = macos.register_all(python="/py", run=FakeLaunchctl(), today=WEEK, tz=NY)
    names = [t["name"] for t in macos.load_schedule()["tasks"]]
    assert len(lines) == len(names) == len(list(agents.glob("*.plist")))


# ---------------------------------------------------------------- run_task

class FakeRun:
    def __init__(self, codes=None):
        self.calls, self.codes = [], dict(codes or {})

    def __call__(self, args, **kw):
        self.calls.append(args)
        key = args[2] if len(args) > 2 and args[1] == "-m" else args[0]
        return NS(returncode=self.codes.get(key, 0), stdout=b"")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(run_task, "REPO", tmp_path)
    monkeypatch.delenv("TB_RUN_TASK_DRY", raising=False)
    return tmp_path


def test_rsi2_job_runs_ma_crossover_after_it_even_if_rsi2_fails(repo):
    fake = FakeRun({"tradingbot.run_rsi2_bot": 3})
    assert run_task.run_steps("RSI2TradingBot", run=fake) == 3
    assert [c[2] for c in fake.calls] == ["tradingbot.run_rsi2_bot", "tradingbot.run_ibkr_bot"]
    today = date.today().isoformat()
    assert (repo / "logs" / f"rsi2_{today}.log").exists() and (repo / "logs" / f"ma_crossover_{today}.log").exists()


def test_control_server_only_runs_in_its_hours(repo):
    fake = FakeRun()
    assert run_task.control_server(datetime(2026, 11, 21, 9, 0, tzinfo=NY), run=fake) == 0   # Saturday
    assert run_task.control_server(datetime(2026, 11, 18, 18, 30, tzinfo=NY), run=fake) == 0  # after 6pm
    assert fake.calls == []
    run_task.control_server(datetime(2026, 11, 18, 8, 0, tzinfo=NY), run=fake)
    assert fake.calls[0][2:] == ["tradingbot.run_control_server", "--open", "--until", "18:00"]


def test_daily_review_without_claude_still_commits_and_rebuilds(repo, monkeypatch):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "daily_review_prompt.md").write_text("Review {DATE}")
    monkeypatch.setattr(run_task, "_claude", lambda: None)
    fake = FakeRun()
    assert run_task.daily_review("2026-11-18", run=fake) == 0
    flat = [" ".join(map(str, c)) for c in fake.calls]
    assert "tradingbot.run_daily_review 2026-11-18" in flat[0]
    assert any(c.startswith("git commit -m Daily review: 2026-11-18") for c in flat)
    assert "tradingbot.run_dashboard" in flat[-1]
    assert "Claude Code isn't installed" in (repo / "logs" / "daily_review_2026-11-18.log").read_text()


def test_daily_review_saves_claudes_reply(repo, monkeypatch):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "daily_review_prompt.md").write_text("Review {DATE} in {YEAR}/{MONTH}/{DAY}")
    monkeypatch.setattr(run_task, "_claude", lambda: "/bin/claude")
    seen = {}

    def fake(args, **kw):
        if args[0] == "/bin/claude":
            seen["args"] = args
            return NS(returncode=0, stdout="# Review — 18 Nov\n".encode("utf-8"))
        return NS(returncode=0, stdout=b"")
    run_task.daily_review("2026-11-18", run=fake)
    assert seen["args"][2] == "Review 2026-11-18 in 2026/11/18"
    assert "Write" in seen["args"] and seen["args"].index("Write") > seen["args"].index("--disallowedTools")
    saved = repo / "analysis" / "daily" / "2026" / "11" / "18" / "review_2026-11-18.md"
    assert saved.read_text(encoding="utf-8").startswith("# Review — 18 Nov")


def test_dry_run_only_logs(repo, monkeypatch):
    monkeypatch.setenv("TB_RUN_TASK_DRY", "1")
    fake = FakeRun()
    assert run_task.run_steps("ORBTradingBot", run=fake) == 0 and fake.calls == []
    log = repo / "logs" / f"orb_{date.today().isoformat()}.log"
    assert "would run python -m tradingbot.run_orb_bot" in log.read_text()


def test_unknown_task_is_refused():
    assert run_task.main(["NoSuchBot"]) == 2


def test_real_subprocess_is_the_default():
    assert run_task.run_module.__defaults__[0] is subprocess.run
