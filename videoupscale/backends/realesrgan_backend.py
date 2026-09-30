"""Real-ESRGAN (ncnn-vulkan) - gratuito, excelente custo/benefício, roda em GPU AMD/NVIDIA/Intel.

Baixe o executável em https://github.com/xinntao/Real-ESRGAN/releases
(realesrgan-ncnn-vulkan-*-{windows,ubuntu,macos}.zip), coloque no PATH ou use --realesrgan-bin.
Modelos: realesr-animevideov3 (x2/x3/x4, ideal para anime/animação),
         realesrgan-x4plus, realesrgan-x4plus-anime, realesrnet-x4plus.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .. import pipeline
from .. import video_io as vio
from .base import Backend, Capabilities, which

VIDEO_MODELS = ("realesr-animevideov3", "realesr-animevideov3-x2", "realesr-animevideov3-x3",
                "realesr-animevideov3-x4", "realesrgan-x4plus", "realesrgan-x4plus-anime", "realesrnet-x4plus")


class RealESRGANBackend(Backend):
    name = "realesrgan"
    description = "Gratuito. Real-ESRGAN ncnn-vulkan em GPU (ótimo para anime e vídeos reais)."
    capabilities = Capabilities(quality=4, speed=3, needs_gpu=False, models=VIDEO_MODELS,
                                default_model="realesr-animevideov3",
                                install="Baixe realesrgan-ncnn-vulkan: https://github.com/xinntao/Real-ESRGAN/releases")

    def available(self):
        exe = self.opts.get("realesrgan_bin") or which("realesrgan-ncnn-vulkan")
        return (True, "") if exe else (False, "realesrgan-ncnn-vulkan não está no PATH")

    def upscale(self, src, dst, width, height, progress=None, should_cancel=None):
        self.check()
        info = vio.probe(src)
        exe = self.opts.get("realesrgan_bin") or which("realesrgan-ncnn-vulkan")
        model = self.opts.get("model") or "realesr-animevideov3"
        need = max(width / info.width, height / info.height)
        if "-x2" in model:
            scale = 2
        elif "-x3" in model:
            scale = 3
        elif "x4plus" in model:
            scale = 4
        else:
            scale = next((s for s in (2, 3, 4) if s >= need), 4)
        tmp_dir = self.opts.get("tmp_dir")
        keep = bool(self.opts.get("keep_frames"))
        workdir = Path(tempfile.mkdtemp(prefix="videoupscale_realesrgan_", dir=tmp_dir))
        try:
            fin, fout = workdir / "in", workdir / "out"
            frames = vio.extract_frames(src, fin, self.opts.get("start"), self.opts.get("end"), "png")
            fout.mkdir(parents=True, exist_ok=True)
            cmd = [exe, "-i", str(fin), "-o", str(fout), "-n", model, "-s", str(scale), "-f", "png"]
            if self.opts.get("gpu_id") is not None:
                cmd += ["-g", str(self.opts["gpu_id"])]
            if self.opts.get("tile"):
                cmd += ["-t", str(self.opts["tile"])]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            stop = threading.Event()
            if progress:
                mon = threading.Thread(target=pipeline.monitor, args=(fout, len(frames), progress, stop), daemon=True)
                mon.start()
            try:
                while proc.poll() is None:
                    if should_cancel and should_cancel():
                        proc.terminate()
                        raise pipeline.Cancelled("Cancelado pelo usuário")
                    proc.wait(timeout=1.0)
            finally:
                stop.set()
            if proc.returncode != 0:
                err = (proc.stderr.read() if proc.stderr else "")[:400]
                raise RuntimeError(f"realesrgan-ncnn-vulkan falhou (código {proc.returncode}): {err}")
            vio.encode_frames(fout, src, dst, width, height, info,
                              codec=self.opts.get("codec", "libx264"), crf=self.opts.get("crf", 18),
                              preset=self.opts.get("preset", "medium"),
                              pix_fmt=vio.output_pix_fmt(self.opts.get("pix_fmt", "auto"), info,
                                                         self.opts.get("codec", "libx264")),
                              bitrate=self.opts.get("bitrate"),
                              extra_filters=self.opts.get("extra_filters", ""))
        finally:
            if keep:
                print(f"Frames mantidos em: {workdir}")
            else:
                shutil.rmtree(workdir, ignore_errors=True)
