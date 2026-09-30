"""Leitura/escrita de vídeo via FFmpeg (pipes de frames brutos), preservando áudio."""
from __future__ import annotations
import json, shutil, subprocess
from dataclasses import dataclass
from typing import Iterator
import numpy as np

TARGETS = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440),
           "4k": (3840, 2160), "8k": (7680, 4320)}


def require_ffmpeg() -> None:
    for b in ("ffmpeg",):
        if not shutil.which(b):
            raise RuntimeError(f"'{b}' não encontrado no PATH. Instale o FFmpeg: https://ffmpeg.org/download.html")


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: str
    frames: int
    has_audio: bool


def probe(path: str) -> VideoInfo:
    require_ffmpeg()
    if not shutil.which("ffprobe"):
        return _probe_cv2(path)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    streams = json.loads(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    frames = int(v.get("nb_frames") or 0)
    return VideoInfo(int(v["width"]), int(v["height"]), v.get("r_frame_rate", "30/1"), frames,
                     any(s["codec_type"] == "audio" for s in streams))


def _probe_cv2(path: str) -> VideoInfo:
    import re, cv2
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"Não foi possível abrir {path}")
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps, n = cap.get(cv2.CAP_PROP_FPS) or 30, int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    return VideoInfo(w, h, f"{fps:.6g}", n, bool(re.search(r"Stream #.*Audio", err)))


def target_size(info: VideoInfo, scale: float | None, target: str | None) -> tuple[int, int]:
    """Calcula resolução final mantendo proporção (dimensões pares)."""
    if target:
        t = target.lower()
        if t in TARGETS:
            tw, th = TARGETS[t]
        elif "x" in t:
            tw, th = map(int, t.split("x"))
        else:
            raise ValueError(f"Alvo inválido: {target}")
        # encaixa dentro do alvo mantendo a proporção
        f = min(tw / info.width, th / info.height)
    else:
        f = scale or 2.0
    w, h = round(info.width * f), round(info.height * f)
    return w - w % 2, h - h % 2


def read_frames(path: str, info: VideoInfo) -> Iterator[np.ndarray]:
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
                             "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    size = info.width * info.height * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(info.height, info.width, 3)
    finally:
        proc.stdout.close()
        proc.wait()


class FrameWriter:
    """Escreve frames BGR em um vídeo, copiando o áudio do original."""

    def __init__(self, out: str, src: str, w: int, h: int, fps: str, has_audio: bool,
                 codec: str = "libx264", crf: int = 18, preset: str = "medium"):
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{w}x{h}", "-r", fps, "-i", "-"]
        if has_audio:
            cmd += ["-i", src, "-map", "0:v:0", "-map", "1:a?", "-c:a", "aac", "-b:a", "192k"]
        cmd += encoder_args(codec, crf, preset) + ["-pix_fmt", "yuv420p", "-shortest", out]
        self.size = (w, h)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        if (frame.shape[1], frame.shape[0]) != self.size:
            import cv2
            frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_LANCZOS4)
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError("FFmpeg falhou ao codificar o vídeo de saída")


def encoder_args(codec: str, crf: int, preset: str) -> list[str]:
    if codec in ("libx264", "libx265"):
        a = ["-c:v", codec, "-crf", str(crf), "-preset", preset]
        return a + (["-tag:v", "hvc1"] if codec == "libx265" else [])
    if codec.endswith("_nvenc"):
        return ["-c:v", codec, "-cq", str(crf), "-preset", "p5"]
    return ["-c:v", codec]
