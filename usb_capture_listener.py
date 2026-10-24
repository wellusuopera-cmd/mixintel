"""MixIntel — direct multichannel audio capture listener.

Replaces the Reaper/JSFX/OSC pipeline entirely: this reads live multichannel
audio DIRECTLY off a USB-connected X32 (its built-in class-compliant audio
interface) or a Dante network (via Dante Virtual Soundcard / Dante Via,
which exposes Dante channels as a normal CoreAudio multichannel device) —
whatever the OS sees as one named audio input device with N channels.

No Reaper, no JSFX, no ReaScript, no OSC. This machine captures raw audio
itself, computes the same Bark-band energy/tonality/masking math Master
Ears uses for file-based reports (via bark_scoring.py, vendored
dependency-light so this can run on a bare FOH laptop), and pushes ticks to
MixIntel's existing /ingest endpoint — the same payload shape the old OSC
path produced, so MixIntel itself needed zero changes.

Advisory only. This reads audio; it never writes to the console/interface.

Usage:
    # List available input devices to find your X32/Dante device name:
    python3 usb_capture_listener.py --list-devices

    # Run against it, with channel 1=Vocal, 2=Guitar, 3=Drums (1-indexed,
    # matching how X32/Dante channel numbers are normally talked about):
    python3 usb_capture_listener.py --device "X32" --channels "1:Vocal,2:Guitar,3:Drums"
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import sounddevice as sd

from bark_scoring import bark_band_energy_and_tonality, pairwise_bark_result, tonal_findings


def list_devices() -> None:
    print("Input-capable audio devices on this machine:\n")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  (max {d['max_input_channels']} input channels, {d['default_samplerate']:.0f}Hz)")
    print("\nWhen your X32 is connected via USB, or Dante Virtual Soundcard/Via is")
    print("running, its name will appear in this list — pass it (or a unique")
    print("substring of it) via --device.")


def parse_channel_map(spec: str) -> dict[int, str]:
    """'1:Vocal,2:Guitar,3:Drums' -> {0: 'Vocal', 1: 'Guitar', 2: 'Drums'}
    (converts the 1-indexed console-channel numbering people actually use
    to 0-indexed array positions internally)."""
    mapping = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        idx_str, name = part.split(":", 1)
        mapping[int(idx_str.strip()) - 1] = name.strip()
    return mapping


class RollingCapture:
    """Thread-safe rolling buffer per captured channel, fed by sounddevice's
    audio callback (runs on PortAudio's own thread — keep that callback
    fast, never block it on analysis)."""

    def __init__(self, channel_indices: list[int], samplerate: int, window_seconds: float):
        self.channel_indices = channel_indices
        self.samplerate = samplerate
        self.window_samples = int(window_seconds * samplerate)
        self._lock = threading.Lock()
        self._buffers = {idx: np.zeros(self.window_samples, dtype=np.float32) for idx in channel_indices}

    def callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[warn] audio stream status: {status}")
        with self._lock:
            for idx in self.channel_indices:
                if idx >= indata.shape[1]:
                    continue
                block = indata[:, idx]
                n = len(block)
                if n >= self.window_samples:
                    self._buffers[idx] = block[-self.window_samples:].copy()
                else:
                    self._buffers[idx] = np.concatenate([self._buffers[idx][n:], block])

    def snapshot(self) -> dict[int, np.ndarray]:
        with self._lock:
            return {idx: buf.copy() for idx, buf in self._buffers.items()}


def relative_loudness(channel_rms: dict[str, float], exclude: set[str] | None = None) -> list[dict]:
    """exclude: sum/bus channels (Master, VocSum, etc.) are not peers of the
    individual channels -- they're the aggregate OF those peers, so they're
    always louder by construction (summed signal). Including them in the
    "average of the rest of the mix" baseline skews every individual
    channel's comparison (this produced the +40dB readings across the
    board). Excluded channels just don't get a relative-loudness entry at
    all here -- their own absolute level is still visible via their own
    RMS meter on the live view, just not as a "vs. peers" comparison."""
    exclude = exclude or set()
    names = [n for n in channel_rms.keys() if n not in exclude]
    if len(names) < 2:
        return []
    rms_db = {n: 20.0 * np.log10(max(channel_rms[n], 1e-12)) for n in names}
    results = []
    for n in names:
        others = [rms_db[o] for o in names if o != n]
        avg_others = float(np.mean(others))
        deviation = rms_db[n] - avg_others
        results.append({"channel": n, "deviation_db": round(deviation, 1)})
    return results


def masking_pairs(channel_bands: dict[str, tuple], exclude: set[str] | None = None) -> list[dict]:
    """exclude: sum/bus channel names (Master, VocSum, DrSum, etc.) that
    should never be pairwise-compared against their own member channels --
    a sum bus's audio mathematically CONTAINS its members' audio, so any
    "masking" comparison against it is a guaranteed false-positive flood,
    not real intel. Sum channels still get their own loudness/tonal checks
    elsewhere, just skipped here."""
    exclude = exclude or set()
    names = [n for n in channel_bands.keys() if n not in exclude]
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a_name, b_name = names[i], names[j]
            ea, aa = channel_bands[a_name]
            eb, ab = channel_bands[b_name]
            result = pairwise_bark_result(ea, aa, eb, ab)
            worst_band = max(result.bands, key=lambda b: b.overlap_score).band if result.bands else None
            pairs.append({
                "channel_a": a_name, "channel_b": b_name,
                "overall_severity": result.overall_severity,
                "overall_score": result.overall_score,
                "worst_band": worst_band,
            })
    pairs.sort(key=lambda p: p["overall_score"], reverse=True)
    return pairs


def render_ticker(channel_bands: dict, rel_loudness: list[dict], pairs: list[dict], tonal: dict) -> str:
    if len(channel_bands) < 2:
        return "Waiting for at least 2 channels to report in..."
    lines = []
    for p in pairs:
        if p["overall_severity"] in ("high", "moderate"):
            lines.append(f"MASKING  {p['channel_a']} vs {p['channel_b']}: {p['overall_severity']} ({p['overall_score']}) in {p['worst_band']}")
    for name, findings in tonal.items():
        for f in findings:
            lines.append(f"TONAL    {f['message']}")
    for r in rel_loudness:
        if abs(r["deviation_db"]) >= 3.0:
            direction = "hot" if r["deviation_db"] > 0 else "buried"
            lines.append(f"LOUDNESS {r['channel']} running {direction} ({r['deviation_db']:+.1f}dB vs rest of mix)")
    if not lines:
        return "All clear -- no masking, tonal, or relative-loudness issues above threshold right now."
    return "\n".join(lines)


def run(args: argparse.Namespace) -> None:
    channel_map = parse_channel_map(args.channels)
    channel_indices = sorted(channel_map.keys())
    max_channel = max(channel_indices) + 1
    sum_channels = {n.strip() for n in args.sum_channels.split(",") if n.strip()}

    capture = RollingCapture(channel_indices, args.samplerate, args.window_seconds)

    device = args.device if args.device else None
    stream = sd.InputStream(
        device=device, channels=max_channel, samplerate=args.samplerate,
        callback=capture.callback, blocksize=0,
    )

    push_urls = [u.strip() for u in args.push_urls.split(",") if u.strip()]

    print(f"MixIntel direct-capture listener -- device={'default' if device is None else device}, "
          f"{len(channel_indices)} channel(s): {channel_map}")
    print("Advisory only -- this reads audio, it never writes to the console/interface.")
    if push_urls:
        print(f"Pushing ticks to: {', '.join(push_urls)}")

    with stream:
        time.sleep(args.window_seconds + 0.5)  # let the first window fill
        while True:
            raw = capture.snapshot()
            channel_bands = {}
            channel_rms = {}
            tonal = {}
            for idx, name in channel_map.items():
                mono = raw.get(idx, np.zeros(1))
                energies, alphas = bark_band_energy_and_tonality(mono, args.samplerate)
                channel_bands[name] = (energies, alphas)
                channel_rms[name] = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
                tonal[name] = tonal_findings(name, energies, alphas)

            pairs = masking_pairs(channel_bands, exclude=sum_channels) if len(channel_bands) >= 2 else []
            rel = relative_loudness(channel_rms, exclude=sum_channels)
            ticker_text = render_ticker(channel_bands, rel, pairs, tonal)

            print("\n" + "=" * 60)
            print(time.strftime("%H:%M:%S"), f"-- {len(channel_bands)} channel(s)")
            print(ticker_text)

            if push_urls:
                payload = {
                    "ticker_text": ticker_text,
                    "channels": {
                        name: {"overall_rms": channel_rms[name], "tonal_findings": tonal[name]}
                        for name in channel_map.values()
                    },
                    "relative_loudness": rel,
                    "masking_pairs": pairs,
                }
                body = json.dumps(payload).encode()
                for url in push_urls:
                    try:
                        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                        urllib.request.urlopen(req, timeout=3)
                    except (urllib.error.URLError, OSError) as e:
                        print(f"[warn] couldn't push to {url}: {e}")

            time.sleep(args.tick_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-devices", action="store_true", help="List input-capable audio devices and exit.")
    parser.add_argument("--device", default=None, help="Device name or substring (e.g. 'X32', 'Dante'). Omit for system default.")
    parser.add_argument("--channels", default="", help="Channel map, 1-indexed, e.g. '1:Vocal,2:Guitar,3:Drums'.")
    parser.add_argument("--sum-channels", default="",
                         help="Comma-separated names (matching the ones you gave in --channels) of sum/bus "
                              "channels (Master, VocSum, DrSum, etc.) to exclude from pairwise masking "
                              "comparison -- a sum bus's audio contains its own members' audio, so comparing "
                              "it against them always reads as total overlap, which is a false positive, not "
                              "real intel. These channels still get their own loudness/tonal checks.")
    parser.add_argument("--samplerate", type=int, default=48000)
    parser.add_argument("--window-seconds", type=float, default=2.0, help="Analysis window length.")
    parser.add_argument("--tick-seconds", type=float, default=3.0, help="How often to re-analyze + push.")
    parser.add_argument("--push-urls", default="http://localhost:5051/ingest",
                         help="Comma-separated push targets. Pass '' to disable pushing.")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    if not args.channels:
        parser.error("--channels is required (unless using --list-devices). Example: --channels '1:Vocal,2:Guitar,3:Drums'")

    try:
        run(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
