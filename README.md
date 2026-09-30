# VideoUpscale

Aplicativo Python (CLI + GUI) para **upscale de vídeos até 4K/8K**, com backends locais gratuitos e
APIs profissionais de IA. Áudio, legendas, capítulos, metadados e (quando aplicável) 10-bit/HDR são preservados.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .                                       # backends leves (ffmpeg, opencv, realesrgan, APIs)
pip install -e .[torch]                                # + backend Real-ESRGAN local (PyTorch)
videoupscale --doctor                                  # confere FFmpeg, encoders e GPUs
```

> Requer o **FFmpeg** no PATH (https://ffmpeg.org/download.html). O `ffprobe` é opcional
> (sem ele, os metadados são lidos via OpenCV).

## Backends

| Backend | Custo | Qualidade | Velocidade | Uso recomendado |
|---|---|---|---|---|
| `ffmpeg` | grátis | ★★☆☆☆ | ★★★★★ | aumentar rápido, converter/remuxar, lotes grandes |
| `opencv` | grátis | ★★★☆☆ | ★★★☆☆ | máquinas fracas/sem GPU (FSRCNN/ESPCN) |
| `torch` | grátis | ★★★★★ | ★★☆☆☆ | **melhor qualidade local** (Real-ESRGAN/ESRGAN via spandrel) |
| `realesrgan` | grátis | ★★★★☆ | ★★★☆☆ | anime/animação e vídeos reais em GPU AMD/NVIDIA/Intel (Vulkan) |
| `replicate` | pago | ★★★★☆ | ★★★☆☆ | sem GPU local; paga por segundo processado |
| `topaz` | pago | ★★★★★ | ★★☆☆☆ | acabamento profissional na nuvem (Astra, Proteus, Iris, Rhea, Nyx) |

```bash
videoupscale video.mp4 -t 4k                                       # ffmpeg (padrão), 3840x2160
videoupscale video.mp4 -b opencv -m fsrcnn -t 1080p --workers 8    # IA leve em CPU, paralelo
videoupscale video.mp4 -b torch -m realesrgan-x4plus -t 4k --tile 512
videoupscale video.mp4 -b realesrgan -m realesr-animevideov3 -t 4k
videoupscale video.mp4 -b replicate -t 4k                          # REPLICATE_API_TOKEN
videoupscale video.mp4 -b topaz -m prob-4 -t 4k                    # TOPAZ_API_KEY
videoupscale --gui
```

Alvos: `480p, 720p, 1080p, 1440p, 2k, 4k, 8k` ou `LARGURAxALTURA` (`3840x1600`).
A proporção é sempre preservada (um 4:3 vai para 2880x2160 num alvo "4k").

## Recursos "profissionais"

- **Pipeline paralelo**: os frames são extraídos, distribuídos entre processos (`--workers`) e
  remontados — aproveita todos os núcleos.
- **Tiling automático** no backend `torch` (`--tile`): upscale 4K sem estourar a VRAM; meia precisão (`--fp16`).
- **Detecção de encoder**: `--codec auto` escolhe NVENC/QuickSync/AMF/libx265 quando disponível.
- **Preservação de tudo**: áudio, legendas, capítulos e metadados do original (`-c:a copy`, `-c:s copy`).
- **10-bit/HDR**: saída em `yuv420p10le` automaticamente quando a fonte é HDR (`--pix-fmt` para forçar).
- **Pré/pós-processamento**: `--extra-filters` aceita qualquer filtro FFmpeg
  (ex.: desentrelaçar `bwdif=mode=send_field`, reduzir ruído `hqdn3d=2:1:3:3`, nitidez extra).
- **Cortes**: `--start 00:01:30 --end 00:02:00` processa só um trecho (ótimo para testar).
- **Preview**: `--preview` gera um PNG lado a lado (antes | depois) de um único frame, antes de gastar horas.
- **Modo lote**: passe uma pasta (`--recursive`, `--ext`, `--out-dir`); arquivos já existentes são pulados
  (use `--overwrite`).
- **Medição objetiva**: `--vmaf upscalado.mp4 original.mp4` calcula o VMAF (requer FFmpeg com libvmaf).
- **Cancelamento**: Ctrl-C (CLI) ou botão Cancelar (GUI) interrompem sem deixar lixo.
- **Config em arquivo**: `~/.config/videoupscale/config.toml` (ou `--config`) e variáveis
  `VIDEOUPSCALE_*` definem padrões.
- **Diagnóstico**: `--doctor` mostra FFmpeg, encoders de hardware, GPU, backends prontos e chaves de API.

## Desempenho e qualidade (4K)

| Ajuste | Quando usar |
|---|---|
| `--workers N` | CPU: use o nº de núcleos. GPU: mantenha 1 (a GPU já paraleliza). |
| `--tile 512` | falta de VRAM/CUDA OOM no backend `torch` |
| `--frame-ext jpg` | disco lento/pequeno (frames JPEG são bem menores, com leve perda) |
| `--tmp-dir /mnt/ssd` | colocar os frames temporários num SSD rápido |
| `--codec libx265 --crf 20` | arquivos menores em 4K (codificação mais lenta) |
| `--codec hevc_nvenc` | NVIDIA: codificação muito mais rápida |
| `--bitrate 40M` | quando o objetivo é um bitrate-alvo (streaming), em vez de CRF |
| `--pix-fmt yuv420p10le` | preservar HDR10/HLG |

**Espaço em disco**: os frames temporários podem chegar a dezenas de GB em 4K
(estimativa: `frames x 3 x largura x altura`). O programa aborta com uma mensagem clara se faltar espaço;
use `--tmp-dir` ou `--frame-ext jpg` nesse caso.

## Receitas úteis

```bash
# VHS/DVD entrelaçado: desentrelaça antes do upscale
videoupscale antigo.mp4 -b torch -t 1080p --extra-filters "bwdif=mode=send_field,hqdn3d=2:1:3:3"

