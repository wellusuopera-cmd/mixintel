#!/bin/bash
# One-time setup on a new machine (e.g. the Mac mini livestream computer).
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
echo ""
echo "Setup done. Next: install BlackHole (virtual audio loopback) if not"
echo "already present:"
echo "  brew install blackhole-16ch"
echo ""
echo "Then route Reaper's master bus + processed channels to BlackHole"
echo "outputs -- see README.md for exact steps."
