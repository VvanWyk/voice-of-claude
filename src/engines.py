"""Pluggable local TTS engines.

Both engines expose the same interface:

    engine.stream(text) -> iterator of (samples, sample_rate)

where `samples` is a 1-D numpy array (int16 for Piper, float32 for Kokoro -
sounddevice plays either). Streaming per-chunk keeps first-audio latency low and
lets playback be interrupted mid-reply.

Measured on an i7-1265U (CPU-only):
  - Piper  (en_US-lessac-medium): ~0.5x real-time  -> low latency  (DEFAULT)
  - Kokoro (int8, tuned threads): ~3.7x real-time   -> natural but laggy
"""
from __future__ import annotations

import re

import numpy as np

import config

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Preferred GPU ONNX providers, best first. CPU is always appended as fallback.
_GPU_PROVIDERS = ["CUDAExecutionProvider", "DmlExecutionProvider"]


def select_providers():
    """Ordered ONNX providers honoring TTS_DEVICE, with CPU as the fallback.

    Only providers the installed onnxruntime actually exposes are used, so the
    default CPU-only build transparently stays on CPU.
    """
    import onnxruntime as rt

    available = rt.get_available_providers()
    want = config.DEVICE
    gpu = []
    if want in ("auto", "gpu"):
        gpu = [p for p in _GPU_PROVIDERS if p in available]
    elif want == "cuda":
        gpu = [p for p in ("CUDAExecutionProvider",) if p in available]
    elif want in ("dml", "directml"):
        gpu = [p for p in ("DmlExecutionProvider",) if p in available]
    # "cpu" (or no GPU provider available) -> CPU only
    return gpu + ["CPUExecutionProvider"]


def cuda_available() -> bool:
    """True if the CUDA provider is installed and GPU use is permitted."""
    if config.DEVICE == "cpu":
        return False
    try:
        import onnxruntime as rt

        return "CUDAExecutionProvider" in rt.get_available_providers()
    except Exception:
        return False


def chunk_sentences(text: str, target: int = 220):
    """Group sentences into ~`target`-char chunks for responsive streaming."""
    buf = ""
    for s in _SENTENCE_SPLIT_RE.split(text.strip()):
        if not s:
            continue
        if not buf:
            buf = s
        elif len(buf) + 1 + len(s) <= target:
            buf += " " + s
        else:
            yield buf
            buf = s
    if buf:
        yield buf


class PiperEngine:
    name = "piper"

    def __init__(self) -> None:
        from piper import PiperVoice

        model = config.piper_model_path()
        cfg = model.with_suffix(model.suffix + ".json")
        if not model.exists():
            raise FileNotFoundError(
                f"Piper voice missing: {model}\nRun setup.ps1 to download it."
            )
        # Piper's only GPU path is CUDA (NVIDIA); it has no DirectML option.
        use_cuda = cuda_available()
        self.device = "cuda" if use_cuda else "cpu"
        self.voice = PiperVoice.load(str(model), config_path=str(cfg), use_cuda=use_cuda)

        syn_kwargs = {}
        if config.SPEED and config.SPEED != 1.0:
            # length_scale > 1 is slower; invert so SPEED follows the usual sense.
            syn_kwargs["length_scale"] = 1.0 / config.SPEED
        if config.PIPER_SPEAKER is not None:
            # Multi-speaker voices (e.g. libritts_r) pick a voice by index.
            syn_kwargs["speaker_id"] = config.PIPER_SPEAKER
        self._syn_config = None
        if syn_kwargs:
            from piper import SynthesisConfig

            self._syn_config = SynthesisConfig(**syn_kwargs)

    def stream(self, text: str):
        for chunk in self.voice.synthesize(text, syn_config=self._syn_config):
            yield np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16), chunk.sample_rate


class KokoroEngine:
    name = "kokoro"

    def __init__(self) -> None:
        import onnxruntime as rt
        from kokoro_onnx import Kokoro

        if not config.MODEL_PATH.exists() or not config.VOICES_PATH.exists():
            raise FileNotFoundError(
                f"Kokoro model missing:\n  {config.MODEL_PATH}\n  {config.VOICES_PATH}\n"
                "Run setup.ps1 to download it."
            )
        providers = select_providers()
        so = rt.SessionOptions()
        if providers[0] == "CPUExecutionProvider":
            # Tuning matters a lot on this hybrid P/E-core CPU: letting
            # onnxruntime spread across the weak efficiency cores roughly
            # doubled latency. (Irrelevant once a GPU provider is leading.)
            so.intra_op_num_threads = config.KOKORO_THREADS
            so.inter_op_num_threads = 1
        so.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = rt.InferenceSession(
            str(config.MODEL_PATH), sess_options=so, providers=providers
        )
        # What ORT actually bound to (it falls back silently if a GPU op is
        # unsupported), e.g. "CUDAExecutionProvider" -> "cuda".
        active = sess.get_providers()[0] if sess.get_providers() else "CPUExecutionProvider"
        self.device = active.replace("ExecutionProvider", "").lower() or "cpu"
        self.k = Kokoro.from_session(sess, str(config.VOICES_PATH))

    def stream(self, text: str):
        for piece in chunk_sentences(text):
            samples, sr = self.k.create(
                piece, voice=config.VOICE, speed=config.SPEED, lang=config.LANG
            )
            yield samples, sr


def load_engine():
    """Instantiate the engine selected by TTS_ENGINE (falls back to the other)."""
    choice = config.ENGINE.lower()
    primary, fallback = (PiperEngine, KokoroEngine)
    if choice == "kokoro":
        primary, fallback = KokoroEngine, PiperEngine
    try:
        return primary()
    except Exception:
        return fallback()
