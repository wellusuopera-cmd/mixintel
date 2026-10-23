# MixIntel — live, glanceable audio-health intelligence

Advisory only. Reads audio, never writes to a console/interface/fader.
Nothing here auto-applies anything, ever.

## Real deployment: livestream Mac mini, post-processing capture

Your actual signal chain:

```
X32 (stage) --USB--> iMac (FOH vocal processing) --back to X32--
X32 --Dante--> Mac mini (Dante Via) --> Reaper (livestream processing)
                                           |
                                        [stream out]
```

The raw X32/Dante feed isn't the useful capture point — by the time it
reaches Reaper on the Mac mini, it's already been through the iMac's vocal
processing chain and whatever else Reaper itself does before it goes out
to the stream. **"Real intelligence on processed tracks & master" means
capturing AFTER that processing, inside Reaper's own session on the Mac
mini** — not the raw stage-box channels.

### How audio gets from Reaper into MixIntel

macOS has no built-in way for one app to "listen in" on another app's
audio. The standard free fix is a **virtual loopback audio driver**:
install BlackHole, add Reaper sends that route specific
tracks/buses to BlackHole output channels, and MixIntel captures from
BlackHole like it would from any other named audio device — this is the
exact same mechanism already validated in this project (see `usb_capture_listener.py`,
tested end-to-end through a real BlackHole 2ch loopback).

### Setup (run once on the Mac mini)

1. **Get this folder onto the Mac mini:**
   ```
   git clone https://github.com/wellusuopera-cmd/mixintel.git
   cd mixintel
   ```
   (Private repo — the Mac mini needs to be logged into the same
   `wellusuopera-cmd` GitHub account, or use `gh auth login` there first.)

2. **Run setup:**
   ```
   cd mixintel
   ./setup.sh
   ```

3. **Install BlackHole 16ch** (16 channels = room for master L/R + several
   processed tracks simultaneously; the free 2ch version only fits one
   stereo pair):
   ```
   brew install blackhole-16ch
   ```
   (You may need to reboot or re-launch Audio MIDI Setup once for it to
   show up as a selectable device everywhere.)

4. **In Reaper, add sends to BlackHole** for everything you want MixIntel
   watching. For each track (e.g. Master, Processed Vocal):
   - Right-click the track's routing matrix (or `Track > Routing` /
     the "Route" button in the TCP) → **Add new send**
   - Destination: **Hardware Output → BlackHole 16Ch**
   - Pick which BlackHole output channels this track lands on — e.g.:
     - Master bus → BlackHole out 1/2 (stereo)
     - Processed Vocal → BlackHole out 3 (mono) or 3/4 (stereo)
     - Add more tracks to more channel pairs as needed, one pair per
       thing you want monitored
   - **Keep the existing send to your actual stream/output device too** —
     this is an ADDITIONAL send, not a replacement. MixIntel listening in
     must never change what actually goes out to the stream.

5. **Find the BlackHole device name and test capture:**
   ```
   source .venv/bin/activate
   python3 usb_capture_listener.py --list-devices
   ```
   Should show `BlackHole 16ch` in the list.

6. **Run MixIntel itself** (the web app):
   ```
   ./run.sh
   ```
   Open `http://localhost:5051` (or `http://<mac-mini-ip>:5051` from
   another device on the same network) to see the live view.

7. **Run the capture listener**, mapping the BlackHole channels you routed
   in step 4 to names (1-indexed, matching how channel numbers are
   normally talked about):
   ```
   python3 usb_capture_listener.py --device "BlackHole 16ch" \
     --channels "1:Master,3:Vocal"
   ```
   (Channel 1 = BlackHole output 1, which you routed the Master bus to in
   step 4; channel 3 = wherever you routed the processed vocal. Adjust to
   match your actual Reaper routing.)

Both `run.sh` (the web app) and `usb_capture_listener.py` (the capture
process) can run in the background on the Mac mini at all times — the app
just sits idle/"waiting for signal" until the capture listener starts
pushing ticks, same degraded-gracefully behavior it already has.

### Why everything runs on the Mac mini here (not a separate machine)

An earlier design (Phase 1, Oct 2026) deliberately kept the live-analysis
engine OFF the Mac mini, for RAM headroom (16GB, Ollama near ceiling) and
crash-isolation reasons — but that was about Master Ears' full file-based
analysis engine, which pulls in Demucs/torch (heavy). MixIntel's live
capture + scoring path (`bark_scoring.py`, `usb_capture_listener.py`) was
deliberately built dependency-light — numpy + scipy only, no torch, no
phantom, no pydantic — specifically so it's safe to run alongside Reaper on
the same machine. The Flask-SocketIO web app itself is also lightweight
(just broadcasting small JSON payloads over WebSocket). If RAM pressure
becomes a real issue in practice, the capture+scoring process is the piece
to move to a separate machine first — the web app can stay wherever.

## Architecture (general)

- `app.py` — Flask + Flask-SocketIO server. Receives ticks via POST
  `/ingest`, broadcasts over WebSocket to every connected browser
  immediately (push, not poll).
- `templates/live.html` — the glanceable live view. Huge advisory banner
  for the single worst thing happening right now, channel tiles, LIVE/STALE
  status. Designed for a screen glanced at from across a room, not read
  closely (large type, color+icon+label together, no dense tables).
- `bark_scoring.py` — vendored, dependency-light Bark-scale psychoacoustic
  masking (Johnston 1988 spreading function) + mud/harshness/sibilance
  tonal detection. Numerically validated against the original
  `~/Projects/master-ears/bark_masking.py`.
- `usb_capture_listener.py` — direct multichannel audio capture via
  `sounddevice`/PortAudio from any named input device (BlackHole, a
  USB-connected X32, or a Dante Virtual Soundcard/Via device all work the
  same way — the OS just sees "a named audio device with N channels").
