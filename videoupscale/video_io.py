"""Leitura, probe e escrita de vídeo via FFmpeg.

O pipeline trabalha em três etapas (extração de frames -> upscale -> remuxagem),
o que permite paralelismo por processo e reaproveitamento entre backends.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

TARGETS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2k": (2048, 1080),
    "4k": (3840, 2160),
    "uhd": (3840, 2160),
    "5k": (5120, 2880),
    "8k": (7680, 4320),
}

FRAME_PATTERN = "f%08d"


def ffmpeg_bin() -> str:
    exe = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "FFmpeg não encontrado. Instale-o e deixe no PATH: https://ffmpeg.org/download.html "
            "(ou aponte a variável FFMPEG_BINARY para o executável)."
        )
    return exe


def require_ffmpeg() -> None:
    ffmpeg_bin()


def _run(cmd: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=quiet, text=True, check=False)


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: str
    fps_float: float
    frames: int
    duration: float
    has_audio: bool = False
    has_subtitle: bool = False
    pix_fmt: str = "yuv420p"
    bit_depth: int = 8
    rotation: int = 0
    vfr: bool = False
    size: int = 0
    color: dict[str, str] = field(default_factory=dict)

    @property
    def pixels(self) -> int:
        return self.width * self.height


def _parse_rate(rate: str, default: float = 30.0) -> float:
    try:
        if not rate or rate == "0/0":
            return default
        if "/" in rate:
            n, d = rate.split("/")
            return float(n) / float(d) if float(d) else default
        return float(rate)
    except ValueError:
        return default


def probe(path: str) -> VideoInfo:
    """Coleta metadados do vídeo. Usa ffprobe; cai para OpenCV se não houver ffprobe."""
    require_ffmpeg()
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    size = Path(path).stat().st_size
    if shutil.which("ffprobe"):
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(raw)
        streams = data.get("streams", [])
        v = next((s for s in streams if s["codec_type"] == "video"), None)
        if v is None:
            raise RuntimeError(f"{path} não contém um stream de vídeo")
        fps = v.get("r_frame_rate") or "30/1"
        fps_float = _parse_rate(fps)
        avg = _parse_rate(v.get("avg_frame_rate") or "", fps_float)
        frames = int(v.get("nb_frames") or 0)
        duration = float(v.get("duration") or 0) or float(data.get("format", {}).get("duration") or 0)
        if not frames and duration:
            frames = int(round(duration * fps_float))
        pix_fmt = v.get("pix_fmt", "yuv420p")
        depth = int(re.sub(r"\D", "", pix_fmt.split("p")[-1]) or 8) if re.search(r"p\d+", pix_fmt) else 8
        depth = depth if depth in (8, 10, 12, 16) else 8
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"])) % 360
        for tag_key in ("rotate",):
            if tag_key in (v.get("tags") or {}):
                rot = int(float(v["tags"][tag_key])) % 360
        color = {k: v[k] for k in ("color_primaries", "color_transfer", "color_space", "color_range") if v.get(k)}
        return VideoInfo(
            path=path, width=int(v["width"]), height=int(v["height"]), fps=fps, fps_float=fps_float,
            frames=frames, duration=duration or (frames / fps_float if fps_float else 0),
            has_audio=any(s["codec_type"] == "audio" for s in streams),
            has_subtitle=any(s["codec_type"] == "subtitle" for s in streams),
            pix_fmt=pix_fmt, bit_depth=depth, rotation=rot,
            vfr=abs(avg - fps_float) > 0.01 * max(fps_float, 1), size=size, color=color,
        )
    return _probe_cv2(path, size)


def _probe_cv2(path: str, size: int) -> VideoInfo:
    """Fallback sem ffprobe (menos preciso: sem taxa de bits por cor HDR etc.)."""
    import cv2  # import local: só necessário neste caminho

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"Não foi possível abrir {path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    err = _run([ffmpeg_bin(), "-hide_banner", "-i", path]).stderr or ""
    return VideoInfo(
        path=path, width=w, height=h, fps=f"{fps:.6f}", fps_float=fps, frames=frames,
        duration=frames / fps if fps else 0,
        has_audio=bool(re.search(r"Stream #.*Audio", err)),
        has_subtitle=bool(re.search(r"Stream #.*Subtitle", err)),
        size=size,
    )


def target_size(info: VideoInfo, scale: float | None, target: str | None) -> tuple[int, int]:
    """Resolução final (dimensões pares), mantendo a proporção dentro do alvo."""
    if target:
        t = target.lower()
        if t in TARGETS:
            tw, th = TARGETS[t]
        elif re.fullmatch(r"\d{2,5}x\d{2,5}", t):
            tw, th = (int(x) for x in t.split("x"))
        else:
            raise ValueError(f"Alvo inválido: {target}. Use {', '.join(TARGETS)} ou LxA (ex.: 3840x2160)")
        f = min(tw / info.width, th / info.height)
    else:
        f = scale or 2.0
        if f <= 0:
            raise ValueError("Fator de escala deve ser positivo")
    w, h = round(info.width * f), round(info.height * f)
    return max(w - w % 2, 2), max(h - h % 2, 2)


def is_upscale(info: VideoInfo, width: int, height: int) -> bool:
    return width > info.width or height > info.height


# --------------------------------------------------------------------------- codecs

def available_encoders() -> set[str]:
    out = _run([ffmpeg_bin(), "-hide_banner", "-encoders"]).stdout or ""
    return set(re.findall(r"^\s*V\.{6}\s+(\S+)", out, re.M))


def pick_encoder(requested: str = "auto") -> str:
    """'auto' escolhe o melhor encoder de hardware disponível; senão valida o pedido."""
    if requested != "auto":
        return requested
    enc = available_encoders()
    for cand in ("hevc_nvenc", "h264_nvenc", "hevc_qsv", "h264_qsv", "hevc_amf", "h264_amf",
                 "hevc_videotoolbox", "h264_videotoolbox", "libx265", "libx264"):
        if cand in enc:
            return cand
    return "libx264"


def encoder_args(codec: str, crf: int = 18, preset: str = "medium", bitrate: str | None = None) -> list[str]:
    args = ["-c:v", codec]
    if bitrate:
        args += ["-b:v", bitrate]
        return args
    if codec in ("libx264", "libx265"):
        args += ["-crf", str(crf), "-preset", preset]
        if codec == "libx265":
            args += ["-tag:v", "hvc1"]
    elif codec.endswith("_nvenc"):
        args += ["-cq", str(crf), "-preset", "p5"]
    elif codec.endswith("_qsv"):
        args += ["-global_quality", str(crf), "-preset", preset]
    elif codec.endswith("_amf"):
        args += ["-quality", "balanced"]
    elif codec.endswith("_videotoolbox"):
        args += ["-q:v", str(min(100, crf * 5))]
    return args


def output_pix_fmt(requested: str, info: VideoInfo, codec: str) -> str:
    """Escolhe o pixel format: preserva 10-bit/HDR quando faz sentido."""
    if requested != "auto":
        return requested
    if info.bit_depth >= 10 or codec in ("libx265", "hevc_nvenc", "hevc_qsv"):
        return "yuv420p10le" if info.bit_depth >= 10 else "yuv420p"
    return "yuv420p"


# --------------------------------------------------------------------------- frames

def extract_frames(src: str, out_dir: Path, start: str | None = None, end: str | None = None,
                   ext: str = "png") -> list[Path]:
    """Extrai frames do vídeo (PNG sem perdas ou JPEG rápido/leve)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-hide_banner"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src]
    if end:
        cmd += ["-to", str(end)]
    cmd += ["-vsync", "0"]
    cmd += ["-q:v", "1"] if ext.lower() in ("jpg", "jpeg") else []
    cmd += [str(out_dir / f"{FRAME_PATTERN}.{ext}")]
    res = _run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"Falha ao extrair frames: {res.stderr.strip()[:400]}")
    frames = sorted(out_dir.glob(f"f*.{ext}"))
    if not frames:
        raise RuntimeError("Nenhum frame extraído — verifique o arquivo de entrada.")
    return frames


