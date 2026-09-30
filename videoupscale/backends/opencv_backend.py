"""Super-resolução com IA via OpenCV DNN (modelos EDSR/ESPCN/FSRCNN/LapSRN) - gratuito."""
from __future__ import annotations
import os, urllib.request
from pathlib import Path
from .base import Backend
from ..video_io import FrameWriter, probe, read_frames

MODEL_URLS = {
    "edsr": "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x{s}.pb",
    "espcn": "https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x{s}.pb",
    "fsrcnn": "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x{s}.pb",
    "lapsrn": "https://github.com/fannymonori/TF-LapSRN/raw/master/export/LapSRN_x{s}.pb",
}
CACHE = Path(os.environ.get("VIDEOUPSCALE_MODELS", Path.home() / ".cache" / "videoupscale"))


def model_path(model: str, scale: int) -> Path:
    name = {"edsr": "EDSR", "espcn": "ESPCN", "fsrcnn": "FSRCNN", "lapsrn": "LapSRN"}[model]
    p = CACHE / f"{name}_x{scale}.pb"
    if not p.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"Baixando modelo {p.name}...")
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URLS[model].format(s=scale), tmp)
        tmp.replace(p)
    return p


class OpenCVBackend(Backend):
    name = "opencv"
    description = "Gratuito, IA leve (ESPCN/FSRCNN rápidos; EDSR melhor e lento). CUDA opcional."

    def upscale(self, src, dst, width, height, progress=None):
        import cv2
        try:
            from cv2 import dnn_superres
        except ImportError as e:
            raise RuntimeError("Instale opencv-contrib-python: pip install opencv-contrib-python") from e
        info = probe(src)
        model = self.opts.get("model") or "fsrcnn"
        need = max(width / info.width, height / info.height)
        allowed = [2, 4, 8] if model == "lapsrn" else [2, 3, 4]
        s = next((x for x in allowed if x >= need), allowed[-1])
        sr = dnn_superres.DnnSuperResImpl_create()
        sr.readModel(str(model_path(model, s)))
        sr.setModel(model, s)
        if self.opts.get("gpu"):
            sr.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
            sr.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
        w = FrameWriter(dst, src, width, height, info.fps, info.has_audio,
                        self.opts.get("codec", "libx264"), self.opts.get("crf", 18), self.opts.get("preset", "medium"))
        try:
            for i, frame in enumerate(read_frames(src, info), 1):
                w.write(sr.upsample(frame))
                if progress:
                    progress(i, info.frames)
        finally:
            w.close()
