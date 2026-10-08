#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
pick=""
for name in python3 python3.13 python3.12 python3.11 python3.10 python; do
  if command -v "$name" >/dev/null 2>&1; then
    if "$name" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' >/dev/null 2>&1; then
      pick="$name"
      break
    fi
  fi
done
if [ -z "$pick" ]; then
  echo "Python 3.10 or newer is required."
  echo "On a Mac, install it from python.org. On Linux: sudo apt install python3 python3-venv python3-pip"
  read -r -p "Press Enter to close."
  exit 1
fi
"$pick" launch.py "$@"
status=$?
if [ "$status" -ne 0 ]; then
  read -r -p "Press Enter to close."
fi
exit "$status"
