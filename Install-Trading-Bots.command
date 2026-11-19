#!/bin/bash
# One-click installer for the trading bots on a Mac (docs/SETUP.md, step 4).
# Double-click it (it comes in Install-Trading-Bots-mac.zip, which keeps it
# runnable). It installs Apple's command line tools (for Git) and Python if
# they're missing, downloads the bots into ~/trading-bot and runs
# scripts/install.sh. Safe to run again: an existing download, .env and data are kept.
#
# For testing: TB_INSTALL_DIR (where to download), TB_REPO_URL (what to clone),
# TB_SETUP_TEST=1 (no pop-ups, default name), TB_FORCE_PYTHON_INSTALL=1
# (install python.org's Python even if one is found); see scripts/install.sh
# for the rest.

REPO_URL="${TB_REPO_URL:-https://github.com/rshukla29/trading-bot-mac.git}"
# Not in Documents: macOS often syncs Documents to iCloud, which would move
# the trade diary and the Python packages off this Mac while the bots run.
DIR="${TB_INSTALL_DIR:-$HOME/trading-bot}"
TEST="${TB_SETUP_TEST:-0}"
GUIDE="https://github.com/rshukla29/trading-bot-mac/blob/master/docs/SETUP.md"
PY_VERSION="3.12.10"
PY_PKG_URL="https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-macos11.pkg"
PY_FRAMEWORK="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"

step() { echo; printf '\033[36m== %s\033[0m\n' "$*"; }
ok() { printf '   \033[32mOK\033[0m  %s\n' "$*"; }
dialog() {  # text [buttons...]: the button clicked
    local text="$1"; shift
    [ "$TEST" = 1 ] && { echo "${1:-OK}"; return; }
    local buttons; buttons=$(printf '"%s",' "${@:-OK}"); buttons="{${buttons%,}}"
    osascript -e 'on run argv' \
        -e "button returned of (display dialog (item 1 of argv) with title \"Trading bots installer\" buttons $buttons default button 1)" \
        -e 'end run' "$text" 2>/dev/null
}
ask() {  # prompt default: what was typed (the default in test mode or on Cancel)
    [ "$TEST" = 1 ] && { echo "$2"; return; }
    osascript -e 'on run argv' \
        -e 'text returned of (display dialog (item 1 of argv) default answer (item 2 of argv) with title "Trading bots installer")' \
        -e 'end run' "$1" "$2" 2>/dev/null || echo "$2"
}
fail() {
    printf '   \033[31mPROBLEM\033[0m  %s\n\n' "$*"
    [ "$TEST" = 1 ] || dialog "$* Fix that, then double-click the installer again. Running it twice is safe." >/dev/null
    [ -t 0 ] && [ "$TEST" != 1 ] && read -r -p "Press Enter to close "
    exit 1
}
as_admin() {  # run a command as administrator: macOS asks for the Mac password
    if sudo -n true 2>/dev/null; then sudo -n "$@"; return; fi
    [ "$TEST" = 1 ] && return 1
    local cmd; cmd=$(printf '%q ' "$@")
    osascript -e 'on run argv' -e 'do shell script (item 1 of argv) with administrator privileges' -e 'end run' "$cmd" >/dev/null
}
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }
find_python() {
    local c p
    for c in python3.13 python3.12 python3.11 python3 \
             /Library/Frameworks/Python.framework/Versions/3.1{3,2,1}/bin/python3 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        p=$(command -v "$c" 2>/dev/null) || continue
        [ "$p" = /usr/bin/python3 ] && continue   # Apple's 3.9, and a pop-up without the Xcode tools
        python_ok "$p" && { echo "$p"; return 0; }
    done
    return 1
}
git_ok() {
    local g; g=$(command -v git 2>/dev/null) || return 1
    # /usr/bin/git only works once Apple's command line tools are installed.
    [ "$g" != /usr/bin/git ] || xcode-select -p >/dev/null 2>&1
}

