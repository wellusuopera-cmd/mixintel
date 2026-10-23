"""MixIntel — standalone Bark-scale psychoacoustic masking + tonal scoring.

This is a dependency-light VENDORED copy of the pure-math pieces of Master
Ears' bark_masking.py (Johnston 1988 spreading-function model on a Bark
filterbank), deliberately re-implemented here with plain dataclasses
instead of phantom's pydantic models and zero dependency on the `phantom`
package.

Why vendor instead of import: MixIntel is meant to run on a DIFFERENT,
possibly minimal machine than the full Master Ears analysis engine (a
dedicated FOH laptop connected directly to an X32/Dante interface) — see
the vault's Phase 1 architecture decision for why analysis stays off the
"live" machine. Keeping MixIntel's own dependency footprint to just
numpy + scipy (no torch, no phantom, no pydantic) means it can be deployed
to that laptop without dragging in the heavy Demucs/ML stack Master Ears
needs for file-based analysis, which MixIntel never touches.

Math is numerically identical to bark_masking.py — same formulas, same
default thresholds (severity_high=0.6, moderate=0.3, low=0.1,
masking_floor_db=40.0, matching phantom's AnalysisSettings defaults).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import welch

# ---------------------------------------------------------------------------
# Bark-scale filterbank (Traunmuller 1990 formula)
# ---------------------------------------------------------------------------

def _hz_to_bark(hz: np.ndarray) -> np.ndarray:
    bark = 26.81 * hz / (1960.0 + hz) - 0.53
    bark = np.where(bark < 2.0, bark + 0.15 * (2.0 - bark), bark)
    bark = np.where(bark > 20.1, bark + 0.22 * (bark - 20.1), bark)
    return bark


def _bark_to_hz(bark: np.ndarray) -> np.ndarray:
    return 1960.0 * (bark + 0.53) / (26.28 - bark)


N_BARK_BANDS = 24
_BARK_EDGES_HZ = _bark_to_hz(np.arange(0, N_BARK_BANDS + 1, dtype=np.float64))
_BARK_EDGES_HZ[0] = 20.0
_BARK_LABELS = [f"bark_{i}_{int(round((_BARK_EDGES_HZ[i] + _BARK_EDGES_HZ[i+1]) / 2))}hz" for i in range(N_BARK_BANDS)]

_BARK_WEIGHTS = np.ones(N_BARK_BANDS)
_center_hz = (_BARK_EDGES_HZ[:-1] + _BARK_EDGES_HZ[1:]) / 2.0
_BARK_WEIGHTS = np.where((_center_hz >= 200) & (_center_hz <= 600), 1.0, _BARK_WEIGHTS)
_BARK_WEIGHTS = np.where(_center_hz < 100, 0.2, _BARK_WEIGHTS)
_BARK_WEIGHTS = np.where((_center_hz >= 100) & (_center_hz < 200), 0.5, _BARK_WEIGHTS)
_BARK_WEIGHTS = np.where((_center_hz > 600) & (_center_hz <= 1500), 0.8, _BARK_WEIGHTS)
_BARK_WEIGHTS = np.where((_center_hz > 1500) & (_center_hz <= 4000), 0.6, _BARK_WEIGHTS)
_BARK_WEIGHTS = np.where(_center_hz > 4000, 0.3, _BARK_WEIGHTS)


def _spreading_function_db(delta_bark: np.ndarray) -> np.ndarray:
    x = delta_bark + 0.474
    return 15.81 + 7.5 * x - 17.5 * np.sqrt(1.0 + x * x)


def _build_spread_matrix() -> np.ndarray:
    centers = np.arange(N_BARK_BANDS, dtype=np.float64)
    delta = centers[np.newaxis, :] - centers[:, np.newaxis]
    spread_db = _spreading_function_db(delta)
    return 10.0 ** (spread_db / 10.0)


_SPREAD_MATRIX = _build_spread_matrix()

# Severity thresholds — match phantom.AnalysisSettings defaults exactly.
SEVERITY_HIGH = 0.6
SEVERITY_MODERATE = 0.3
SEVERITY_LOW = 0.1
MASKING_FLOOR_DB = 40.0


def classify_severity(score: float) -> str:
    if score >= SEVERITY_HIGH:
        return "high"
    if score >= SEVERITY_MODERATE:
        return "moderate"
    if score >= SEVERITY_LOW:
        return "low"
    return "none"


def bark_band_energy_and_tonality(mono: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Returns (energies, alphas) — one value per Bark band, computed
    directly from a raw mono audio buffer (e.g. a 1-2s window captured live
    off a USB/Dante multichannel interface)."""
    nperseg = min(4096, len(mono)) if len(mono) > 0 else 1
    if nperseg < 2:
        return np.zeros(N_BARK_BANDS), np.zeros(N_BARK_BANDS)

    freqs, psd = welch(mono, fs=sample_rate, nperseg=nperseg)
    energies = np.zeros(N_BARK_BANDS)
    alphas = np.zeros(N_BARK_BANDS)

    for b in range(N_BARK_BANDS):
        lo, hi = _BARK_EDGES_HZ[b], _BARK_EDGES_HZ[b + 1]
        mask = (freqs >= lo) & (freqs < hi)
        bins = psd[mask]
        bins = bins[bins > 0]
        if bins.size == 0:
            continue
        energies[b] = float(np.sum(bins))
        geo_mean = float(np.exp(np.mean(np.log(bins))))
        arith_mean = float(np.mean(bins))
        sfm = geo_mean / arith_mean if arith_mean > 0 else 1.0
        sfm_db = 10.0 * np.log10(max(sfm, 1e-10))
        alphas[b] = min(max(sfm_db / -60.0, 0.0), 1.0)

    return energies, alphas


