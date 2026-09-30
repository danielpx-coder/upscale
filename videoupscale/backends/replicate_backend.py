"""Replicate (API paga) - modelos de upscale de vídeo em GPUs na nuvem.

Requer: pip install replicate  e  REPLICATE_API_TOKEN (https://replicate.com/account/api-tokens)
Modelo padrão: lucataco/real-esrgan-video (entradas: video_path, model, resolution).
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

from .base import Backend, Capabilities

DEFAULT_MODEL = "lucataco/real-esrgan-video"
RESOLUTIONS = ("FHD", "2k", "4k")


def resolution_for(width: int) -> str:
    return "4k" if width >= 3000 else ("2k" if width >= 2000 else "FHD")


class ReplicateBackend(Backend):
    name = "replicate"
    description = "Pago (API). Upscale na nuvem via Replicate (Real-ESRGAN e outros)."
    capabilities = Capabilities(quality=4, speed=3, free=False,
                                models=("RealESRGAN_x4plus", "RealESRGAN_x2plus", "realesr-animevideov3"),
                                default_model="RealESRGAN_x4plus",
                                install="pip install replicate && export REPLICATE_API_TOKEN=...",
                                env=("REPLICATE_API_TOKEN",))

    def available(self):
        if not os.environ.get("REPLICATE_API_TOKEN"):
            return False, "REPLICATE_API_TOKEN não definida"
        try:
            import replicate  # noqa: F401
        except ImportError:
            return False, "pacote replicate não instalado"
        return True, ""

    def upscale(self, src, dst, width, height, progress=None, should_cancel=None):
        self.check()
        import replicate

        model = self.opts.get("model") or DEFAULT_MODEL
        # modelo pode ser "nome:versao", "dono/nome" ou "dono/nome:versao"
        inputs = {
            "video_path": open(src, "rb"),
            "model": self.opts.get("sr_model") or "RealESRGAN_x4plus",
            "resolution": self.opts.get("resolution") or resolution_for(width),
        }
        inputs.update(self.opts.get("extra_inputs") or {})
        print(f"Enviando para Replicate ({model} | {inputs['resolution']})... pode levar alguns minutos.")
        out = replicate.run(model, input=inputs)
        if isinstance(out, list):
            out = out[0]
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        if hasattr(out, "read"):                      # FileOutput (replicate >= 1.0)
            with open(dst, "wb") as f:
                f.write(out.read())
        else:
            urllib.request.urlretrieve(str(out), dst)
