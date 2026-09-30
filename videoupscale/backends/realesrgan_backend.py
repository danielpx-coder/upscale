"""Real-ESRGAN (ncnn-vulkan) - gratuito, melhor qualidade local. Roda em GPU AMD/NVIDIA/Intel.

Baixe o executável em https://github.com/xinntao/Real-ESRGAN/releases
(realesrgan-ncnn-vulkan-*-{windows,ubuntu,macos}.zip) e coloque no PATH ou use --realesrgan-bin.
Modelos: realesrgan-x4plus, realesrgan-x4plus-anime, realesr-animevideov3 (x2/x3/x4).
"""
from __future__ import annotations
import shutil, subprocess, tempfile
from pathlib import Path
from .base import Backend
from ..video_io import encoder_args, probe


class RealESRGANBackend(Backend):
    name = "realesrgan"
    description = "Gratuito, IA de alta qualidade (Real-ESRGAN ncnn-vulkan, GPU)."

    def upscale(self, src, dst, width, height, progress=None):
        exe = self.opts.get("realesrgan_bin") or shutil.which("realesrgan-ncnn-vulkan")
        if not exe:
            raise RuntimeError("realesrgan-ncnn-vulkan não encontrado. Veja https://github.com/xinntao/Real-ESRGAN/releases")
        info = probe(src)
        model = self.opts.get("model") or "realesr-animevideov3"
        need = max(width / info.width, height / info.height)
        scale = 4 if "x4plus" in model else next((s for s in (2, 3, 4) if s >= need), 4)
        with tempfile.TemporaryDirectory(prefix="vup_") as tmp:
            fin, fout = Path(tmp, "in"), Path(tmp, "out")
            fin.mkdir(); fout.mkdir()
            print("[1/3] Extraindo frames...")
            subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-qscale:v", "1", "-qmin", "1",
                            "-vsync", "0", str(fin / "f%08d.png")], check=True)
            total = len(list(fin.iterdir()))
            print(f"[2/3] Upscale de {total} frames (x{scale}, {model})...")
            cmd = [exe, "-i", str(fin), "-o", str(fout), "-n", model, "-s", str(scale), "-f", "png"]
            if self.opts.get("gpu_id") is not None:
                cmd += ["-g", str(self.opts["gpu_id"])]
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import time
            while p.poll() is None:
                if progress:
                    progress(len(list(fout.iterdir())), total)
                time.sleep(1)
            if p.returncode != 0:
                raise RuntimeError("realesrgan-ncnn-vulkan falhou")
            print("[3/3] Montando vídeo final...")
            cmd = ["ffmpeg", "-y", "-v", "error", "-framerate", info.fps, "-i", str(fout / "f%08d.png"),
                   "-i", src, "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy",
                   "-vf", f"scale={width}:{height}:flags=lanczos",
                   *encoder_args(self.opts.get("codec", "libx264"), self.opts.get("crf", 18),
                                 self.opts.get("preset", "medium")),
                   "-pix_fmt", "yuv420p", dst]
            subprocess.run(cmd, check=True)
