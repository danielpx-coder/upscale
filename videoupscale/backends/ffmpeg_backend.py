import subprocess

from .. import video_io as vio
from .base import Backend, Capabilities


class FFmpegBackend(Backend):
    name = "ffmpeg"
    description = "Gratuito e rapidíssimo. Lanczos + nitidez, sem IA (bom para limpar/estabilizar)."
    capabilities = Capabilities(quality=2, speed=5, install="Instale o FFmpeg: https://ffmpeg.org/download.html")

    def available(self):
        return (True, "") if _has_ffmpeg() else (False, "ffmpeg não está no PATH")

    def upscale(self, src, dst, width, height, progress=None, should_cancel=None):
        info = vio.probe(src)
        pix_fmt = vio.output_pix_fmt(self.opts.get("pix_fmt", "auto"), info, self.opts.get("codec", "libx264"))
        sharpen = self.opts.get("sharpen", True)
        filters = f"scale={width}:{height}:flags=lanczos,setsar=1"
        if sharpen:
            filters += ",unsharp=5:5:0.6:5:5:0.0"
        cmd = [
            vio.ffmpeg_bin(), "-y", "-v", "error", "-hide_banner", "-i", src,
            "-vf", filters,
            *vio.encoder_args(self.opts.get("codec", "libx264"), self.opts.get("crf", 18),
                              self.opts.get("preset", "medium"), self.opts.get("bitrate")),
            "-c:a", "copy",
        ]
        if info.has_subtitle:
            cmd += ["-c:s", "copy"]
        cmd += ["-map_metadata", "0", "-map_chapters", "0", "-pix_fmt", pix_fmt,
                "-movflags", "+faststart", dst]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg falhou: {res.stderr.strip()[:400]}")


def _has_ffmpeg() -> bool:
    try:
        vio.ffmpeg_bin()
        return True
    except RuntimeError:
        return False
