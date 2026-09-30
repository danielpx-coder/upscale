# VideoUpscale

Aplicativo Python (CLI + GUI) para **upscale de vídeos até 4K/8K**, com backends gratuitos locais e,
opcionalmente, APIs profissionais de IA. O áudio original é preservado.

| Backend      | Custo  | Qualidade | Requisitos |
|--------------|--------|-----------|------------|
| `ffmpeg`     | Grátis | ★★☆ (Lanczos + nitidez) | FFmpeg |
| `opencv`     | Grátis | ★★★ (IA: FSRCNN/ESPCN/EDSR/LapSRN) | `opencv-contrib-python` (CUDA opcional) |
| `realesrgan` | Grátis | ★★★★ (Real-ESRGAN, GPU Vulkan) | [realesrgan-ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN/releases) |
| `replicate`  | Pago   | ★★★★ (nuvem) | `pip install replicate`, `REPLICATE_API_TOKEN` |
| `topaz`      | Pago   | ★★★★★ (Topaz Video AI: Proteus, Iris, Rhea…) | `TOPAZ_API_KEY` |

## Instalação
```bash
# 1) FFmpeg no PATH (https://ffmpeg.org/download.html)
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # ou: pip install -r requirements.txt
pip install -e .[cloud]     # opcional: Replicate
```

## Uso
```bash
videoupscale --list                                   # lista backends
videoupscale video.mp4 -t 4k                          # FFmpeg -> 3840x2160 (padrão)
videoupscale video.mp4 -b opencv -m fsrcnn -t 1080p   # IA leve (use -m edsr p/ mais qualidade)
videoupscale video.mp4 -b realesrgan -t 4k            # melhor opção grátis (GPU)
videoupscale video.mp4 -b realesrgan -m realesrgan-x4plus -t 4k --codec libx265
videoupscale video.mp4 -s 2 --codec hevc_nvenc        # fator 2x, encoder NVIDIA
videoupscale --gui                                    # interface gráfica (Tkinter)
# também: python -m videoupscale ...
```

### APIs pagas
```bash
export REPLICATE_API_TOKEN=r8_...
videoupscale video.mp4 -b replicate -t 4k             # modelo padrão lucataco/real-esrgan-video

export TOPAZ_API_KEY=...
videoupscale video.mp4 -b topaz -t 4k -m prob-4       # Proteus; outros: iris-3, rhea-1, ahq-12...
```
> Confira preços e nomes de modelos na documentação de cada serviço antes de usar
> (https://replicate.com, https://developer.topazlabs.com) — APIs e modelos podem mudar.

## Dicas para 4K
- A resolução alvo é encaixada mantendo a proporção (ex.: 4:3 → 2880x2160).
- IA com fator x4 + redimensionamento final Lanczos para atingir exatamente o alvo.
- Use `--codec libx265` (arquivos menores) ou `h264_nvenc`/`hevc_nvenc` (rápido em NVIDIA).
- Real-ESRGAN em 4K extrai frames PNG para uma pasta temporária: reserve espaço em disco.
- Modelos OpenCV são baixados em `~/.cache/videoupscale` (altere com `VIDEOUPSCALE_MODELS`).

## Adicionando um backend
Crie uma subclasse de `videoupscale.backends.base.Backend`, implemente `upscale(src, dst, width, height, progress)`
e registre em `videoupscale/backends/__init__.py`.
