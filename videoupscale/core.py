"""Núcleo reaproveitado por CLI e GUI: resolução, execução de jobs, preview e diagnóstico."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import video_io as vio
from .backends import BACKENDS
from .backends import get as get_backend

LOCAL_BACKENDS = ("ffmpeg", "opencv", "torch", "realesrgan")


@dataclass
class JobResult:
    src: str
    dst: str
    src_size: tuple[int, int]
    dst_size: tuple[int, int]
    backend: str
    seconds: float
    in_bytes: int = 0
    out_bytes: int = 0
    skipped: bool = False
    note: str = ""


@dataclass
class Options:
    backend: str = "ffmpeg"
    model: str | None = None
    target: str | None = "4k"
    scale: float | None = None
    codec: str = "auto"
    crf: int = 18
    preset: str = "medium"
    bitrate: str | None = None
    pix_fmt: str = "auto"
    workers: int = 0
    gpu: bool = False
    gpu_id: int | None = None
    device: str = "auto"
    fp16: bool = True
    tile: int = 0
    frame_ext: str = "png"
    tmp_dir: str | None = None
    keep_frames: bool = False
    start: str | None = None
    end: str | None = None
    sharpen: bool = True
    extra_filters: str = ""
    extra_inputs: dict = field(default_factory=dict)
    resolution: str | None = None
    realesrgan_bin: str | None = None
    overwrite: bool = False
    dry_run: bool = False


def resolve_codec(codec: str) -> str:
    return vio.pick_encoder(codec)


def plan(src: str, opts: Options) -> tuple[vio.VideoInfo, int, int]:
    info = vio.probe(src)
    w, h = vio.target_size(info, opts.scale, opts.target)
    return info, w, h


def default_output(src: str, width: int, height: int, backend: str, out_dir: str | None = None) -> str:
    p = Path(src)
    name = f"{p.stem}_{width}x{height}_{backend}{p.suffix or '.mp4'}"
    return str((Path(out_dir) / name) if out_dir else p.with_name(name))


def backend_opts(opts: Options, info: vio.VideoInfo) -> dict:
    return dict(model=opts.model, codec=resolve_codec(opts.codec), crf=opts.crf, preset=opts.preset,
                bitrate=opts.bitrate, pix_fmt=opts.pix_fmt, workers=opts.workers, gpu=opts.gpu,
                gpu_id=opts.gpu_id, device=opts.device, fp16=opts.fp16, tile=opts.tile,
                frame_ext=opts.frame_ext, tmp_dir=opts.tmp_dir, keep_frames=opts.keep_frames,
                start=opts.start, end=opts.end, sharpen=opts.sharpen,
                extra_filters=opts.extra_filters, extra_inputs=opts.extra_inputs,
                resolution=opts.resolution, sr_model=opts.model if opts.backend == "replicate" else None,
                realesrgan_bin=opts.realesrgan_bin)


def run_job(src: str, dst: str | None, opts: Options, progress=None, should_cancel=None) -> JobResult:
    t0 = time.time()
    info, w, h = plan(src, opts)
    dst = dst or default_output(src, w, h, opts.backend)
    out = Path(dst)
    in_bytes = Path(src).stat().st_size
    if out.exists() and not opts.overwrite:
        return JobResult(src, dst, (info.width, info.height), (w, h), opts.backend, 0.0,
                         in_bytes, out.stat().st_size, skipped=True,
                         note="saída já existe (use --overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    if opts.dry_run:
        return JobResult(src, dst, (info.width, info.height), (w, h), opts.backend, 0.0, in_bytes, 0,
                         skipped=True, note="simulação (--dry-run)")
    backend = get_backend(opts.backend, **backend_opts(opts, info))
    backend.check()
    backend.upscale(src, dst, w, h, progress, should_cancel)
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"O backend não gerou um arquivo válido: {dst}")
    return JobResult(src, dst, (info.width, info.height), (w, h), opts.backend, time.time() - t0,
                     in_bytes, out.stat().st_size)


def preview(src: str, backend_name: str, width: int, height: int, out_png: str, opts: Options,
            at: str | None = None) -> str:
    """Gera uma imagem lado a lado (original | upscale) para avaliar antes de processar o vídeo todo."""
    if backend_name not in LOCAL_BACKENDS:
        raise RuntimeError(f"Preview disponível apenas para backends locais: {', '.join(LOCAL_BACKENDS)}")
    import cv2

    info = vio.probe(src)
    with tempfile.TemporaryDirectory(prefix="videoupscale_preview_") as tmp:
        tmp = Path(tmp)
        cmd = [vio.ffmpeg_bin(), "-y", "-v", "error"]
        if at:
            cmd += ["-ss", str(at)]
        cmd += ["-i", src, "-frames:v", "1", str(tmp / "src.png")]
        subprocess.run(cmd, check=True, capture_output=True)
        single = tmp / "clip.mp4"
        subprocess.run([vio.ffmpeg_bin(), "-y", "-v", "error", "-loop", "1", "-i", str(tmp / "src.png"),
                        "-t", "1", "-r", "1", "-pix_fmt", "yuv420p", str(single)],
                       check=True, capture_output=True)
        prev_opts = Options(**{**opts.__dict__, "backend": backend_name, "overwrite": True,
                               "target": None, "scale": max(width / info.width, height / info.height)})
        run_job(str(single), str(tmp / "out.mp4"), prev_opts)
        subprocess.run([vio.ffmpeg_bin(), "-y", "-v", "error", "-i", str(tmp / "out.mp4"),
                        "-frames:v", "1", str(tmp / "up.png")], check=True, capture_output=True)
        a = cv2.imread(str(tmp / "src.png"))
        b = cv2.imread(str(tmp / "up.png"))
        if b is None:
            raise RuntimeError("Falha ao gerar o frame upscalado")
        side = cv2.hconcat([cv2.resize(a, (b.shape[1], b.shape[0])), b])
        cv2.imwrite(out_png, side)
    return out_png


def find_videos(folder: str | Path, exts: tuple[str, ...] = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"),
                recursive: bool = False) -> list[Path]:
    base = Path(folder)
    norm = tuple(e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts)
    it = base.rglob("*") if recursive else base.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in norm)


def vmaf(dist: str, ref: str) -> float | None:
    """Compara o vídeo final com o original usando VMAF (requer FFmpeg com libvmaf).

    O original é reescalado para a resolução do upscale com bicubic para servir de referência.
    """
    import json
    import subprocess
    from pathlib import Path as _P

    d = vio.probe(dist)
    log = _P(tempfile.mkdtemp(prefix="videoupscale_vmaf_")) / "vmaf.json"
    cmd = [vio.ffmpeg_bin(), "-y", "-v", "info", "-i", dist, "-i", ref, "-filter_complex",
           f"[1:v]scale={d.width}:{d.height}:flags=bicubic[ref];[0:v][ref]"
           f"libvmaf=log_fmt=json:log_path={log}", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("FFmpeg sem suporte a libvmaf (compile/instale um build com --enable-libvmaf): "
                           f"{res.stderr.strip()[-300:]}")
    if log.is_file():
        data = json.loads(log.read_text())
        return float(data["pooled_metrics"]["vmaf"]["mean"])
    m = [ln for ln in res.stderr.splitlines() if "VMAF score" in ln]
    return float(m[-1].split()[-1]) if m else None


def doctor() -> list[tuple[str, str]]:
    """Verifica o ambiente: ffmpeg, encoders de hardware, backends e GPUs."""
    rows: list[tuple[str, str]] = []
    try:
        exe = vio.ffmpeg_bin()
        ver = subprocess.run([exe, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        rows.append(("ffmpeg", ver.replace("ffmpeg version", "").strip() or exe))
    except RuntimeError as e:
        rows.append(("ffmpeg", f"AUSENTE - {e}"))
    rows.append(("ffprobe", shutil.which("ffprobe") or "ausente (usa fallback do OpenCV)"))
    try:
        enc = sorted(vio.available_encoders())
        hw = [e for e in enc if any(k in e for k in ("nvenc", "qsv", "amf", "videotoolbox"))]
        rows.append(("encoder sugerido", vio.pick_encoder("auto")))
        rows.append(("encoders de hardware", ", ".join(hw) or "nenhum (usará CPU libx264/libx265)"))
    except RuntimeError:
        pass
    try:
        import cv2

        rows.append(("opencv", cv2.__version__))
        rows.append(("opencv dnn_superres", "sim" if hasattr(cv2, "dnn_superres") else
                     "não (instale opencv-contrib-python)"))
    except ImportError:
        rows.append(("opencv", "ausente"))
    try:
        import torch

        rows.append(("torch", f"{torch.__version__} | CUDA {'sim' if torch.cuda.is_available() else 'não'}"
                              f" | GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-'}"))
    except ImportError:
        rows.append(("torch", "ausente (backend 'torch' desativado: pip install torch spandrel)"))
    for name, cls in BACKENDS.items():
        inst = cls()
        ok, why = inst.available() if hasattr(inst, "available") else (True, "")
        env = ",".join(cls.capabilities.env) or "-"
        rows.append((f"backend {name}", ("pronto" if ok else f"indisponível ({why})") + f" | env: {env}"))
    return rows