def _masking_threshold_db(energies: np.ndarray, alphas: np.ndarray) -> np.ndarray:
    spread_energy = energies @ _SPREAD_MATRIX
    spread_db = 10.0 * np.log10(np.maximum(spread_energy, 1e-12))
    bark_centers = np.arange(N_BARK_BANDS, dtype=np.float64)
    offset_db = alphas * (14.5 + bark_centers) + (1.0 - alphas) * 5.5
    return spread_db - offset_db


@dataclass
class MaskingBand:
    band: str
    severity: str
    overlap_score: float


@dataclass
class MaskingResult:
    bands: list = field(default_factory=list)
    overall_severity: str = "none"
    overall_score: float = 0.0


def pairwise_bark_result(
    energies_a: np.ndarray, alphas_a: np.ndarray,
    energies_b: np.ndarray, alphas_b: np.ndarray,
) -> MaskingResult:
    """Same scoring math as bark_masking.py's pairwise_bark_result_from_energies
    — given two channels' Bark-band energy/tonality, returns an asymmetric
    'is either one burying the other' masking score per band + overall."""
    threshold_a = _masking_threshold_db(energies_a, alphas_a)
    threshold_b = _masking_threshold_db(energies_b, alphas_b)

    energy_a_db = 10.0 * np.log10(np.maximum(energies_a, 1e-12))
    energy_b_db = 10.0 * np.log10(np.maximum(energies_b, 1e-12))

    margin_a_masks_b = threshold_a - energy_b_db
    margin_b_masks_a = threshold_b - energy_a_db
    peak_db = max(float(np.max(energy_a_db)), float(np.max(energy_b_db)))
    floor_db = peak_db - MASKING_FLOOR_DB

    def _margin_to_score(margin_db: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-margin_db / 6.0))

    scores = np.maximum(_margin_to_score(margin_a_masks_b), _margin_to_score(margin_b_masks_a))
    below_floor = (energy_a_db < floor_db) & (energy_b_db < floor_db)
    scores = np.where(below_floor, 0.0, scores)

    bands = [
        MaskingBand(band=label, severity=classify_severity(float(score)), overlap_score=round(float(score), 3))
        for label, score in zip(_BARK_LABELS, scores, strict=False)
    ]
    overall_score = float(np.average(scores, weights=_BARK_WEIGHTS))
    overall_severity = classify_severity(overall_score)
    return MaskingResult(bands=bands, overall_severity=overall_severity, overall_score=round(overall_score, 3))


# ---------------------------------------------------------------------------
# Tonal character (mud/harshness/sibilance) — same zones/threshold as the
# file-based path (phantom.problems.detect_problems), derived from the Bark
# energies instead of re-deriving arbitrary Hz windows from raw audio.
# ---------------------------------------------------------------------------

def _band_range_for_hz(lo_hz: float, hi_hz: float) -> tuple[int, int]:
    centers = (_BARK_EDGES_HZ[:-1] + _BARK_EDGES_HZ[1:]) / 2.0
    idx = np.where((centers >= lo_hz) & (centers <= hi_hz))[0]
    if idx.size == 0:
        return (0, 0)
    return (int(idx.min()), int(idx.max()) + 1)


_TONAL_ZONES = {
    "mud": _band_range_for_hz(200, 500),
    "harshness": _band_range_for_hz(2000, 4000),
    "sibilance": _band_range_for_hz(5000, 10000),
}
_TONAL_LABEL = {"mud": "muddy", "harshness": "harsh", "sibilance": "sibilant"}
_EXCESS_THRESHOLD_DB = 6.0
_ALPHA_GATE_MAX = 0.85


def tonal_findings(channel_name: str, energies: np.ndarray, alphas: np.ndarray) -> list[dict]:
    findings = []
    total_energy = float(np.sum(energies))
    if total_energy <= 0:
        return findings
    overall_db = 10.0 * np.log10(total_energy)
    avg_alpha = float(np.average(alphas, weights=np.maximum(energies, 1e-12)))

    if avg_alpha > _ALPHA_GATE_MAX:
        return findings

    for problem_type, (lo_idx, hi_idx) in _TONAL_ZONES.items():
        if lo_idx == hi_idx:
            continue
        band_energy = float(np.sum(energies[lo_idx:hi_idx]))
        if band_energy <= 0:
            continue
        band_db = 10.0 * np.log10(band_energy)
        bw_fraction = (hi_idx - lo_idx) / N_BARK_BANDS
        expected_db = overall_db + 10.0 * np.log10(max(bw_fraction, 1e-9))
        excess_db = band_db - expected_db
        if excess_db >= _EXCESS_THRESHOLD_DB:
            findings.append({
                "type": problem_type,
                "label": _TONAL_LABEL[problem_type],
                "excess_db": round(excess_db, 1),
                "message": f"{channel_name}: {_TONAL_LABEL[problem_type]} -- {excess_db:.1f}dB above expected in this range",
            })
    return findings
