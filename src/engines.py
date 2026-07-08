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
import normalizer

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Preferred GPU ONNX providers, best first. CPU is always appended as fallback.
_GPU_PROVIDERS = ["CUDAExecutionProvider", "DmlExecutionProvider"]

_dlls_preloaded = False


def _preload_gpu_dlls() -> None:
    """Load CUDA/cuDNN DLLs shipped as nvidia-* pip wheels (onnxruntime >= 1.21).

    Without this, CUDA only works if the CUDA toolkit is on the system PATH;
    with it, `pip install onnxruntime-gpu[cuda,cudnn]` is all that's needed.
    """
    global _dlls_preloaded
    if _dlls_preloaded:
        return
    _dlls_preloaded = True
    try:
        import onnxruntime as rt

        rt.preload_dlls()
    except Exception:
        pass  # older onnxruntime or CPU-only build - nothing to preload


def select_providers():
    """Ordered ONNX providers honoring TTS_DEVICE, with CPU as the fallback.

    Only providers the installed onnxruntime actually exposes are used, so the
    default CPU-only build transparently stays on CPU.
    """
    import onnxruntime as rt

    _preload_gpu_dlls()
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

        _preload_gpu_dlls()
        return "CUDAExecutionProvider" in rt.get_available_providers()
    except Exception:
        return False


# Extra silence rendered for a PAUSE_TOKEN (after headings).
_PAUSE_TOKEN_S = 0.45


def chunk_sentences(text: str, target: int = 220):
    """Yield (chunk, pause_after) grouping sentences into ~`target` chars.

    config.PAUSE_TOKEN is a hard chunk boundary: the chunk before it is
    yielded with pause_after=True so the engine can add extra silence
    (structure-aware prosody - text_filter puts tokens after headings).
    """
    segments = text.strip().split(config.PAUSE_TOKEN)
    for seg_idx, segment in enumerate(segments):
        last_of_segment = seg_idx < len(segments) - 1  # a token followed it
        buf = ""
        for s in _SENTENCE_SPLIT_RE.split(segment.strip()):
            if not s:
                continue
            if not buf:
                buf = s
            elif len(buf) + 1 + len(s) <= target:
                buf += " " + s
            else:
                yield buf, False
                buf = s
        if buf:
            yield buf, last_of_segment


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
        # Piper has no per-chunk pause control; just don't feed it the token.
        text = text.replace(config.PAUSE_TOKEN, " ")
        first = True
        for chunk in self.voice.synthesize(text, syn_config=self._syn_config):
            # Piper doesn't expose per-sentence chunks; label first chunk with
            # the full text so the overlay can show it, rest with "".
            piece = text if first else ""
            first = False
            yield np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16), chunk.sample_rate, piece


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
        # Pre-compile CUDA kernels so the first real synthesis has no JIT delay.
        try:
            next(iter(self.stream("warm")), None)
        except Exception:
            pass

    def stream(self, text: str):
        for piece, pause_after in chunk_sentences(text, target=100):
            # Normalize for natural speech (decimals, ordinals, abbreviations)
            # but keep the original piece so the overlay can highlight it.
            spoken = normalizer.normalize(piece)
            # trim=False preserves the natural audio tail; the default trim=True
            # was clipping the last phoneme of each sentence.
            samples, sr = self.k.create(
                spoken, voice=config.VOICE, speed=config.SPEED, lang=config.LANG,
                trim=False,
            )
            # Inter-sentence gap; much longer after a heading (pause token).
            pad_s = 0.06 + (_PAUSE_TOKEN_S if pause_after else 0.0)
            pad = np.zeros(int(sr * pad_s), dtype=samples.dtype)
            yield np.concatenate([samples, pad]), sr, piece


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
