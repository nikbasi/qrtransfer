#!/bin/sh
# Double-click on macOS (or run on Linux). If macOS says it cannot be opened: right-click > Open once.
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then python3 run.py; else echo "Python 3 is needed: https://www.python.org/downloads/"; fi
printf "\nPress Enter to close this window."; read _