# Anime em GPU fraca (Vulkan)
videoupscale abertura.mkv -b realesrgan -m realesr-animevideov3 -t 1080p --frame-ext jpg

# Comparar backends em 1 frame antes de decidir
videoupscale --preview video.mp4 -b torch -t 4k
videoupscale --preview video.mp4 -b opencv -m edsr -t 4k

# Lote inteiro em 4K, pulando o que já existe
videoupscale ./originais --recursive -t 4k -b torch --out-dir ./4k --workers 1

# Qualidade medida
videoupscale --vmaf ./4k/video_torch.mp4 ./originais/video.mp4
```

## APIs pagas

```bash
export REPLICATE_API_TOKEN=r8_...     # https://replicate.com/account/api-tokens
videoupscale video.mp4 -b replicate -t 4k

export TOPAZ_API_KEY=...              # https://account.topazlabs.com/manage-api
videoupscale video.mp4 -b topaz -m prob-4 -t 4k
```

Modelos Topaz usados por padrão: `prob-4` (Proteus). Outros citados na documentação:
`iris-3`, `rhea-1`, `ahq-12`, `nyx-3`, `slp-4k`. O fluxo implementado é o oficial
(criar → accept → upload multipart → complete-upload → status → download);
o upload é fatiado em partes de 25 MB.

> **Atenção:** essas integrações foram escritas com base na documentação pública
> ([Replicate](https://replicate.com/lucataco/real-esrgan-video),
> [Topaz](https://developer.topazlabs.com/getting-started/video-quickstart)) e **não foram executadas**
> (não há chaves de API neste ambiente). Confira preços, nomes de modelos e limites antes de usar:
> cada chamada consome créditos reais. Use `--dry-run`/`--preview` (backends locais) para validar o plano.

## Arquitetura

```
videoupscale/
  cli.py         CLI (argumentos, lote, preview, doctor, vmaf)
  gui.py         interface Tkinter (fila, progresso, cancelar)
  core.py        núcleo compartilhado (Jobs, Options, preview, doctor, vmaf)
  pipeline.py    extrai frames -> upscale em paralelo -> remonta o vídeo
  video_io.py    probe, codecs, pixel formats, extração/remontagem
  config.py      config.toml + variáveis de ambiente
  backends/      ffmpeg, opencv, torch, realesrgan, replicate, topaz
```

Para adicionar um backend: subclassifique `videoupscale.backends.base.Backend`
(implemente `upscale()` e, opcionalmente, `available()`), preencha `Capabilities` e registre em
`videoupscale/backends/__init__.py`. Se o modelo for por frame, reaproveite `pipeline.run()` passando
uma *factory* serializável (o modelo é carregado dentro de cada processo).

## Desenvolvimento

```bash
pip install -r requirements-dev.txt
pytest -q        # 11 testes (não exigem rede nem GPU)
ruff check .     # lint
```

O CI (`.github/workflows/ci.yml`) roda lint + testes no Ubuntu com FFmpeg.

## Docker

```bash
docker build -t videoupscale .
docker run --rm -v "$PWD:/data" --gpus all videoupscale /data/video.mp4 -t 4k -b ffmpeg
```

## Limitações conhecidas

- Vídeos com **taxa de quadros variável (VFR)** são tratados com o FPS nominal (o FFmpeg avisa com `[VFR]` em `--info`).
- O backend `torch` exige `spandrel` para carregar modelos automaticamente.
- Cancelar um job das APIs pagas não cancela o processamento remoto (ele pode ser cobrado).
- Sem modelos de consistência temporal: para cenas com muito movimento, avalie `--extra-filters`
  (estabilização/denoise) ou uma solução dedicada de VSR.
