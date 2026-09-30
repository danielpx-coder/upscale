# Changelog

## 0.2.0
- Pipeline paralelo (multiprocessing) compartilhado por todos os backends por frame.
- Novo backend `torch` (PyTorch + spandrel): Real-ESRGAN/ESRGAN local, FP16, MPS/CUDA e tiling para 4K.
- Preservação de áudio, legendas, capítulos e metadados; saída 10-bit automática em fontes HDR.
- `--codec auto` com detecção de NVENC/QuickSync/AMF/Videotoolbox.
- Modo lote (pasta, `--recursive`, `--out-dir`), `--preview` (1 frame), `--vmaf`, `--doctor`, `--info`.
- Corte por tempo (`--start/--end`), `--extra-filters`, `--bitrate`, `--pix-fmt`, `--frame-ext`, `--tmp-dir`.
- Estimativa de espaço em disco antes de extrair frames; cancelamento (CLI e GUI).
- Config em `~/.config/videoupscale/config.toml` + variáveis `VIDEOUPSCALE_*`.
- GUI com fila de arquivos, seleção de modelo por backend, progresso e cancelamento.
- Testes (pytest), lint (ruff) e CI no GitHub Actions; Dockerfile.

## 0.1.0
- Primeira versão: backends ffmpeg, opencv, realesrgan, replicate e topaz.
