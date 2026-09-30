from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, core
from . import video_io as vio
from .backends import BACKENDS
from .config import load as load_config

_BAR_START = [time.time()]


def progress_bar(done: int, total: int) -> None:
    el = time.time() - _BAR_START[0]
    if total:
        pct = done / total
        eta = (el / pct - el) if pct else 0
        rate = done / el if el else 0
        bar = "#" * int(pct * 28)
        msg = f"[{bar:<28}] {done}/{total} {pct:5.1%} | {rate:5.1f} fps | ETA {eta:5.0f}s"
    else:
        msg = f"{done} frames | {el:5.0f}s"
    sys.stdout.write("\r" + msg)
    sys.stdout.flush()


def _stars(n: int) -> str:
    return "*" * n + "-" * (5 - n)


def print_backends() -> None:
    print("Backends disponíveis:")
    for name, cls in BACKENDS.items():
        c = cls.capabilities
        print(f"  {name:<11} {'GRÁTIS' if c.free else 'PAGO ':<6} qualidade {_stars(c.quality)}  "
              f"velocidade {_stars(c.speed)}  {cls.description}")
        if c.models:
            print(f"  {'':<11} modelos: {', '.join(c.models)} (padrão: {c.default_model})")


def print_doctor() -> None:
    print("Diagnóstico do ambiente")
    print("-" * 72)
    for k, v in core.doctor():
        print(f"  {k:<24} {v}")