clear 2>/dev/null
echo "Trading bots installer (macOS)"
if [ "$(dialog "This installs the trading bots on this Mac (paper trading only).

It installs Apple's command line tools (for Git) and Python if they're missing, downloads the bots into
$DIR
and sets up the scheduled jobs, the Trading dashboard app and a weekday wake-up.

It takes about 10-20 minutes. macOS may ask for your Mac password: that's macOS asking, not the bots." "OK" "Cancel")" != "OK" ]; then
    exit 1
fi

step "Git (Apple's command line tools)"
if ! git_ok; then
    echo "   Installing Apple's command line tools: click Install in the window that opens (5-15 minutes)."
    xcode-select --install >/dev/null 2>&1
    dialog "A macOS window asks to install the \"command line developer tools\". Click Install, then Agree, and wait for it to finish.

This window carries on by itself when they're installed." >/dev/null &
    for _ in $(seq 1 360); do git_ok && break; sleep 10; printf '.'; done
    echo
    git_ok || fail "Apple's command line tools didn't install. Open Terminal, type: xcode-select --install and follow the window."
fi
ok "$(git --version)"

step "Python"
PYTHON=""
[ "${TB_FORCE_PYTHON_INSTALL:-0}" = 1 ] || PYTHON=$(find_python)
if [ -z "$PYTHON" ]; then
    echo "   Downloading Python $PY_VERSION from python.org..."
    PKG="${TMPDIR:-/tmp}/tb-python-$PY_VERSION.pkg"
    curl -fsSL --retry 3 -o "$PKG" "$PY_PKG_URL" || fail "Downloading Python failed. Check the internet connection."
    echo "   Installing it (macOS asks for your Mac password)..."
    as_admin /usr/sbin/installer -pkg "$PKG" -target / >/dev/null || fail "Installing Python failed (or the password was cancelled)."
    rm -f "$PKG"
    CERTS="/Applications/Python 3.12/Install Certificates.command"
    [ -f "$CERTS" ] && /bin/sh "$CERTS" >/dev/null 2>&1
    PYTHON="$PY_FRAMEWORK"
    python_ok "$PYTHON" || fail "Python didn't install. Install Python 3.12 from python.org, then run this again."
fi
ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())')"

step "Downloading the bots into $DIR"
if [ -d "$DIR/.git" ]; then
    ok "Already downloaded - kept as it is (updates come from the dashboard)"
else
    if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
        fail "The folder $DIR already exists but isn't a download of the bots. Rename or move it, then run this again."
    fi
    mkdir -p "$(dirname "$DIR")"
    git clone --quiet "$REPO_URL" "$DIR" && [ -f "$DIR/scripts/install.sh" ] \
        || fail "Downloading the bots failed. Check the internet connection."
    ok "Downloaded"
fi

step "Your name for the saved reports"
# The daily reports are saved as Git commits, which need a name. It stays on this Mac.
NAME=$(git -C "$DIR" config user.name)
if [ -z "$NAME" ]; then
    NAME=$(ask "Your name (the bots stamp it on the daily reports they save; it stays on this Mac):" "$(id -F 2>/dev/null || echo "$USER")")
    [ -n "$NAME" ] || NAME="$USER"
    EMAIL=$(ask "Your email (optional, same use; leave it blank to skip):" "")
    [ -n "$EMAIL" ] || EMAIL="$USER@localhost"
    git -C "$DIR" config user.name "$NAME"
    git -C "$DIR" config user.email "$EMAIL"
fi
ok "$NAME"

step "Setting up the bots (scripts/install.sh)"
TB_PYTHON="$PYTHON" /bin/bash "$DIR/scripts/install.sh" || exit $?   # install.sh already showed the problem

if [ "$TEST" != 1 ] && [ "$(dialog "The trading bots are installed.

Next: check the settings file and set up Trader Workstation (TWS) - steps 6 to 8 of the setup guide. Open the guide now?" "Yes" "No")" = "Yes" ]; then
    open "$GUIDE#step-6-check-the-settings-file"
fi
[ -t 0 ] && [ "$TEST" != 1 ] && read -r -p "Press Enter to close this window "
exit 0
