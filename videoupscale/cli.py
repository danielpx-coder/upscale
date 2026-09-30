from __future__ import annotations
import argparse, sys, time
from pathlib import Path
from .backends import BACKENDS
from .video_io import TARGETS, probe, target_size


def progress_bar(done: int, total: int, _t=[time.time()]):
    el = time.time() - _t[0]
    if total:
        pct = done / total
        eta = el / pct - el if pct else 0
        bar = "#" * int(pct * 30)
        sys.stdout.write(f"\r[{bar:<30}] {done}/{total} {pct:5.1%} ETA {eta:5.0f}s")
    else:
        sys.stdout.write(f"\r{done} frames  {el:5.0f}s")
    sys.stdout.flush()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="videoupscale", description="Upscale de vídeos até 4K/8K")
    p.add_argument("input", nargs="?")
    p.add_argument("-o", "--output")
    p.add_argument("-b", "--backend", default="ffmpeg", choices=BACKENDS)
    g = p.add_mutually_exclusive_group()
    g.add_argument("-t", "--target", help=f"{', '.join(TARGETS)} ou LxA (ex.: 3840x2160)")
    g.add_argument("-s", "--scale", type=float, help="fator (ex.: 2, 4)")
    p.add_argument("-m", "--model", help="modelo do backend (ex.: edsr, realesrgan-x4plus, prob-4)")
    p.add_argument("--codec", default="libx264", help="libx264, libx265, h264_nvenc, hevc_nvenc...")
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="medium")
    p.add_argument("--gpu", action="store_true", help="usar CUDA (opencv)")
    p.add_argument("--gpu-id", type=int)
    p.add_argument("--realesrgan-bin")
    p.add_argument("--list", action="store_true", help="listar backends")
    p.add_argument("--gui", action="store_true", help="abrir interface gráfica")
    a = p.parse_args(argv)

    if a.gui:
        from .gui import run
        return run()
    if a.list or not a.input:
        for n, b in BACKENDS.items():
            print(f"  {n:<11} {'GRÁTIS' if b.free else 'PAGO  '}  {b.description}")
        return 0 if a.list else p.print_usage() or 1

    info = probe(a.input)
    w, h = target_size(info, a.scale, a.target or (None if a.scale else "4k"))
    out = a.output or str(Path(a.input).with_name(f"{Path(a.input).stem}_{w}x{h}_{a.backend}.mp4"))
    print(f"{info.width}x{info.height} -> {w}x{h}  | backend: {a.backend} | saída: {out}")
    backend = BACKENDS[a.backend](model=a.model, codec=a.codec, crf=a.crf, preset=a.preset,
                                  gpu=a.gpu, gpu_id=a.gpu_id, realesrgan_bin=a.realesrgan_bin)
    t = time.time()
    backend.upscale(a.input, out, w, h, progress_bar)
    print(f"\nConcluído em {time.time() - t:.1f}s -> {out}")
    return 0