def print_info(path: str) -> None:
    info = vio.probe(path)
    print(f"Arquivo      : {info.path}")
    print(f"Resolução    : {info.width}x{info.height}  (rotação {info.rotation}°)")
    print(f"FPS          : {info.fps_float:.3f} ({info.fps}){'  [VFR]' if info.vfr else ''}")
    print(f"Duração      : {info.duration:.2f}s  |  {info.frames} frames")
    print(f"Pixel format : {info.pix_fmt} ({info.bit_depth}-bit)")
    print(f"Áudio        : {'sim' if info.has_audio else 'não'}  | Legendas: {'sim' if info.has_subtitle else 'não'}")
    mb = info.size / 2**20
    print(f"Tamanho      : {mb:.1f} MB" if mb >= 1 else f"Tamanho      : {info.size / 2**10:.0f} KB")
    if info.color:
        print(f"Cor/HDR      : {info.color}")
    for t in ("720p", "1080p", "1440p", "4k", "8k"):
        w, h = vio.target_size(info, None, t)
        print(f"  alvo {t:<6} -> {w}x{h}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="videoupscale",
        description="Upscale profissional de vídeos até 4K/8K (backends locais gratuitos e APIs de IA).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""exemplos:
  videoupscale video.mp4 -t 4k                            # ffmpeg, rápido
  videoupscale video.mp4 -b torch -m realesrgan-x4plus -t 4k --tile 512
  videoupscale video.mp4 -b realesrgan -m realesr-animevideov3 -t 1080p
  videoupscale pasta/ --recursive -t 4k -b opencv -m fsrcnn --out-dir up4k/
  videoupscale video.mp4 -b topaz -t 4k -m prob-4         # requer TOPAZ_API_KEY
  videoupscale --preview video.mp4 -b torch -t 4k         # testa em 1 frame antes
  videoupscale --doctor                                   # diagnóstico do ambiente
""")
    p.add_argument("input", nargs="?", help="arquivo de vídeo ou pasta (com --recursive para lote)")
    p.add_argument("-o", "--output")
    p.add_argument("--out-dir", help="pasta de saída no modo lote")
    p.add_argument("-b", "--backend", default=None, choices=list(BACKENDS))
    g = p.add_mutually_exclusive_group()
    g.add_argument("-t", "--target", help=f"{', '.join(vio.TARGETS)} ou LxA (ex.: 3840x2160)")
    g.add_argument("-s", "--scale", type=float, help="fator de escala (ex.: 2, 3, 4)")
    p.add_argument("-m", "--model")
    p.add_argument("--codec", help="auto, libx264, libx265, h264_nvenc, hevc_nvenc, hevc_qsv...")
    p.add_argument("--crf", type=int)
    p.add_argument("--preset")
    p.add_argument("--bitrate", help="bitrate alvo (ex.: 40M) — alternativa ao CRF")
    p.add_argument("--pix-fmt", help="auto, yuv420p, yuv420p10le")
    p.add_argument("--workers", type=int, help="processos de upscale (padrão: nº de CPUs - 1)")
    p.add_argument("--gpu", action="store_true", help="usar CUDA (backend opencv)")
    p.add_argument("--gpu-id", type=int)
    p.add_argument("--device", help="dispositivo do backend torch: auto, cuda, mps, cpu")
    p.add_argument("--no-fp16", action="store_true", help="desativa meia precisão no backend torch")
    p.add_argument("--tile", type=int, help="tamanho do tile em px (torch); evita falta de VRAM em 4K")
    p.add_argument("--frame-ext", choices=("png", "jpg"), help="formato dos frames temporários")
    p.add_argument("--tmp-dir", help="pasta para frames temporários (use um disco rápido)")
    p.add_argument("--keep-frames", action="store_true")
    p.add_argument("--start", help="tempo inicial (ex.: 00:01:30 ou 90)")
    p.add_argument("--end", help="tempo final")
    p.add_argument("--no-sharpen", action="store_true", help="desativa o filtro de nitidez do ffmpeg")
    p.add_argument("--extra-filters", default="", help="filtros FFmpeg extras (aplicados após o upscale)")
    p.add_argument("--extra-inputs", help='JSON com entradas extras para APIs (ex.: \'{"fps": 60}\')')
    p.add_argument("--realesrgan-bin", help="caminho do executável realesrgan-ncnn-vulkan")
    p.add_argument("--recursive", action="store_true", help="percorre subpastas no modo lote")
    p.add_argument("--ext", default="mp4,mov,mkv,avi,webm,m4v", help="extensões aceitas no modo lote")
    p.add_argument("-n", "--dry-run", action="store_true", help="mostra o plano sem processar")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--preview", action="store_true", help="gera comparação (antes|depois) de um frame")
    p.add_argument("--doctor", action="store_true")
    p.add_argument("--info", metavar="ARQUIVO")
    p.add_argument("--list", action="store_true", help="lista os backends")
    p.add_argument("--gui", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--vmaf", nargs=2, metavar=("UPSCALED", "ORIGINAL"),
                   help="mede a qualidade (VMAF) do vídeo upscalado contra o original")
    p.add_argument("--config", help="caminho de um config.toml alternativo")
    p.add_argument("--version", action="version", version=f"videoupscale {__version__}")
    return p


def build_options(args: argparse.Namespace, cfg: dict) -> core.Options:
    def pick(attr: str, default):
        val = getattr(args, attr, None)
        return default if val is None else val

    return core.Options(
        backend=args.backend or cfg.get("backend", "ffmpeg"),
        model=args.model or cfg.get("model"),
        target=args.target or (None if args.scale else cfg.get("target", "4k")),
        scale=args.scale,
        codec=args.codec or cfg.get("codec", "auto"),
        crf=int(pick("crf", cfg.get("crf", 18))),
        preset=args.preset or cfg.get("preset", "medium"),
        bitrate=args.bitrate,
        pix_fmt=args.pix_fmt or cfg.get("pix_fmt", "auto"),
        workers=int(pick("workers", cfg.get("workers", 0))),
        gpu=args.gpu,
        gpu_id=args.gpu_id,
        device=args.device or cfg.get("device", "auto"),
        fp16=not args.no_fp16,
        tile=args.tile or cfg.get("tile", 0),
        frame_ext=args.frame_ext or cfg.get("frame_ext", "png"),
        tmp_dir=args.tmp_dir or cfg.get("tmp_dir"),
        keep_frames=args.keep_frames,
        start=args.start, end=args.end,
        sharpen=not args.no_sharpen,
        extra_filters=args.extra_filters or "",
        extra_inputs=json.loads(args.extra_inputs) if args.extra_inputs else {},
        realesrgan_bin=args.realesrgan_bin or cfg.get("realesrgan_bin"),
        overwrite=args.overwrite, dry_run=args.dry_run,
    )


def _human(n: float) -> str:
    return f"{n / 2**20:.1f} MB" if n < 2**30 else f"{n / 2**30:.2f} GB"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    if args.gui:
        try:
            from .gui import run
        except ImportError as e:
            print(f"A interface gráfica requer o Tkinter: {e}. "
                  "No Debian/Ubuntu: sudo apt install python3-tk")
            return 1
        return run()
    if args.doctor:
        print_doctor()
        return 0
    if args.list:
        print_backends()
        return 0
    if args.info:
        print_info(args.info)
        return 0
    if args.vmaf:
        try:
            score = core.vmaf(*args.vmaf)
        except Exception as e:
            print(f"Erro: {e}")
            return 1
        print(f"VMAF: {score:.2f}" if score is not None else "VMAF indisponível")
        return 0
    if not args.input:
        build_parser().print_help()
        return 1

    opts = build_options(args, cfg)
    src = Path(args.input)
    verbose = args.verbose and not args.quiet

    if src.is_dir():
        videos = core.find_videos(src, tuple(e.strip() for e in args.ext.split(",") if e.strip()), args.recursive)
        if not videos:
            print(f"Nenhum vídeo encontrado em {src}")
            return 1
        print(f"Modo lote: {len(videos)} vídeo(s) | backend {opts.backend} | alvo {opts.target or f'{opts.scale}x'}")
        results = []
        for i, v in enumerate(videos, 1):
            _BAR_START[0] = time.time()
            print(f"\n[{i}/{len(videos)}] {v.name}")
            try:
                r = core.run_job(str(v), None if not args.out_dir else
                                 str(Path(args.out_dir) / f"{v.stem}_{opts.backend}.mp4"), opts,
                                 progress=None if args.quiet else progress_bar)
                results.append(r)
                print(f"\n  OK {r.src_size[0]}x{r.src_size[1]} -> {r.dst_size[0]}x{r.dst_size[1]} "
                      f"{'(' + r.note + ')' if r.skipped else f'em {r.seconds:.1f}s -> {_human(r.out_bytes)}'}")
            except Exception as e:
                print(f"\n  FALHOU: {e}")
                results.append(None)
        ok = sum(1 for r in results if r and not r.skipped)
        skipped = sum(1 for r in results if r and r.skipped)
        failed = len(videos) - ok - skipped
        print(f"\nConcluídos: {ok} | já existiam: {skipped} | falhas: {failed} | total: {len(videos)}")
        return 0 if ok or skipped else 1

    try:
        info, w, h = core.plan(str(src), opts)
    except Exception as e:
        print(f"Erro: {e}")
        return 1

    if verbose:
        print(f"Entrada : {info.width}x{info.height} @ {info.fps_float:.3f} fps, {info.frames} frames, "
              f"{info.duration:.1f}s, {info.pix_fmt}")
        print(f"Codec   : {core.resolve_codec(opts.codec)} | CRF {opts.crf} | workers {opts.workers or 'auto'}")

    if args.preview:
        out_png = args.output or str(src.with_name(f"{src.stem}_preview_{w}x{h}.png"))
        _BAR_START[0] = time.time()
        core.preview(str(src), opts.backend, w, h, out_png, opts, at=args.start)
        print(f"Preview salvo em {out_png}")
        return 0

    dst = args.output or core.default_output(str(src), w, h, opts.backend)
    print(f"{info.width}x{info.height} -> {w}x{h} | backend: {opts.backend}"
          f"{' modelo: ' + opts.model if opts.model else ''} | saída: {dst}")
    _BAR_START[0] = time.time()
    try:
        r = core.run_job(str(src), dst, opts, progress=None if args.quiet else progress_bar)
    except KeyboardInterrupt:
        print("\nCancelado.")
        return 130
    except Exception as e:
        print(f"\nErro: {e}")
        return 1
    print()
    if r.skipped:
        print(f"Nada a fazer: {r.note}")
        return 0
    speed = info.duration / r.seconds if r.seconds else 0
    print(f"Concluído em {r.seconds:.1f}s ({speed:.2f}x o tempo real) -> {dst} ({_human(r.out_bytes)})")
    return 0
