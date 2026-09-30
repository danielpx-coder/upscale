"""Registro de backends. Gratuitos: ffmpeg, opencv, realesrgan. Pagos (API): replicate, topaz."""
from .base import Backend
from .ffmpeg_backend import FFmpegBackend
from .opencv_backend import OpenCVBackend
from .realesrgan_backend import RealESRGANBackend
from .replicate_backend import ReplicateBackend
from .topaz_backend import TopazBackend

BACKENDS: dict[str, type[Backend]] = {b.name: b for b in (
    FFmpegBackend, OpenCVBackend, RealESRGANBackend, ReplicateBackend, TopazBackend)}
