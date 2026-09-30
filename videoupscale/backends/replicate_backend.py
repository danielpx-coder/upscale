"""Replicate (API paga) - executa modelos de upscale de vídeo na nuvem.

Requer: pip install replicate  e  REPLICATE_API_TOKEN (https://replicate.com/account/api-tokens)
Modelo padrão configurável com --model (ex.: "lucataco/real-esrgan-video").
"""
from __future__ import annotations
import os, urllib.request
from .base import Backend

DEFAULT_MODEL = "lucataco/real-esrgan-video"


class ReplicateBackend(Backend):
    name = "replicate"
    free = False
    description = "Pago (API). Real-ESRGAN e outros modelos em GPUs na nuvem via Replicate."

    def upscale(self, src, dst, width, height, progress=None):
        try:
            import replicate
        except ImportError as e:
            raise RuntimeError("pip install replicate") from e
        if not os.environ.get("REPLICATE_API_TOKEN"):
            raise RuntimeError("Defina a variável REPLICATE_API_TOKEN")
        model = self.opts.get("model") or DEFAULT_MODEL
        res = "4k" if width >= 3000 else ("2k" if width >= 2000 else "FHD")
        inputs = {"video_path": open(src, "rb"), "resolution": res}
        inputs.update(self.opts.get("extra_inputs") or {})
        print(f"Enviando para Replicate ({model}, {res})... isso pode levar alguns minutos.")
        out = replicate.run(model, input=inputs)
        if isinstance(out, list):
            out = out[0]
        if hasattr(out, "read"):  # FileOutput (replicate >= 1.0)
            with open(dst, "wb") as f:
                f.write(out.read())
        else:
            urllib.request.urlretrieve(str(out), dst)
