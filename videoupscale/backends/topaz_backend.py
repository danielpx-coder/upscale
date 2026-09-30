"""Topaz Labs Video API (paga) - modelos profissionais na nuvem (Astra, Proteus, Iris, Rhea, Nyx...).

Requer TOPAZ_API_KEY (https://account.topazlabs.com/manage-api).
Documentação: https://developer.topazlabs.com/getting-started/video-quickstart
Fluxo: criar request -> accept (reserva créditos) -> upload multipart -> complete-upload -> status -> download.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from .. import video_io as vio
from .base import Backend, Capabilities

API = "https://api.topazlabs.com/video"
CHUNK = 25 * 1024 * 1024      # 25 MB por parte
POLL = 15                     # segundos entre consultas de status


class TopazBackend(Backend):
    name = "topaz"
    description = "Pago (API). Topaz Video AI na nuvem - qualidade profissional."
    capabilities = Capabilities(quality=5, speed=2, free=False,
                                models=("prob-4", "iris-3", "rhea-1", "ahq-12", "nyx-3", "slp-4k"),
                                default_model="prob-4",
                                install="export TOPAZ_API_KEY=... (https://account.topazlabs.com/manage-api)",
                                env=("TOPAZ_API_KEY",))

    def available(self):
        if not os.environ.get("TOPAZ_API_KEY"):
            return False, "TOPAZ_API_KEY não definida"
        try:
            import requests  # noqa: F401
        except ImportError:
            return False, "pacote requests não instalado"
        return True, ""

    def upscale(self, src, dst, width, height, progress=None, should_cancel=None):
        self.check()
        import requests

        h = {"X-API-Key": os.environ["TOPAZ_API_KEY"], "accept": "application/json",
             "content-type": "application/json"}
        info = vio.probe(src)
        size = Path(src).stat().st_size
        container = os.path.splitext(src)[1].lstrip(".") or "mp4"
        body = {
            "source": {"container": container, "size": size, "duration": round(info.duration, 3),
                       "frameCount": info.frames, "frameRate": round(info.fps_float, 6),
                       "resolution": {"width": info.width, "height": info.height}},
            "filters": [{"model": self.opts.get("model") or "prob-4"}],
            "output": {"resolution": {"width": width, "height": height},
                       "frameRate": round(info.fps_float, 6), "container": "mp4",
                       "audioTransfer": "Copy", "audioCodec": "AAC",
                       "videoEncoder": "H265", "videoProfile": "Main",
                       "dynamicCompressionLevel": "High"},
        }
        r = requests.post(f"{API}/", json=body, headers=h, timeout=60)
        _raise(r, "criar request")
        data = r.json()
        rid = data.get("requestId")
        est = data.get("estimates") or {}
        print(f"Request {rid} criado. Estimativa: {est}")

        no_ct = {k: v for k, v in h.items() if k != "content-type"}
        r = requests.patch(f"{API}/{rid}/accept", headers=no_ct, timeout=60)
        _raise(r, "aceitar request")
        urls = r.json().get("urls") or []
        if not urls:
            raise RuntimeError(f"Topaz não retornou URLs de upload: {r.text[:300]}")

        parts = math.ceil(size / CHUNK) or 1
        if len(urls) < parts:                     # servidor já decidiu a quantidade de partes
            parts = len(urls)
            chunk = math.ceil(size / parts)
        else:
            chunk = CHUNK
        etags = []
        with open(src, "rb") as f:
            for i, url in enumerate(urls[:parts], 1):
                up = requests.put(url, data=f.read(chunk), headers={"Content-Type": f"video/{container}"}, timeout=1800)
                if up.status_code >= 400:
                    raise RuntimeError(f"Falha no upload da parte {i}: {up.status_code} {up.text[:200]}")
                etag = (up.headers.get("ETag") or up.headers.get("etag") or "").strip('"')
                etags.append({"partNum": i, "eTag": etag})
                print(f"Upload parte {i}/{parts}")

        r = requests.patch(f"{API}/{rid}/complete-upload", json={"uploadResults": etags}, headers=h, timeout=60)
        _raise(r, "confirmar upload")

        while True:
            st = requests.get(f"{API}/{rid}/status", headers=no_ct, timeout=60).json()
            status = (st.get("status") or "").lower()
            pct = st.get("progress")
            if progress and isinstance(pct, (int, float)):
                progress(int(pct), 100)
            if status == "complete":
                url = (st.get("download") or {}).get("url")
                if not url:
                    raise RuntimeError(f"Topaz concluiu sem URL de download: {st}")
                break
            if status in ("failed", "canceled", "error"):  # noqa: SIM102
                raise RuntimeError(f"Topaz retornou status '{status}': {st}")
            if should_cancel and should_cancel():
                raise RuntimeError("Cancelado pelo usuário (o job continua na Topaz e pode ser cobrado)")
            time.sleep(POLL)

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=600) as d, open(dst, "wb") as f:
            d.raise_for_status()
            for piece in d.iter_content(1 << 20):
                f.write(piece)


def _raise(resp, step: str) -> None:
    if resp.status_code >= 400:
        raise RuntimeError(f"Topaz falhou ao {step}: {resp.status_code} {resp.text[:300]}")
