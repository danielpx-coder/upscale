"""Orquestração do upscale: extrai frames, processa em paralelo e remonta o vídeo."""
from __future__ import annotations

import multiprocessing as mp
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import video_io as vio

Progress = Callable[[int, int], None]
UpscalerFactory = Callable[[], Callable]


class Cancelled(Exception):
    """Levantada quando o usuário pede cancelamento."""


@dataclass
class PipelineOptions:
    width: int
    height: int
    workers: int = 0
    frame_ext: str = "png"
    keep_frames: bool = False
    tmp_dir: str | None = None
    start: str | None = None
    end: str | None = None
    codec: str = "libx264"
    crf: int = 18
    preset: str = "medium"
    pix_fmt: str = "yuv420p"
    bitrate: str | None = None
    extra_filters: str = ""
    copy_subtitles: bool = True


_UPSCALER = None


def _worker_init(factory: UpscalerFactory) -> None:
    global _UPSCALER
    _UPSCALER = factory()


def _worker_run(job: tuple[str, str, int, int]) -> str:
    import cv2

    src, dst, w, h = job
    img = cv2.imread(src, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"Falha ao ler frame {src}")
    out = _UPSCALER(img)
    out = _ensure_bgr(out)
    if (out.shape[1], out.shape[0]) != (w, h):
        out = cv2.resize(out, (w, h), interpolation=cv2.INTER_LANCZOS4)
    params = [cv2.IMWRITE_PNG_COMPRESSION, 3] if dst.endswith(".png") else [cv2.IMWRITE_JPEG_QUALITY, 100]
    if not cv2.imwrite(dst, out, params):
        raise RuntimeError(f"Falha ao gravar frame {dst}")
    return dst


def _ensure_bgr(img):
    import cv2

    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def run(src: str, dst: str, info: vio.VideoInfo, opts: PipelineOptions,
        make_upscaler: UpscalerFactory, progress: Progress | None = None,
        should_cancel: Callable[[], bool] | None = None) -> None:
    """Executa o upscale completo usando `make_upscaler` (chamado uma vez por processo)."""
    vio.require_ffmpeg()
    workers = opts.workers or max(1, (mp.cpu_count() or 2) - 1)
    tmp = Path(opts.tmp_dir) if opts.tmp_dir else None
    workdir = Path(tempfile.mkdtemp(prefix="videoupscale_", dir=tmp))
    try:
        in_dir, out_dir = workdir / "in", workdir / "out"
        frames = vio.extract_frames(src, in_dir, opts.start, opts.end, opts.frame_ext)
        total = len(frames)
        out_dir.mkdir(parents=True, exist_ok=True)
        need = total * 3 * opts.width * opts.height  # estimativa grosseira de disco
        avail = vio.free_bytes(workdir)
        if avail < need * 1.2:
            raise RuntimeError(
                f"Espaço insuficiente em {workdir}: {avail / 2**30:.1f} GB livres, "
                f"~{need / 2**30:.1f} GB necessários. Use --tmp-dir ou --frame-ext jpg."
            )
        jobs = [(str(f), str(out_dir / f.name), opts.width, opts.height) for f in frames]
        done = 0
        ctx = mp.get_context()
        with ctx.Pool(processes=min(workers, total), initializer=_worker_init, initargs=(make_upscaler,)) as pool:
            for _ in pool.imap_unordered(_worker_run, jobs, chunksize=1):
                done += 1
                if progress:
                    progress(done, total)
                if should_cancel and should_cancel():
                    pool.terminate()
                    raise Cancelled("Cancelado pelo usuário")
        if len(list(out_dir.glob(f"f*.{opts.frame_ext}"))) != total:
            raise RuntimeError("Alguns frames não foram processados; abortando para não gerar vídeo truncado.")
        vio.encode_frames(out_dir, src, dst, opts.width, opts.height, info, codec=opts.codec,
                          crf=opts.crf, preset=opts.preset, pix_fmt=opts.pix_fmt, bitrate=opts.bitrate,
                          extra_filters=opts.extra_filters, copy_subtitles=opts.copy_subtitles)
    finally:
        if opts.keep_frames:
            print(f"Frames mantidos em: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def monitor(out_dir: Path, total: int, progress: Progress, stop: threading.Event) -> None:
    """Progresso aproximado para backends externos que só gravam arquivos (ex.: Real-ESRGAN)."""
    while not stop.is_set():
        progress(len(list(out_dir.glob("*.png"))), total)
        if total and len(list(out_dir.glob("*.png"))) >= total:
            return
        time.sleep(1.0)