def encode_frames(frames_dir: Path, src: str, dst: str, width: int, height: int, info: VideoInfo,
                  codec: str = "libx264", crf: int = 18, preset: str = "medium",
                  pix_fmt: str = "yuv420p", bitrate: str | None = None,
                  extra_filters: str = "", copy_subtitles: bool = True) -> None:
    """Remonta os frames em vídeo, preservando áudio, legendas, capítulos e metadados."""
    filters = f"scale={width}:{height}:flags=lanczos,setsar=1"
    if extra_filters:
        filters += "," + extra_filters
    if not _filter_keeps_format(filters, pix_fmt):
        filters += f",format={pix_fmt}"
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-hide_banner",
           "-framerate", f"{info.fps_float:.6f}", "-i", str(frames_dir / f"{FRAME_PATTERN}.png"),
           "-i", src,
           "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy"]
    if copy_subtitles and info.has_subtitle:
        cmd += ["-map", "1:s?", "-c:s", "copy"]
    cmd += ["-map_metadata", "1", "-map_chapters", "1",
            "-vf", filters,
            *encoder_args(codec, crf, preset, bitrate),
            "-pix_fmt", pix_fmt, "-movflags", "+faststart", "-shortest", dst]
    res = _run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"Falha ao codificar vídeo: {res.stderr.strip()[:400]}")


def _filter_keeps_format(filters: str, pix_fmt: str) -> bool:
    return f"format={pix_fmt}" in filters


def read_frames(path: str, info: VideoInfo) -> Iterator[np.ndarray]:
    """Stream de frames BGR (útil para backends simples/sequenciais)."""
    proc = subprocess.Popen(
        [ffmpeg_bin(), "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE,
    )
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
    """Escreve frames BGR diretamente no encoder (sem pasta temporária)."""

    def __init__(self, out: str, src: str, w: int, h: int, info: VideoInfo,
                 codec: str = "libx264", crf: int = 18, preset: str = "medium",
                 pix_fmt: str = "yuv420p"):
        import cv2
        self._cv2 = cv2
        filters = f"scale={w}:{h}:flags=lanczos,setsar=1,format={pix_fmt}"
        cmd = [ffmpeg_bin(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{w}x{h}", "-r", f"{info.fps_float:.6f}", "-i", "-", "-i", src,
               "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy",
               "-map_metadata", "1", "-vf", filters,
               *encoder_args(codec, crf, preset), "-pix_fmt", pix_fmt,
               "-movflags", "+faststart", "-shortest", out]
        self.size = (w, h)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        if (frame.shape[1], frame.shape[0]) != self.size:
            frame = self._cv2.resize(frame, self.size, interpolation=self._cv2.INTER_LANCZOS4)
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError("FFmpeg falhou ao codificar o vídeo de saída")


def free_bytes(path: str | Path) -> int:
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize
