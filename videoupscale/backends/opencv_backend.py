"""Super-resolução com IA via OpenCV DNN (ESPCN/FSRCNN/EDSR/LapSRN) - gratuito e leve.

Os modelos são baixados automaticamente em ~/.cache/videoupscale
(altere com a variável VIDEOUPSCALE_MODELS).
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

from .. import pipeline
from .. import video_io as vio
from .base import Backend, Capabilities

MODEL_URLS = {
    "edsr": "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x{s}.pb",
    "espcn": "https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x{s}.pb",
    "fsrcnn": "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x{s}.pb",
    "lapsrn": "https://github.com/fannymonori/TF-LapSRN/raw/master/export/LapSRN_x{s}.pb",
}
FILE_NAMES = {"edsr": "EDSR", "espcn": "ESPCN", "fsrcnn": "FSRCNN", "lapsrn": "LapSRN"}
SCALES = {"edsr": (2, 3, 4), "espcn": (2, 3, 4), "fsrcnn": (2, 3, 4), "lapsrn": (2, 4, 8)}
CACHE = Path(os.environ.get("VIDEOUPSCALE_MODELS", Path.home() / ".cache" / "videoupscale"))


def model_path(model: str, scale: int, quiet: bool = False) -> Path:
    if model not in MODEL_URLS:
        raise ValueError(f"Modelo desconhecido: {model}. Opções: {', '.join(MODEL_URLS)}")
    p = CACHE / f"{FILE_NAMES[model]}_x{scale}.pb"
    if not p.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        if not quiet:
            print(f"Baixando modelo {p.name} (~{_size_hint(model, scale)})...")
        tmp = p.with_suffix(".part")
        try:
            urllib.request.urlretrieve(MODEL_URLS[model].format(s=scale), tmp)
            tmp.replace(p)
        finally:
            tmp.unlink(missing_ok=True)
    return p


def _size_hint(model: str, scale: int) -> str:
    mb = {"edsr": {2: 38, 3: 38, 4: 38}, "espcn": {2: 0.1, 3: 0.1, 4: 0.1},
          "fsrcnn": {2: 0.05, 3: 0.05, 4: 0.05}, "lapsrn": {2: 1, 4: 1, 8: 1}}
    return f"{mb.get(model, {}).get(scale, 1):.1f} MB"


class _SuperRes:
    """Chamável criado dentro de cada processo de trabalho."""

    def __init__(self, model: str, scale: int, gpu: bool, gpu_id: int = 0):
        import cv2

        try:
            from cv2 import dnn_superres
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Instale opencv-contrib-python: pip install opencv-contrib-python") from e
        self.cv2 = cv2
        sr = dnn_superres.DnnSuperResImpl_create()
        sr.readModel(str(model_path(model, scale, quiet=True)))
        sr.setModel(model, scale)
        if gpu:
            sr.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
            sr.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
        self.sr = sr

    def __call__(self, img):
        return self.sr.upsample(img)


class _SuperResFactory:
    """Picklable: o modelo é carregado dentro de cada processo de trabalho."""

    def __init__(self, model: str, scale: int, gpu: bool, gpu_id: int = 0):
        self.model, self.scale, self.gpu, self.gpu_id = model, scale, gpu, gpu_id

    def __call__(self):
        return _SuperRes(self.model, self.scale, self.gpu, self.gpu_id)


def pick_scale(model: str, need: float) -> int:
    allowed = SCALES.get(model, (2, 3, 4))
    return next((s for s in allowed if s >= need), allowed[-1])


class OpenCVBackend(Backend):
    name = "opencv"
    description = "Gratuito. IA leve via OpenCV DNN (FSRCNN/ESPCN rápidos; EDSR melhor e mais lento)."
    capabilities = Capabilities(quality=3, speed=3, models=tuple(MODEL_URLS), default_model="fsrcnn",
                                install="pip install opencv-contrib-python")

    def available(self):
        try:
            from cv2 import dnn_superres  # noqa: F401
        except ImportError:
            return False, "opencv-contrib-python não instalado"
        return True, ""

    def upscale(self, src, dst, width, height, progress=None, should_cancel=None):
        self.check()
        info = vio.probe(src)
        model = (self.opts.get("model") or "fsrcnn").lower()
        need = max(width / info.width, height / info.height)
        scale = pick_scale(model, need)
        model_path(model, scale)  # garante o download antes de abrir os processos
        gpu = bool(self.opts.get("gpu"))

        factory = _SuperResFactory(model, scale, gpu, self.opts.get("gpu_id", 0))
        opts = pipeline.PipelineOptions(
            width=width, height=height, workers=self.opts.get("workers", 0),
            frame_ext=self.opts.get("frame_ext", "png"), keep_frames=self.opts.get("keep_frames", False),
            tmp_dir=self.opts.get("tmp_dir"), start=self.opts.get("start"), end=self.opts.get("end"),
            codec=self.opts.get("codec", "libx264"), crf=self.opts.get("crf", 18),
            preset=self.opts.get("preset", "medium"),
            pix_fmt=vio.output_pix_fmt(self.opts.get("pix_fmt", "auto"), info, self.opts.get("codec", "libx264")),
            bitrate=self.opts.get("bitrate"), extra_filters=self.opts.get("extra_filters", ""),
        )
        pipeline.run(src, dst, info, opts, factory, progress, should_cancel)
