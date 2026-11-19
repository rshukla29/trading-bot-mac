#!/bin/bash
# Sets up (or repairs) the bots on this Mac. Run by Install-Trading-Bots.command;
# can also be run by hand in Terminal:
#     bash ~/trading-bot/scripts/install.sh
#
# It checks Python and Git, installs the bots' Python packages into .venv,
# creates .env from .env.example (if there isn't one), runs the tests,
# creates the launchd jobs (New York times converted to this Mac's clock),
# puts a "Trading dashboard" app on the Desktop and sets a 7:50am weekday
# wake-up (asks for your Mac password). Safe to run again: it keeps an
# existing .env, your trades and reports, and replaces the jobs.
#
#   TB_PYTHON=/path/python3   the Python to build .venv with (the installer passes it)
#   TB_SKIP_TASKS=1           no launchd jobs and no wake-up
#   TB_SETUP_TEST=1           no pop-ups or prompts (automated tests)
#   TB_DRY_JOBS=1             jobs only log what they would run (automated tests)
#   TB_DESKTOP=/path          where to put the dashboard app (default ~/Desktop)

cd "$(dirname "$0")/.." || exit 1          # the repo folder, wherever it was cloned
REPO=$(pwd)
TEST="${TB_SETUP_TEST:-0}"

step() { echo; printf '\033[36m== %s\033[0m\n' "$*"; }
ok() { printf '   \033[32mOK\033[0m  %s\n' "$*"; }
fail() {
    printf '   \033[31mPROBLEM\033[0m  %s\n\n' "$*"
    if [ "$TEST" != 1 ]; then
        osascript -e 'on run argv' -e 'display dialog (item 1 of argv) with title "Trading bots installer" buttons {"OK"} default button 1 with icon stop' -e 'end run' \
            "$* Fix that, then run the installer again. Running it twice is safe." >/dev/null 2>&1
        [ -t 0 ] && read -r -p "Press Enter to close "
    fi
    exit 1
}
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
find_python() {
    # /usr/bin/python3 is skipped: it's Apple's 3.9 (too old), and without the
    # Xcode tools it opens an "install the developer tools" window.
    local c p
    for c in python3.13 python3.12 python3.11 python3 \
             /Library/Frameworks/Python.framework/Versions/3.1{3,2,1}/bin/python3 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        p=$(command -v "$c" 2>/dev/null) || continue
        [ "$p" = /usr/bin/python3 ] && continue
        python_ok "$p" && { echo "$p"; return 0; }
    done
    return 1
}

step "Checking Python and Git"
PYTHON="${TB_PYTHON:-}"
[ -n "$PYTHON" ] && python_ok "$PYTHON" || PYTHON=$(find_python) \
    || fail "Python 3.11 or newer not found. Run Install-Trading-Bots (it installs Python), or install it from python.org."
ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())') ($PYTHON)"
git --version >/dev/null 2>&1 || fail "Git not found. Run Install-Trading-Bots (it installs Apple's command line tools, which include Git)."
ok "$(git --version)"
if [ -z "$(git config user.name)" ]; then
    NAME="$USER"
    if [ "$TEST" != 1 ] && [ -t 0 ]; then
        echo "   Git needs a name once (the reports are saved as Git commits; it stays on this Mac)."
        read -r -p "   Your name: " N; [ -n "$N" ] && NAME="$N"
    fi
    git config user.name "$NAME"
    [ -n "$(git config user.email)" ] || git config user.email "$USER@localhost"
fi
ok "Git name set"

step "Installing the bots' Python packages (a few minutes the first time)"
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv || fail "Could not create .venv"
fi
.venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1
.venv/bin/python -m pip install --quiet -e ".[dev]" || fail "Installing packages failed (see the messages above). Check the internet connection."
ok "Packages installed"

step "Settings file (.env)"
if [ -f .env ]; then
    ok ".env already exists - kept as it is"
else
    cp .env.example .env
    ok "Created .env from .env.example - review it in TextEdit (docs/SETUP.md, step 6)"
fi
mkdir -p data logs reports

step "Running the tests"
.venv/bin/python -m pytest -q 2>&1 | tail -3
[ "${PIPESTATUS[0]}" = 0 ] || fail "Some tests failed. Don't run the bots until they pass."
ok "All tests passed"

if [ "${TB_SKIP_TASKS:-0}" = 1 ]; then
    step "Skipping the launchd jobs and the wake-up (TB_SKIP_TASKS=1)"
else
    step "Creating the launchd jobs (Mon-Fri, from scripts/schedule.json)"
    EXTRA=""; [ "${TB_DRY_JOBS:-0}" = 1 ] && EXTRA="--dry-run-jobs"
    .venv/bin/python -m tradingbot.macos register $EXTRA || fail "Creating the launchd jobs failed (see above)."

    step "Weekday wake-up (so the Mac is awake for the 8am start)"
    WAKE=$(.venv/bin/python -m tradingbot.macos wake)
    if sudo -n true 2>/dev/null; then
        sudo -n $WAKE && ok "$WAKE" || echo "   Couldn't set the wake-up (the readiness check will remind you)."
    elif [ "$TEST" != 1 ]; then
        echo "   macOS asks for your Mac password to set it."
        if osascript -e 'on run argv' -e 'do shell script (item 1 of argv) with administrator privileges' -e 'end run' "$WAKE" >/dev/null 2>&1; then
            ok "$WAKE"
        else
            echo "   Skipped (no password given). Run the installer again to set it; the readiness check reminds you."
        fi
    else
        echo "   Skipped in test mode (no administrator rights)."
    fi
fi

step "Trading dashboard app (on the Desktop)"
# Double-click: rebuilds the dashboard from the latest data and opens it through the
# control server, starting one if needed, so the Start / Pause / Stop buttons work any day.
DESK="${TB_DESKTOP:-$HOME/Desktop}"
mkdir -p "$DESK"
rm -rf "$DESK/Trading dashboard.app"
if osacompile -o "$DESK/Trading dashboard.app" \
    -e "do shell script \"cd \" & quoted form of \"$REPO\" & \" && .venv/bin/python -m tradingbot.run_dashboard --open > /dev/null 2>&1 &\"" \
    >/dev/null 2>&1; then
    ok "Trading dashboard (on the Desktop)"
else
    echo "   Couldn't create it. Open the dashboard with: .venv/bin/python -m tradingbot.run_dashboard --open"
fi

step "Done"
echo "   Next: the settings file, TWS and the first check - docs/SETUP.md, steps 6 to 8."
