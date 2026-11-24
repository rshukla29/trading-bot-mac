#!/bin/bash
# Installs a new release (a vX.Y.Z tag). Started by the dashboard's Update now
# button (updates.start_update); can also be run by hand in Terminal:
#     bash ~/trading-bot/scripts/update.sh            # newest release
#     bash ~/trading-bot/scripts/update.sh v1.3.0
#
# Never touches .env, data/ (trade diary, controls) or the reports. Steps:
# fast-forward to the release, reinstall the Python packages, check the code
# loads, re-register the launchd jobs, restart the control server.
# If any step fails it goes back to the previous version.
# The outcome is written to data/update_result.json (shown on the dashboard).
# --no-tasks / --no-restart: for trying it on a spare copy, without touching
# this Mac's launchd jobs or the running control server.

cd "$(dirname "$0")/.." || exit 1          # the repo folder
export GIT_TERMINAL_PROMPT=0
PY=".venv/bin/python"
VERSION=""; NO_TASKS=0; NO_RESTART=0
for a in "$@"; do
    case "$a" in
        --no-tasks) NO_TASKS=1 ;;
        --no-restart) NO_RESTART=1 ;;
        *) VERSION="$a" ;;
    esac
done
TODAY=$(date +%Y-%m-%d)

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
version() { "$PY" -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"; }
result() {  # ok(true|false) from to message
    mkdir -p data
    "$PY" - "$1" "$2" "$3" "$4" <<'EOF'
import json, sys
from datetime import datetime
ok, frm, to, msg = sys.argv[1:5]
json.dump({"ok": ok == "true", "from": frm, "to": to, "message": msg,
           "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}, open("data/update_result.json", "w"), indent=2)
EOF
}
register_tasks() {
    [ "$NO_TASKS" = 1 ] && return 0
    "$PY" -m tradingbot.macos register --skip-running
}
in_hours() {  # Mon-Fri 8am-6pm New York
    "$PY" -c "
from datetime import datetime, time; from zoneinfo import ZoneInfo
n = datetime.now(ZoneInfo('America/New_York'))
raise SystemExit(0 if n.weekday() < 5 and time(8) <= n.time() < time(18) else 1)"
}
restart_server() {
    # Stop the running control server, then start the new code: the launchd job
    # during its hours, otherwise one that closes itself when idle. An open
    # dashboard tab reloads itself on the new build.
    [ "$NO_RESTART" = 1 ] && return 0
    pkill -f "tradingbot.run_control_server" 2>/dev/null
    sleep 2
    [ "$NO_TASKS" = 1 ] || "$PY" -m tradingbot.macos register --skip-running >/dev/null 2>&1
    if in_hours && launchctl print "gui/$(id -u)/com.tradingbots.BotControlServer" >/dev/null 2>&1; then
        launchctl kickstart "gui/$(id -u)/com.tradingbots.BotControlServer"
    else
        nohup "$PY" -m tradingbot.run_control_server --open --idle 30 >> "logs/control_server_$TODAY.log" 2>&1 &
    fi
}

FROM=$(version)
say "Updating from $FROM"

# 1. Nothing of yours in the way: no edited code files.
DIRTY=$(git status --porcelain --untracked-files=no)
if [ -n "$DIRTY" ]; then
    say "Edited files would be overwritten:"; echo "$DIRTY"
    FILES=$(echo "$DIRTY" | cut -c4- | paste -sd, - | sed 's/,/, /g')
    result false "$FROM" "$FROM" "Not updated: these files were edited on this Mac: $FILES. Undo the edits (or ask for help), then try again."
    restart_server; exit 1
fi

# 2. Get the release.
if ! git fetch --tags --quiet origin; then
    result false "$FROM" "$FROM" "Not updated: couldn't reach GitHub. Check the internet connection and try again."
    restart_server; exit 1
fi
if [ -z "$VERSION" ]; then
    VERSION=$("$PY" -c "import subprocess; from tradingbot import updates
print(updates.latest_tag(subprocess.run(['git', 'tag', '--list', 'v*'], capture_output=True, text=True).stdout.split()) or '')")
fi
if [ -z "$VERSION" ] || ! git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
    result false "$FROM" "$FROM" "Not updated: release $VERSION not found."
    restart_server; exit 1
fi
OLD=$(git rev-parse HEAD)

rollback() {
    say "FAILED: $1 - going back to $FROM"
    git merge --abort >/dev/null 2>&1
    git reset --hard --quiet "$OLD"
    "$PY" -m pip install --quiet -e ".[dev]" >/dev/null 2>&1
    register_tasks >/dev/null 2>&1
    result false "$FROM" "$FROM" "Update to ${VERSION#v} failed ($1). Still on $FROM; nothing else changed. Details: logs/update_$TODAY.log"
    restart_server; exit 1
}

# The bots commit each day's reports and reviews to this copy, so it usually
# has commits of its own: merge the release in, keeping them. Where a file
# differs on both sides, the release's version wins (code is never edited
# here: step 1 refused edited files).
git merge --no-edit --quiet -X theirs -m "Update to $VERSION" "$VERSION" \
    || rollback "the release couldn't be merged with this copy's own commits"
# Reports and reviews stay on this Mac only (no longer in git): stop tracking
# any this copy committed itself. The files are kept.
if [ -n "$(git ls-files reports analysis)" ]; then
    git rm -r --cached --quiet --ignore-unmatch reports analysis \
        && git commit --quiet -m "Keep this Mac's reports out of git" \
        || rollback "couldn't stop tracking this Mac's reports"
fi

# 3. Packages, a load check, the launchd jobs.
"$PY" -m pip install --quiet -e ".[dev]" || rollback "installing the Python packages failed"
"$PY" -c "import tradingbot.run_control_server, tradingbot.run_orb_bot, tradingbot.dashboard, tradingbot.run_task" \
    || rollback "the new code didn't load"
register_tasks || rollback "re-registering the launchd jobs failed"

TO=$(version)
say "Updated $FROM -> $TO"
result true "$FROM" "$TO" "Updated from $FROM to $TO."
restart_server
exit 0
