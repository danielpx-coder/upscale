"""Configuração em arquivo (~/.config/videoupscale/config.toml) + variáveis de ambiente."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get(
    "VIDEOUPSCALE_CONFIG", Path.home() / ".config" / "videoupscale" / "config.toml"))

ENV_MAP = {                      # chave de config -> variável de ambiente
    "backend": "VIDEOUPSCALE_BACKEND",
    "model": "VIDEOUPSCALE_MODEL",
    "codec": "VIDEOUPSCALE_CODEC",
    "crf": "VIDEOUPSCALE_CRF",
    "preset": "VIDEOUPSCALE_PRESET",
    "workers": "VIDEOUPSCALE_WORKERS",
    "target": "VIDEOUPSCALE_TARGET",
    "tmp_dir": "VIDEOUPSCALE_TMP_DIR",
    "realesrgan_bin": "VIDEOUPSCALE_REALESRGAN_BIN",
}
INT_KEYS = {"crf", "workers"}

SAMPLE = """# ~/.config/videoupscale/config.toml  (todas as chaves são opcionais)
backend = "torch"        # ffmpeg | opencv | torch | realesrgan | replicate | topaz
model = "realesrgan-x4plus"
target = "4k"
codec = "auto"           # auto detecta NVENC/QuickSync/AMF/libx265
crf = 16
preset = "medium"
workers = 4
# tmp_dir = "/mnt/rapido/upscale"
"""


def _parse_simple(text: str) -> dict:
    """Parser mínimo de TOML (chave = valor) para Python < 3.11 sem tomllib."""
    data = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        v = v.strip().strip('"').strip("'")
        data[k] = int(v) if k in INT_KEYS and v.isdigit() else v
    return data


def load(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PATH
    data: dict = {}
    if p.is_file():
        text = p.read_text(encoding="utf-8")
        try:
            import tomllib

            data = tomllib.loads(text)
        except ModuleNotFoundError:
            data = _parse_simple(text)
    for key, env in ENV_MAP.items():
        if os.environ.get(env):
            val = os.environ[env]
            data[key] = int(val) if key in INT_KEYS and val.isdigit() else val
    return data


def write_sample(path: str | Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SAMPLE, encoding="utf-8")
    return p
