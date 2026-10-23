"""MixIntel — live, glanceable audio-health intelligence for the FOH engineer.

Receives per-tick snapshots pushed by Master Ears' live_listener.py (which
reads real-time Bark-band energies off Reaper via OSC) and immediately
broadcasts them over WebSocket to any connected browser — a second
monitor/tablet at front-of-house, meant to be glanced at, not read.

Advisory only. Nothing in this app, or upstream of it, ever touches a
fader/EQ/gain automatically — see Master Ears Action Plan's live-scope
decision. This is a "here's what's happening and what to consider" screen,
full stop.
"""
from __future__ import annotations

import time

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

app = Flask(__name__)
app.secret_key = "mixintel-dev"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# In-memory only — this is a live view, not a record. If the app restarts
# mid-service it just shows "no signal" until the next tick arrives from
# live_listener.py, which is the correct degraded behavior (same choice
# Master Ears' own Live tab made).
_state = {
    "last_update": None,
    "ticker_text": None,
    "channels": {},
    "relative_loudness": [],
    "masking_pairs": [],
}


@app.route("/")
def live_view():
    return render_template("live.html")


@app.route("/state")
def state():
    """REST fallback for first paint / reconnect, before any socket push
    has arrived on this particular browser connection."""
    return jsonify(_state)


@app.route("/ingest", methods=["POST"])
def ingest():
    """live_listener.py POSTs here every tick (~3-5s). We store it and
    immediately broadcast to every connected browser over WebSocket —
    push, not poll, so the screen updates the moment new data exists."""
    payload = request.get_json(silent=True) or {}
    _state["last_update"] = time.time()
    _state["ticker_text"] = payload.get("ticker_text")
    _state["channels"] = payload.get("channels", {})
    _state["relative_loudness"] = payload.get("relative_loudness", [])
    _state["masking_pairs"] = payload.get("masking_pairs", [])
    socketio.emit("tick", _state)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("MixIntel live monitoring — advisory only, nothing here touches a fader.")
    socketio.run(app, host="0.0.0.0", port=5051, debug=True, allow_unsafe_werkzeug=True)
