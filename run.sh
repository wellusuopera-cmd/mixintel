#!/bin/bash
# MixIntel — live, glanceable audio-health intelligence for FOH.
# Advisory only. Fed by Master Ears' live_listener.py over HTTP push.
cd "$(dirname "$0")"
source .venv/bin/activate
python3 app.py
