"""
Das Skript vorlesen lassen und als MP3 speichern.

Welche Stimme vorliest, steht in config.yaml unter "voice". Die Stimme ist austauschbar:
Jede "Engine" ist eine Funktion, die Text in Audio verwandelt. Zum Start gibt es nur Kokoro.
Eine weitere Engine (z. B. OpenAI) kommt später als zusätzliche Funktion dazu, der Rest
des Programms bleibt gleich.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "models"

# Kokoro-Modelldateien (Version 1.0), veröffentlicht auf GitHub vom Projekt kokoro-onnx
KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


@dataclass
class AudioResult:
    seconds: float       # Länge der Folge
    render_seconds: float  # So lange hat das Vorlesen gedauert
    size_bytes: int


def split_paragraphs(script: str) -> list[str]:
    return [p.strip() for p in script.split("\n") if p.strip()]


# --- Engine: Kokoro --------------------------------------------------------------

def download_kokoro() -> None:
    """Lädt die Modelldateien herunter, falls sie noch nicht da sind (etwa 350 MB)."""
    MODEL_DIR.mkdir(exist_ok=True)
    for name, url in KOKORO_FILES.items():
        target = MODEL_DIR / name
        if not target.exists():
            print(f"  Lade {name} herunter ...", flush=True)
            urllib.request.urlretrieve(url, target)


def render_kokoro(paragraphs: list[str], settings: dict) -> tuple[np.ndarray, int]:
    from kokoro_onnx import Kokoro

    download_kokoro()
    kokoro = Kokoro(str(MODEL_DIR / "kokoro-v1.0.onnx"), str(MODEL_DIR / "voices-v1.0.bin"))
    pause = settings.get("pause_between_paragraphs", 0.6)

    pieces: list[np.ndarray] = []
    sample_rate = 24_000
    for number, paragraph in enumerate(paragraphs, 1):
        audio, sample_rate = kokoro.create(
            paragraph,
            voice=settings["name"],
            speed=settings.get("speed", 1.0),
            lang=settings.get("language", "en-us"),
        )
        pieces.append(audio)
        pieces.append(np.zeros(int(sample_rate * pause), dtype=audio.dtype))  # kurze Pause
        print(f"  Absatz {number}/{len(paragraphs)} vorgelesen", flush=True)
    return np.concatenate(pieces), sample_rate


# Hier werden alle verfügbaren Engines eingetragen. Später z. B.: "openai": render_openai
ENGINES = {
    "kokoro": render_kokoro,
}


# --- Gemeinsamer Teil ------------------------------------------------------------

def synthesize(script: str, target: Path, settings: dict, title: str = "") -> AudioResult:
    engine = settings.get("engine", "kokoro")
    if engine not in ENGINES:
        raise ValueError(f"Unbekannte Stimmen-Engine '{engine}'. Verfügbar: {', '.join(ENGINES)}")

    started = time.time()
    audio, sample_rate = ENGINES[engine](split_paragraphs(script), settings)
    render_seconds = time.time() - started

    # Erst als WAV zwischenspeichern, dann mit ffmpeg in eine kompakte MP3 umwandeln.
    # Mono mit 64 kbit/s reicht für Sprache völlig: etwa 0,5 MB pro Minute.
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "folge.wav"
        sf.write(wav, audio, sample_rate)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-b:a", "64k",
             "-metadata", f"title={title}", str(target)],
            check=True,
        )

    return AudioResult(
        seconds=len(audio) / sample_rate,
        render_seconds=render_seconds,
        size_bytes=target.stat().st_size,
    )
