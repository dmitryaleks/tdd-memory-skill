#!/bin/sh
# Thin launcher: the real installer is install.py, so the logic lives in one
# tested place instead of being duplicated across two shells.
set -e
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        exec "$py" "$dir/install.py" "$@"
    fi
done
echo "install: need python 3.8+ on PATH" >&2
exit 1
