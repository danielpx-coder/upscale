import subprocess
from .base import Backend
from ..video_io import encoder_args, require_ffmpeg


class FFmpegBackend(Backend):
    name = "ffmpeg"
    description = "Gratuito, muito rápido. Lanczos + nitidez (sem IA)."

    def upscale(self, src, dst, width, height, progress=None):
        require_ffmpeg()
        vf = f"scale={width}:{height}:flags=lanczos,unsharp=5:5:0.6:5:5:0.0"
        cmd = ["ffmpeg", "-y", "-v", "error", "-stats", "-i", src, "-vf", vf,
               *encoder_args(self.opts.get("codec", "libx264"), self.opts.get("crf", 18),
                             self.opts.get("preset", "medium")),
               "-pix_fmt", "yuv420p", "-c:a", "copy", dst]
        subprocess.run(cmd, check=True)
