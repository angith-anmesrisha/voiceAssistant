"""JARVIS's own voice: the Kokoro-82M speech model (Apache 2.0) running on this PC.

Free, offline and unlimited once the model (about 350 MB) has been downloaded into the
data folder; nothing is sent anywhere to speak. Text becomes British English phonemes
with espeak-ng, then 24 kHz audio with onnxruntime, one sentence at a time so speech can
start while the rest is still being made. British male voices: bm_george (default; the
closest to a composed, mature English butler), bm_fable, bm_lewis, bm_daniel.

The model files come from the kokoro-onnx project's GitHub release and are checked
against the SHA-256 sums below before use.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

SAMPLE_RATE = 24_000
BRITISH_VOICES = ("bm_george", "bm_fable", "bm_lewis", "bm_daniel")
DEFAULT_VOICE = "bm_george"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
_MAX_PHONEMES = 510
_SILENCE = 0.01  # amplitude below which the model's lead-in/out counts as silence


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    sha256: str
    size: int


MODEL_FILES = (
    ModelFile(
        "kokoro-v1.0.onnx", "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
        325_505_369,
    ),
    ModelFile(
        "voices-v1.0.bin", "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        28_214_398,
    ),
)  # fmt: skip


class VoiceModelError(RuntimeError):
    pass


def missing_files(folder: Path) -> list[ModelFile]:
    return [f for f in MODEL_FILES if not _present(folder / f.name, f)]


def _present(path: Path, file: ModelFile) -> bool:
    try:
        return path.stat().st_size == file.size
    except OSError:
        return False


def download(
    folder: Path,
    client: httpx.Client,
    progress: Callable[[int, int], None] | None = None,
    base_url: str = MODEL_URL,
) -> None:
    """Fetch any missing model file, verify its checksum, then move it into place."""
    folder.mkdir(parents=True, exist_ok=True)
    todo = missing_files(folder)
    total = sum(f.size for f in todo)
    done = 0
    for file in todo:
        part = folder / (file.name + ".part")
        digest = hashlib.sha256()
        with client.stream("GET", base_url + file.name, follow_redirects=True) as response:
            response.raise_for_status()
            with part.open("wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if digest.hexdigest() != file.sha256:
            part.unlink(missing_ok=True)
            raise VoiceModelError(f"{file.name} didn't match its checksum; it was discarded")
        part.replace(folder / file.name)


_SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=\S)")
_CLAUSE = re.compile(r"(?<=[,;:])\s+")


def sentences(text: str, longest: int = 220, first: int = 70) -> list[str]:
    """Split into sentences, and overly long ones at commas, so speech starts sooner.

    The first piece is kept short (cut at its first comma past a few words, if it's
    longer than ``first``), because speech can't begin until it has been made."""
    pieces: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        if len(sentence) <= longest:
            pieces.append(sentence)
            continue
        current = ""
        for clause in _CLAUSE.split(sentence):
            if current and len(current) + len(clause) > longest:
                pieces.append(current)
                current = clause
            else:
                current = f"{current} {clause}".strip()
        if current:
            pieces.append(current)
    pieces = [p for p in pieces if p.strip()]
    if pieces and len(pieces[0]) > first:
        cut = _CLAUSE.search(pieces[0], 20)
        if cut is not None and cut.start() < len(pieces[0]) - 10:
            pieces[0:1] = [pieces[0][: cut.start()], pieces[0][cut.end() :]]
    return pieces


def _split_phonemes(phonemes: str, limit: int = _MAX_PHONEMES) -> list[str]:
    parts: list[str] = []
    while len(phonemes) > limit:
        cut = phonemes.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(phonemes[:cut])
        phonemes = phonemes[cut:].lstrip()
    return [*parts, phonemes] if phonemes else parts


def blend(voice: str) -> list[tuple[str, float]]:
    """A voice, or a mix of voices: "bm_george", "bm_george+bm_fable" (half each) or
    "bm_george*6+am_michael*4" (weights). [] if it can't be read."""
    mix = []
    for part in voice.replace(" ", "").split("+"):
        name, _, weight = part.partition("*")
        try:
            amount = float(weight) if weight else 1.0
        except ValueError:
            return []
        if not name or amount <= 0:
            return []
        mix.append((name, amount))
    return mix


class KokoroVoice:
    """Loaded model plus one voice. ``synthesize`` is safe to call from one thread at a time."""

    def __init__(self, folder: Path, voice: str = DEFAULT_VOICE, speed: float = 1.15) -> None:
        import espeakng_loader
        import numpy as np
        import onnxruntime as rt
        from phonemizer.backend.espeak.wrapper import EspeakWrapper

        EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
        EspeakWrapper.set_library(espeakng_loader.get_library_path())
        options = rt.SessionOptions()
        options.log_severity_level = 3  # errors only
        self._session = rt.InferenceSession(
            str(folder / MODEL_FILES[0].name), options, providers=["CPUExecutionProvider"]
        )
        config = self._session.get_modelmeta().custom_metadata_map.get("kokoro_config", "")
        try:
            self._vocab: dict[str, int] = json.loads(config)["vocab"]
        except (ValueError, KeyError) as exc:
            raise VoiceModelError("the voice model has no phoneme table") from exc
        voices = np.load(folder / MODEL_FILES[1].name)
        mix = blend(voice)
        if not mix or any(name not in voices for name, _ in mix):
            logger.warning("Voice {} isn't in the model; using {}", voice, DEFAULT_VOICE)
            voice, mix = DEFAULT_VOICE, [(DEFAULT_VOICE, 1.0)]
        self.voice = voice
        total = sum(weight for _, weight in mix)
        self._style = np.sum(
            [np.asarray(voices[name], dtype=np.float32) * (weight / total) for name, weight in mix],
            axis=0,
            dtype=np.float32,
        )
        self.speed = max(0.5, min(2.0, speed))
        self._lock = threading.Lock()
        self.synthesize("Ready.")  # the first run is slow (allocations); do it now

    def _phonemes(self, text: str) -> str:
        import phonemizer

        raw = phonemizer.phonemize(
            text, "en-gb", backend="espeak", preserve_punctuation=True, with_stress=True
        )
        return " ".join("".join(ch for ch in str(raw) if ch in self._vocab).split())

    def synthesize(self, text: str) -> Any:
        """Float32 mono audio at :data:`SAMPLE_RATE`, with the silence at both ends trimmed."""
        import numpy as np

        with self._lock:
            phonemes = self._phonemes(text)
            chunks = []
            for part in _split_phonemes(phonemes):
                ids = [self._vocab[ch] for ch in part]
                if not ids:
                    continue
                style = self._style[min(len(ids), len(self._style)) - 1]
                audio = self._session.run(
                    None,
                    {
                        "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
                        "style": style,
                        "speed": np.array([self.speed], dtype=np.float32),
                    },
                )[0]
                chunks.append(trim(np.asarray(audio, dtype=np.float32).ravel()))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def trim(audio: Any, margin: int = SAMPLE_RATE // 50) -> Any:
    """Cut the quiet lead-in and tail the model adds, keeping a short margin."""
    import numpy as np

    loud = np.flatnonzero(np.abs(audio) > _SILENCE)
    if not len(loud):
        return audio[:0]
    return audio[max(0, int(loud[0]) - margin) : int(loud[-1]) + 1 + margin]
