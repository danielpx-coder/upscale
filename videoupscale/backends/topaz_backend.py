"""Topaz Labs Video API (paga) - modelos profissionais (Proteus, Iris, Rhea, Artemis...).

Requer TOPAZ_API_KEY (https://www.topazlabs.com/api). Documentação: https://developer.topazlabs.com
Fluxo: criar request -> aceitar (reserva créditos) -> upload multipart -> completar -> poll -> download.
"""
from __future__ import annotations
import math, os, time
from .base import Backend
from ..video_io import probe

API = "https://api.topazlabs.com/video"


class TopazBackend(Backend):
    name = "topaz"
    free = False
    description = "Pago (API). Topaz Video AI na nuvem - qualidade profissional."

    def upscale(self, src, dst, width, height, progress=None):
        import requests
        key = os.environ.get("TOPAZ_API_KEY")
        if not key:
            raise RuntimeError("Defina a variável TOPAZ_API_KEY")
        h = {"X-API-Key": key, "accept": "application/json"}
        info = probe(src)
        size = os.path.getsize(src)
        fps = eval(info.fps) if "/" in info.fps else float(info.fps)  # "30000/1001"
        dur = info.frames / fps if info.frames else 0
        body = {
            "source": {"container": os.path.splitext(src)[1].lstrip(".") or "mp4", "size": size,
                       "duration": dur, "frameCount": info.frames, "frameRate": fps,
                       "resolution": {"width": info.width, "height": info.height}},
            "filters": [{"model": self.opts.get("model") or "prob-4"}],
            "output": {"resolution": {"width": width, "height": height}, "frameRate": fps,
                       "audioTransfer": "Copy", "audioCodec": "AAC", "videoEncoder": "H265",
                       "videoProfile": "Main", "dynamicCompressionLevel": "High", "container": "mp4"},
        }
        r = requests.post(f"{API}/", json=body, headers=h); r.raise_for_status()
        rid = r.json()["requestId"]
        print(f"Request {rid} criado. Estimativa: {r.json().get('estimates', {})}")
        r = requests.patch(f"{API}/{rid}/accept", headers=h); r.raise_for_status()
        urls = r.json()["urls"]
        part = math.ceil(size / len(urls))
        etags = []
        with open(src, "rb") as f:
            for i, url in enumerate(urls, 1):
                up = requests.put(url, data=f.read(part)); up.raise_for_status()
                etags.append({"partNum": i, "eTag": up.headers.get("ETag", "").strip('"')})
                print(f"Upload parte {i}/{len(urls)}")
        r = requests.patch(f"{API}/{rid}/complete-upload", json={"uploadResults": etags}, headers=h)
        r.raise_for_status()
        while True:
            st = requests.get(f"{API}/{rid}/status", headers=h).json()
            s = st.get("status")
            if progress and st.get("progress") is not None:
                progress(int(st["progress"]), 100)
            if s == "complete":
                url = st["download"]["url"]; break
            if s in ("failed", "canceled"):
                raise RuntimeError(f"Topaz: {st}")
            time.sleep(10)
        with requests.get(url, stream=True) as d, open(dst, "wb") as f:
            d.raise_for_status()
            for chunk in d.iter_content(1 << 20):
                f.write(chunk)
