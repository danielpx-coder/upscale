"""Backends de upscale.

Gratuitos/locais : ffmpeg, opencv, torch (PyTorch+spandrel), realesrgan (ncnn-vulkan)
Pagos (API)      : replicate, topaz
"""
from .base import Backend, Capabilities
from .ffmpeg_backend import FFmpegBackend
from .opencv_backend import OpenCVBackend
from .realesrgan_backend import RealESRGANBackend
from .replicate_backend import ReplicateBackend
from .topaz_backend import TopazBackend
from .torch_backend import TorchBackend

BACKENDS: dict[str, type[Backend]] = {b.name: b for b in (
    FFmpegBackend, OpenCVBackend, TorchBackend, RealESRGANBackend, ReplicateBackend, TopazBackend)}


def get(name: str, **opts) -> Backend:
    if name not in BACKENDS:
        raise ValueError(f"Backend desconhecido: {name}. Opções: {', '.join(BACKENDS)}")
    return BACKENDS[name](**opts)


__all__ = ["Backend", "Capabilities", "BACKENDS", "get"]
