"""Upscale local de alta qualidade com PyTorch + spandrel (Real-ESRGAN e afins).

Roda qualquer modelo de super-resolução compatível com spandrel (.pth/.safetensors):
Real-ESRGAN x4plus/x2plus/x4plus-anime, 4x-UltraSharp, 4x-Nomos, RealESRGANv2 etc.

Requer (opcional): pip install torch torchvision spandrel
  GPU NVIDIA: use a roda com CUDA em https://pytorch.org/get-started/locally/
Uso: --model <nome|url|caminho>  (padrão: realesrgan-x4plus)
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import numpy as np

from .. import pipeline
from .. import video_io as vio
from .base import Backend, Capabilities

CACHE = Path(os.environ.get("VIDEOUPSCALE_MODELS", Path.home() / ".cache" / "videoupscale"))
MODELS = {
    "realesrgan-x4plus": ("https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth", 4),
    "realesrgan-x2plus": ("https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth", 2),
    "realesrgan-x4plus-anime": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth", 4),
    "realesrnet-x4plus": ("https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.1/RealESRNet_x4plus.pth", 4),
}


def resolve_model(model: str) -> Path:
    """Aceita caminho local, URL ou nome conhecido."""
    p = Path(model).expanduser()
    if p.is_file():
        return p
    if model.startswith(("http://", "https://")):
        return _download(model, CACHE / os.path.basename(model.split("?")[0]))
    if model in MODELS:
        url, _ = MODELS[model]
        return _download(url, CACHE / f"{model}.pth")
    if p.is_file() is False and p.suffix in (".pth", ".safetensors", ".pt"):
        raise FileNotFoundError(f"Modelo não encontrado: {model}")
    raise ValueError(f"Modelo desconhecido: {model}. Nomes aceitos: {', '.join(MODELS)} (ou URL/caminho)")


def _download(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"Baixando {dest.name}...")
    tmp = dest.with_suffix(".part")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def pick_device(requested: str = "auto"):
    import torch

    if requested != "auto":
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


class _TorchUpscaler:
    """Carrega o modelo por processo (cada worker tem o seu) e faz upscale com tiling."""

    def __init__(self, model: str, device: str = "auto", fp16: bool = True, tile: int = 0, tile_pad: int = 16):
        import torch

        self.torch = torch
        self.device = pick_device(device)
        self.fp16 = fp16 and self.device.type == "cuda"
        self.tile = max(0, tile)
        self.tile_pad = tile_pad
        path = resolve_model(model)
        try:
            from spandrel import ModelLoader
        except ImportError as err:
            raise RuntimeError("Instale o spandrel para carregar modelos: pip install spandrel") from err
        self.net = ModelLoader().load_from_file(str(path))
        self.scale = int(getattr(self.net, "scale", 0) or 0)
        self.net.eval().to(self.device)
        if self.scale not in (2, 3, 4, 8):
            self.scale = _guess_scale(self.net) or 4

    @property
    def net_call(self):
        return self.net if callable(self.net) else self.net.model

    def __call__(self, bgr):
        import cv2
        import numpy as np

        tensor = self._to_tensor(bgr)
        out = self._forward(tensor)
        img = (out.squeeze(0).permute(1, 2, 0).clamp(0, 1).mul(255).round().byte().cpu().numpy())
        return cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR)

    def _to_tensor(self, bgr):
        import cv2
        import torch

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        t = torch.from_numpy(np.ascontiguousarray(rgb)).permute(2, 0, 1).unsqueeze(0).float().div(255)
        return t.to(self.device)

    def _forward(self, tensor):
        import torch

        net = self.net_call
        if not self.tile or min(tensor.shape[-2:]) <= self.tile:
            with torch.no_grad(), torch.autocast(device_type=self.device.type, enabled=self.fp16):
                return net(tensor)
        # tiling: evita estouro de VRAM em 4K e acelera em GPUs pequenas
        b, c, h, w = tensor.shape
        s = self.scale
        out = torch.zeros((b, c, h * s, w * s), device=tensor.device, dtype=tensor.dtype)
        weights = torch.zeros_like(out)
        tile, pad = self.tile, self.tile_pad
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                y0, x0 = max(0, y - pad), max(0, x - pad)
                y1, x1 = min(h, y + tile + pad), min(w, x + tile + pad)
                with torch.no_grad(), torch.autocast(device_type=self.device.type, enabled=self.fp16):
                    chunk = net(tensor[:, :, y0:y1, x0:x1])
                cy, cx = (y - y0) * s, (x - x0) * s          # recorte do padding
                ty = min(tile * s, out.shape[2] - y * s)     # tamanho do tile sem padding
                tx = min(tile * s, out.shape[3] - x * s)
                out[:, :, y * s:y * s + ty, x * s:x * s + tx] += chunk[:, :, cy:cy + ty, cx:cx + tx]
                weights[:, :, y * s:y * s + ty, x * s:x * s + tx] += 1
        return out / weights.clamp(min=1)


class _TorchFactory:
    """Picklable: cada processo de trabalho carrega o modelo no seu próprio dispositivo."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __call__(self):
        return _TorchUpscaler(**self.kwargs)


def _guess_scale(net) -> int:
    for attr in ("scale", "upscale", "upscale_factor"):
        v = getattr(net, attr, None)
        if isinstance(v, int):
            return v
    return 0


class TorchBackend(Backend):
    name = "torch"
    description = "Gratuito. Real-ESRGAN/ESRGAN local via PyTorch (melhor qualidade local; usa GPU)."
    capabilities = Capabilities(quality=5, speed=2, models=tuple(MODELS), default_model="realesrgan-x4plus",
                                install="pip install torch spandrel (com CUDA para melhor desempenho)")

    def available(self):
        try:
            import spandrel  # noqa: F401
            import torch  # noqa: F401
        except ImportError as e:
            return False, f"dependência ausente ({e.name})"
        return True, ""

    def upscale(self, src, dst, width, height, progress=None, should_cancel=None):
        self.check()
        info = vio.probe(src)
        model = self.opts.get("model") or "realesrgan-x4plus"
        tile = int(self.opts.get("tile", 0) or 0)
        if not tile and (width * height) > 1920 * 1080 * 4:
            tile = 512  # padrão seguro a partir de 4K
        kwargs = dict(model=model, device=self.opts.get("device", "auto"),
                      fp16=bool(self.opts.get("fp16", True)), tile=tile)
        resolve_model(model)

        factory = _TorchFactory(**kwargs)
        opts = pipeline.PipelineOptions(
            width=width, height=height,
            workers=self.opts.get("workers", 1 if str(kwargs["device"]).startswith("cuda") else 0),
            frame_ext=self.opts.get("frame_ext", "png"), keep_frames=self.opts.get("keep_frames", False),
            tmp_dir=self.opts.get("tmp_dir"), start=self.opts.get("start"), end=self.opts.get("end"),
            codec=self.opts.get("codec", "libx264"), crf=self.opts.get("crf", 18),
            preset=self.opts.get("preset", "medium"),
            pix_fmt=vio.output_pix_fmt(self.opts.get("pix_fmt", "auto"), info, self.opts.get("codec", "libx264")),
            bitrate=self.opts.get("bitrate"), extra_filters=self.opts.get("extra_filters", ""),
        )
        pipeline.run(src, dst, info, opts, factory, progress, should_cancel)
