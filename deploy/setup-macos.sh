#!/usr/bin/env bash
# One-shot setup for the watcher on a Mac.
#
# Does the mechanical half: checks the macOS version, downloads the branch,
# builds the venv, installs dependencies with Playwright pinned to a build whose
# Chromium still runs on macOS 11, and proves Chromium actually launches.
# Config and the Facebook login stay manual — both need a person at the machine.
#
#   ./setup-macos.sh [destination]
#
# Default destination: ~/fb-iphone-watcher
set -euo pipefail

REPO=https://github.com/Numba1ne/fb-iphone-watcher
BRANCH=freshness-gate
# Playwright's floor moved past macOS 11 in later 1.x releases. This one still
# ships a Chromium that launches on Big Sur; apps requiring a newer OS are a
# different problem, fixed by a newer Mac, not a newer pin.
PINNED_PLAYWRIGHT=1.49.1

DEST=${1:-$HOME/fb-iphone-watcher}
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

# --- 1. the OS ---------------------------------------------------------------
os=$(sw_vers -productVersion)
major=${os%%.*}
[ "$major" -ge 11 ] || die "macOS $os is too old: Chromium needs 11 (Big Sur) or newer."
echo "macOS $os — ok"

# --- 2. a Python -------------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
        PY=$candidate
        break
    fi
done
[ -n "$PY" ] || die "No Python 3.11+ found.
  Install the universal2 build from https://www.python.org/downloads/macos/
  (Homebrew no longer supports macOS 11), then run this again."
echo "Using $("$PY" --version) at $(command -v "$PY")"

# --- 3. the code -------------------------------------------------------------
if [ -e "$DEST" ]; then
    die "$DEST already exists — move it aside or pass a different destination."
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Downloading $BRANCH..."
curl -fsSL "$REPO/archive/refs/heads/$BRANCH.zip" -o "$tmp/w.zip"
unzip -q "$tmp/w.zip" -d "$tmp"
mkdir -p "$(dirname "$DEST")"
mv "$tmp/fb-iphone-watcher-$BRANCH" "$DEST"

# --- 4. the environment ------------------------------------------------------
cd "$DEST"
"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
# Downgrade after requirements: the constraint allows the newest 1.x, and the
# newest Chromium may no longer support this macOS.
.venv/bin/pip install --quiet "playwright==$PINNED_PLAYWRIGHT"
echo "Downloading Chromium (~150 MB)..."
.venv/bin/python -m playwright install chromium

# --- 5. prove Chromium launches ----------------------------------------------
.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("<h1>ok</h1>")
    if page.inner_text("h1") != "ok":
        raise SystemExit("Chromium launched but rendered nothing")
    browser.close()
print("Chromium launches — ok")
PY

cat <<DONE

  Installed in $DEST

  Left to do by hand:
    1. cp config.example.yaml config.yaml   (her location, prices, filters)
    2. cp .env.example .env                 (Telegram token or Discord webhook)
    3. .venv/bin/python main.py --login     (log in once, then close the window)
    4. .venv/bin/python main.py             (the first poll is silent)

  Keeping it running: deploy/README.md has the LaunchAgent, and the main
  README's "Running it continuously" covers sleep settings.
DONE
