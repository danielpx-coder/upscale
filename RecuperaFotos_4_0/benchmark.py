# -*- coding: utf-8 -*-
"""
benchmark.py — medição REAL de desempenho e de acerto em uma imagem de 2 GiB.

O que este script faz (nada é estimado, tudo é medido nesta execução):

1. Constrói em /tmp (fora do repositório, para não inflar o Git) uma imagem de
   cartão de 2 GiB com gabarito conhecido:
       setor 0            MBR com 1 partição FAT32 (LBA 2048, 512 MiB)
       1 MiB .. 513 MiB   volume FAT32 real (clusters de 32 KiB) com fotos
                          VIVAS em DCIM/100CANON + fotos APAGADAS (a última
                          fragmentada)
       513 MiB .. 2 GiB   "espaço não alocado": dados aleatórios com TODAS as
                          assinaturas de imagem neutralizadas, e 50 JPEGs
                          ÓRFÃOS encaixados em offsets conhecidos (restos de
                          uma partição antiga, só a etapa 2 os alcança)
2. Mede a leitura bruta da imagem inteira (teto de I/O do ambiente).
3. Roda 3 cenários cronometrando etapa 1 e etapa 2 separadamente:
       A) padrão da interface gráfica (só o espaço livre do volume)
       B) cartão inteiro com verificação       -> varre os 2 GiB
       C) cartão inteiro sem verificação       -> isola o custo da verificação
4. Confere cada arquivo gravado contra o gabarito por SHA-256: recall,
   arquivos byte a byte idênticos, fora do gabarito, duplicados e MB/s.
5. Grava benchmark_saida/relatorio_benchmark.md, medidas.json, gabarito.json e
   cópias dos relatórios CSV.

Uso:  python benchmark.py            constrói a imagem de 2 GiB e roda tudo
      python benchmark.py --reusar   reaproveita a imagem já construída
      python benchmark.py --rapido   imagem de 512 MiB (conferir o script)
"""

from __future__ import annotations

import gc
import hashlib
import resource
import io
import json
import os
import platform
import random
import shutil
import sys
import time
from datetime import datetime

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "tests"))

import motor                                     # noqa: E402
from aquisicao import LeitorArquivo              # noqa: E402
import construtor                                # noqa: E402

SAIDA = os.path.join(RAIZ, "benchmark_saida")
TRABALHO = "/tmp/rf4_bench"          # fora do repositório: não vai para o Git
IMAGEM = os.path.join(TRABALHO, "cartao_benchmark.img")

TAM_SETOR = 512
LBA_PARTICAO = 2048                              # partição começa em 1 MiB
SPC = 64                                         # clusters de 32 KiB
BLOCO = 8 * 1024 * 1024                          # escrita/leitura em 8 MiB

# gabarito do modo completo: volume de 512 MiB + ~1,5 GiB não alocados
CONFIG = {
    "completo": {"tamanho": 2 * 1024 ** 3, "setores_volume": 1048576,
                 "vivas": 45, "apagadas": 30, "orfas": 50},
    "rapido":   {"tamanho": 512 * 1024 * 1024, "setores_volume": 524288,
                 "vivas": 8, "apagadas": 6, "orfas": 8},
}
LARGURA, ALTURA = 640, 480


def _mi(n) -> str:
    return f"{n / (1024 * 1024):.1f} MiB"


def _mbps(nbytes, segundos) -> float:
    return (nbytes / (1024 * 1024)) / segundos if segundos > 0 else 0.0


def sha(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def foto(semente: int, largura=LARGURA, altura=ALTURA, qualidade=88) -> bytes:
    """JPEG real (decodificável pelo Pillow), com tamanho de foto de verdade.

    Ruído + gradiente vertical: o ruído dá entropia alta (arquivo grande, com
    marcadores RST), o gradiente impede a sopa uniforme e torna o tamanho
    parecido com o de uma fotografia real.
    """
    from PIL import Image
    rnd = random.Random(semente)
    brutos = bytearray(rnd.randbytes(largura * altura * 3))
    for y in range(altura):
        base = (y * 255) // max(1, altura - 1) // 2
        i = y * largura * 3
        for x in range(0, largura * 3, 3):
            brutos[i + x] = (brutos[i + x] >> 1) + base
    img = Image.frombytes("RGB", (largura, altura), bytes(brutos))
    bio = io.BytesIO()
    img.save(bio, "JPEG", quality=qualidade, restart_marker_rows=4)
    return bio.getvalue()


def neutralizar_assinaturas(bloco: bytes) -> bytes:
    """Apaga as marcas de imagem que o acaso criar nos dados aleatórios.

    Em 1,5 GiB aleatórios surgem ~90 sequências FFD8FF e ~23 mil "BM" por puro
    acaso. FFD8FF é neutralizada porque cada ocorrência dispara o analisador
    JPEG completo (custo real de varredura); as demais são apagadas para o
    gabarito não ser contaminado por achados do acaso. "BM" permanece: é uma
    assinatura de 2 bytes que existe em cartões de verdade, e o seu custo está
    sendo medido justamente aqui.
    """
    bloco = bloco.replace(b"\xff\xd8\xff", b"\xff\xd8\x00")
    bloco = bloco.replace(b"\xff\xd9\xff\xd8", b"\xff\xd9\x00\xd8")
    for marca in (b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"II*\x00",
                  b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00", b"FUJIFILMCCD-RAW",
                  b"8BPS", b"RIFF", b"\x1a\x45\xdf\xa3", b"ftyp"):
        bloco = bloco.replace(marca, b"\x00" * len(marca))
    return bloco


# ---------------------------------------------------------------------------
# 1. construção da imagem com gabarito
# ---------------------------------------------------------------------------

def construir_imagem(cfg: dict, caminho: str) -> dict:
    tamanho_total = cfg["tamanho"]
    setores = tamanho_total // TAM_SETOR
    setores_volume = cfg["setores_volume"]
    assert setores_volume % SPC == 0
    clusters = (setores_volume - 32 - 128) // SPC
    assert clusters >= 4085, f"clusters={clusters} seria lido como FAT16"

    q_vivas, q_apag, q_orf = cfg["vivas"], cfg["apagadas"], cfg["orfas"]
    print(f"[1/5] gerando {q_vivas} vivas + {q_apag} apagadas + {q_orf} órfãs "
          f"({LARGURA}x{ALTURA})...")
    t0 = time.time()
    vivas = [foto(1000 + i) for i in range(q_vivas)]
    apagadas = [foto(2000 + i, qualidade=85) for i in range(q_apag)]
    orfas = [foto(3000 + i, qualidade=90) for i in range(q_orf)]
    media = sum(map(len, vivas + apagadas + orfas)) // (q_vivas + q_apag + q_orf)
    print(f"      fotos prontas em {time.time() - t0:.1f}s (média {media} B)")

    arquivos, gab_apagadas, gab_vivas = [], [], []
    for i, dados in enumerate(vivas):
        nome = f"IMG_{1000 + i:04d}.JPG"
        arquivos.append(construtor.ArquivoTeste(
            nome, dados, apagado=False,
            data=(2024, 6, 15, 10, i % 60, 20), subpasta="DCIM/100CANON"))
        gab_vivas.append({"nome": nome, "tamanho": len(dados), "sha256": sha(dados)})
    for i, dados in enumerate(apagadas):
        nome = f"IMG_{2000 + i:04d}.JPG"
        frag = (i == q_apag - 1)           # a última vai fragmentada
        arquivos.append(construtor.ArquivoTeste(
            nome, dados, apagado=True, fragmentado=frag,
            data=(2023, 11, i % 28 + 1, 18, 4, 0), subpasta="DCIM/100CANON"))
        gab_apagadas.append({"nome": nome, "tamanho": len(dados),
                             "sha256": sha(dados), "fragmentado": frag})
    del vivas, apagadas
    gc.collect()

    print(f"[2/5] montando o volume FAT32 de {_mi(setores_volume * TAM_SETOR)} "
          f"({clusters} clusters de {SPC * TAM_SETOR // 1024} KiB)...")
    t0 = time.time()
    volume = construtor.construir_fat32(arquivos, tam_setores=setores_volume,
                                        spc=SPC, tam_setor=TAM_SETOR)
    print(f"      volume pronto em {time.time() - t0:.1f}s")
    del arquivos
    gc.collect()
    mbr = construtor.construir_mbr([(LBA_PARTICAO, setores_volume, 0x0C)],
                                   TAM_SETOR, setores)

    inicio_livre = (LBA_PARTICAO + setores_volume) * TAM_SETOR
    maior = max(len(o) for o in orfas)
    fim_livre = tamanho_total - maior - TAM_SETOR
    passo = (fim_livre - inicio_livre) // q_orf
    assert passo > maior, "as órfãs não cabem sem se sobrepor"
    rnd = random.Random(777)
    # offsets sorteados, sempre múltiplos de 512 e sempre dentro de um vão
    # (passo > maior arquivo) para duas órfãs nunca se sobreporem
    posicoes = [inicio_livre + i * passo
                + rnd.randrange(0, (passo - maior) // TAM_SETOR) * TAM_SETOR
                for i in range(q_orf)]
    assert all(inicio_livre <= p < fim_livre for p in posicoes), posicoes[:3]
    assert len(set(posicoes)) == q_orf

    print(f"[3/5] escrevendo a imagem de {_mi(tamanho_total)} em {caminho}...")
    t0 = time.time()
    with open(caminho, "wb", buffering=0) as f:
        f.write(mbr)
        f.write(b"\x00" * (LBA_PARTICAO * TAM_SETOR - len(mbr)))
        f.write(volume)
        escrito = inicio_livre
        while escrito < tamanho_total:
            n = min(BLOCO, tamanho_total - escrito)
            f.write(neutralizar_assinaturas(os.urandom(n)))
            escrito += n
    del volume
    gc.collect()
    # segundo passo: encaixa as órfãs nos offsets sorteados do gabarito
    with open(caminho, "r+b", buffering=0) as f:
        gab_orfas = []
        for off, dados in zip(posicoes, orfas):
            f.seek(off)
            f.write(dados)
            gab_orfas.append({"offset": off, "tamanho": len(dados),
                              "sha256": sha(dados)})
    dur_escrita = time.time() - t0
    real = os.path.getsize(caminho)
    assert real == tamanho_total, (real, tamanho_total)
    print(f"      imagem escrita em {dur_escrita:.1f}s "
          f"({_mbps(tamanho_total, dur_escrita):.0f} MB/s de gravação)")
    del orfas
    gc.collect()

    return {
        "imagem": caminho,
        "tamanho_bytes": tamanho_total,
        "tamanho_setores": setores,
        "volume": {"lba_inicio": LBA_PARTICAO, "setores": setores_volume,
                   "offset": LBA_PARTICAO * TAM_SETOR,
                   "bytes": setores_volume * TAM_SETOR, "spc": SPC,
                   "cluster_bytes": SPC * TAM_SETOR, "clusters": clusters},
        "area_nao_alocada": {"inicio": inicio_livre, "fim": tamanho_total,
                             "bytes": tamanho_total - inicio_livre},
        "fotos_vivas": gab_vivas,
        "fotos_apagadas": gab_apagadas,
        "fotos_orfas": gab_orfas,
        "segundos_para_construir": dur_escrita,
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "python": sys.version.split()[0],
        "plataforma": f"{platform.system()} {platform.release()} "
                      f"({platform.machine()})",
    }


def medir_espaco_livre(gab: dict) -> dict:
    """Conta os clusters livres lendo a FAT gravada na imagem (não é estimado).

    O cenário A varre exatamente esses clusters, então é esse o número de bytes
    que deve aparecer como "varrido" — e não o tamanho inteiro do volume.
    """
    v = gab["volume"]
    reservados = 32                     # setores reservados do boot FAT32
    with open(gab["imagem"], "rb") as f:
        f.seek(v["offset"] + reservados * TAM_SETOR)
        fat = f.read(16384 * 4)         # fat_setores = 64 -> 16384 entradas
    livres = sum(1 for c in range(2, v["clusters"] + 2)
                 if fat[c * 4:c * 4 + 4] == b"\x00\x00\x00\x00")
    return {"clusters_livres": livres,
            "espaco_livre_bytes": livres * v["cluster_bytes"]}


# ---------------------------------------------------------------------------
# 2. leitura bruta (teto de I/O)
# ---------------------------------------------------------------------------

def medir_leitura_bruta(caminho: str, bloco: int = 4 * 1024 * 1024) -> dict:
    tamanho = os.path.getsize(caminho)
    leitor = LeitorArquivo(caminho)
    lidos, off = 0, 0
    t0 = time.time()
    while off < tamanho:
        dados = leitor.read(off, min(bloco, tamanho - off))
        if not dados:
            break
        lidos += len(dados)
        off += len(dados)
    dur = time.time() - t0
    erros = leitor.errores_leitura
    leitor.fechar()
    return {"bytes_lidos": lidos, "segundos": dur, "mbps": _mbps(lidos, dur),
            "bloco": bloco, "erros_leitura": erros}


# ---------------------------------------------------------------------------
# 3. cenários de recuperação
# ---------------------------------------------------------------------------

def rodar_cenario(nome: str, gab: dict, destino: str, **opcoes) -> dict:
    if os.path.isdir(destino):
        shutil.rmtree(destino, ignore_errors=True)
    os.makedirs(destino, exist_ok=True)
    op = motor.Opcoes(origem=gab["imagem"], destino=destino,
                      permitir_mesmo_disco=True, **opcoes)

    marcos = []

    def log(msg):
        marcos.append((time.time(), msg))

    fases = []

    def progresso(fracao, total, fase):
        if not fases or fases[-1][2] != fase:
            fases.append((time.time(), fracao, fase))

    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.time()
    res = motor.Motor(op, progresso=progresso, log=log).executar()
    dur = time.time() - t0
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)

    t_etapa2 = next((t for t, msg in marcos if msg.startswith("Etapa 2:")), None)
    dur_e1 = (t_etapa2 - t0) if t_etapa2 else dur
    dur_e2 = (dur - dur_e1) if t_etapa2 else 0.0

    # ---- conferência byte a byte contra o gabarito
    alvo = ({g["sha256"] for g in gab["fotos_apagadas"]}
            | {g["sha256"] for g in gab["fotos_orfas"]})
    vivos = {g["sha256"] for g in gab["fotos_vivas"]}
    gravados, relatorios = [], []
    for raiz, _dirs, arqs in os.walk(destino):
        for a in sorted(arqs):
            p = os.path.join(raiz, a)
            with open(p, "rb") as f:
                d = f.read()
            item = {"arquivo": os.path.relpath(p, destino).replace(os.sep, "/"),
                    "tamanho": len(d), "sha256": sha(d)}
            # os CSV que o próprio programa gera não são recuperação
            (relatorios if a.lower().endswith(".csv") else gravados).append(item)
    certos = [g for g in gravados if g["sha256"] in alvo]
    vivos_achados = [g for g in gravados if g["sha256"] in vivos]
    fora = [g for g in gravados
            if g["sha256"] not in alvo and g["sha256"] not in vivos]
    distintos = {g["sha256"] for g in certos}
    apag_ok = {g["sha256"] for g in gab["fotos_apagadas"]} & distintos
    orf_ok = {g["sha256"] for g in gab["fotos_orfas"]} & distintos
    vivos_ok = {g["sha256"] for g in vivos_achados}

    if op.cartao_inteiro:
        varridos, varrido_txt = gab["tamanho_bytes"], "o cartão inteiro"
    elif op.apenas_espaco_livre and gab["volume"].get("espaco_livre_bytes"):
        varridos = gab["volume"]["espaco_livre_bytes"]
        varrido_txt = "só os clusters livres do volume"
    else:
        varridos, varrido_txt = gab["volume"]["bytes"], "o volume inteiro"
    return {
        "nome": nome,
        "opcoes": dict(opcoes),
        "cpu_usuario_s": round(cpu1.ru_utime - cpu0.ru_utime, 3),
        "cpu_sistema_s": round(cpu1.ru_stime - cpu0.ru_stime, 3),
        "rss_pico_mb": round(cpu1.ru_maxrss / 1024, 1),
        "segundos": dur, "segundos_etapa1": dur_e1, "segundos_etapa2": dur_e2,
        "bytes_varridos": varridos, "varrido_txt": varrido_txt,
        "mbps_efetivo": _mbps(varridos, dur),
        "recuperados": res.recuperados,
        "candidatos": len(res.candidatos),
        "rejeitados": res.rejeitados,
        "recusados_ntfs": len(res.recusas_ntfs),
        "volumes": [str(v) for v in res.volumes],
        "bytes_recuperados": res.bytes_recuperados,
        "por_estado": dict(res.por_estado), "por_metodo": dict(res.por_metodo),
        "duplicados_hash": res.duplicados_hash,
        "duplicados_regiao": res.duplicados_regiao,
        "erros_leitura": res.erros_leitura, "sistemas": list(res.sistemas),
        "avisos": list(res.avisos), "cancelado": res.cancelado,
        "pausado": res.pausado, "tempo_interno": res.tempo_segundos,
        "fases_observadas": [(round(t - t0, 3), f, fr) for t, fr, f in fases],
        "gravados_no_disco": len(gravados),
        "relatorios_csv_no_destino": len(relatorios),
        "bytes_gravados": sum(g["tamanho"] for g in gravados),
        "identicos_ao_gabarito": len(certos),
        "distintos_do_gabarito": len(distintos),
        "vivos_reencontrados": len(vivos_achados),
        "vivos_distintos": len(vivos_ok),
        "fora_do_gabarito": len(fora),
        "amostra_fora_do_gabarito": fora[:6],
        "recall_apagadas": f"{len(apag_ok)}/{len(gab['fotos_apagadas'])}",
        "recall_orfas": f"{len(orf_ok)}/{len(gab['fotos_orfas'])}",
        "relatorio_csv": res.relatorio_csv,
        "marcos": [(round(t - t0, 3), msg) for t, msg in marcos],
    }


# ---------------------------------------------------------------------------
# 4. relatório
# ---------------------------------------------------------------------------

def _tabela(cenarios: list[dict]) -> str:
    linhas = [
        "| cenário | varrido | tempo total | etapa 1 | etapa 2 | MB/s | "
        "gravados | idênticos ao alvo | vivos reencontrados | fora do gabarito "
        "| apagadas | órfãs |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cenarios:
        linhas.append(
            f"| {c['nome']} | {_mi(c['bytes_varridos'])} | {c['segundos']:.1f}s | "
            f"{c['segundos_etapa1']:.1f}s | {c['segundos_etapa2']:.1f}s | "
            f"{c['mbps_efetivo']:.0f} | {c['gravados_no_disco']} | "
            f"{c['identicos_ao_gabarito']} | {c['vivos_reencontrados']} | "
            f"{c['fora_do_gabarito']} | "
            f"{c['recall_apagadas']} | {c['recall_orfas']} |")
    return "\n".join(linhas)


def escrever_relatorio(gab: dict, bruta: dict, cenarios: list[dict],
                       argv: list[str]) -> str:
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    tam = gab["tamanho_bytes"]
    s = io.StringIO()
    w = s.write
    w(f"# Recupera Fotos 4.0 — benchmark real em imagem de {_mi(tam)}\n\n")
    w(f"*Gerado por `python benchmark.py{' '.join(argv[1:])}` em {agora}. "
      "Todos os números foram medidos nesta execução; nenhum é estimado.*\n\n")
    w("## Ambiente\n\n")
    w(f"- sistema: {gab['plataforma']} | cpus: {os.cpu_count()}\n")
    w(f"- python: {gab['python']}\n")
    try:
        import PIL
        w(f"- Pillow: {PIL.__version__}\n")
    except ImportError:
        w("- Pillow: ausente\n")
    w(f"- imagem: `{gab['imagem']}` — {tam} bytes "
      f"({tam / 1024**3:.3f} GiB), arquivo real em disco, fora do repositório\n")
    w(f"- construção da imagem: {gab['segundos_para_construir']:.1f}s\n")
    v = gab["volume"]
    w(f"- volume FAT32: LBA {v['lba_inicio']} ({v['offset']} B), "
      f"{_mi(v['bytes'])}, {v['clusters']} clusters de "
      f"{v['cluster_bytes'] // 1024} KiB\n")
    if gab["volume"].get("espaco_livre_bytes") is not None:
        w(f"- espaço livre dentro do volume (lendo a FAT): "
          f"{gab['volume']['clusters_livres']} clusters = "
          f"{_mi(gab['volume']['espaco_livre_bytes'])} — é isso o que o "
          "cenário A varre\n")
    a = gab["area_nao_alocada"]
    w(f"- área não alocada: {a['inicio']} .. {a['fim']} ({_mi(a['bytes'])}) de "
      "dados aleatórios com as assinaturas de imagem neutralizadas\n\n")
    w("## Gabarito (o que existe na imagem, conferido por SHA-256)\n\n")
    w(f"- {len(gab['fotos_vivas'])} fotos vivas no FAT32 (não devem ser "
      "recuperadas pela etapa 1; o carving de cartão inteiro as reencontra, "
      "porque elas ainda estão lá)\n")
    w(f"- {len(gab['fotos_apagadas'])} fotos apagadas no FAT32 "
      f"({sum(1 for g in gab['fotos_apagadas'] if g['fragmentado'])} "
      f"fragmentada), {_mi(sum(g['tamanho'] for g in gab['fotos_apagadas']))}\n")
    w(f"- {len(gab['fotos_orfas'])} fotos órfãs na área não alocada, "
      f"{_mi(sum(g['tamanho'] for g in gab['fotos_orfas']))} — só a etapa 2 "
      "(carving) as alcança\n")
    w(f"- tamanho médio de uma foto: "
      f"{sum(g['tamanho'] for g in gab['fotos_orfas']) // max(1, len(gab['fotos_orfas']))} B\n\n")
    w("## Leitura bruta (teto de I/O do ambiente)\n\n")
    w(f"- {bruta['bytes_lidos']} bytes lidos em {bruta['segundos']:.1f}s = "
      f"**{bruta['mbps']:.0f} MB/s** (blocos de "
      f"{bruta['bloco'] // (1024*1024)} MiB, erros de leitura: "
      f"{bruta['erros_leitura']})\n")
    w("- atenção: a imagem acabou de ser gravada, então o Linux a serve do "
      "cache de página (o sandbox não permite descartar o cache com "
      "`echo 3 > /proc/sys/vm/drop_caches`). Este número é o teto de "
      "leitura+CPU do ambiente, não a velocidade de um cartão físico.\n\n")
    w("## Resultados\n\n")
    w(_tabela(cenarios) + "\n\n")
    for c in cenarios:
        w(f"### {c['nome']}\n\n")
        w(f"- opções: `{json.dumps(c['opcoes'], ensure_ascii=False)}`\n")
        w(f"- o que foi varrido: {c['varrido_txt']} ({_mi(c['bytes_varridos'])})\n")
        w(f"- tempo: {c['segundos']:.1f}s total "
          f"(etapa 1 {c['segundos_etapa1']:.1f}s + etapa 2 "
          f"{c['segundos_etapa2']:.1f}s) | {_mi(c['bytes_varridos'])} varridos | "
          f"{c['mbps_efetivo']:.0f} MB/s\n")
        w(f"- cpu: {c['cpu_usuario_s']:.1f}s usuário + {c['cpu_sistema_s']:.1f}s "
          f"sistema | pico de memória do processo (RSS): {c['rss_pico_mb']:.0f} MB\n")
        w(f"- recuperados: {c['recuperados']} | candidatos em memória: "
          f"{c['candidatos']} | rejeitados: {c['rejeitados']} | "
          f"recusas NTFS: {c['recusados_ntfs']}\n")
        w(f"- gravados no destino: {c['gravados_no_disco']} arquivo(s), "
          f"{_mi(c['bytes_gravados'])} | volumes: `{c['volumes']}`\n")
        w(f"- por estado: `{json.dumps(c['por_estado'], ensure_ascii=False)}`\n")
        w(f"- por método: `{json.dumps(c['por_metodo'], ensure_ascii=False)}`\n")
        w(f"- duplicados descartados: {c['duplicados_hash']} por hash, "
          f"{c['duplicados_regiao']} por região sobreposta\n")
        w(f"- erros de leitura: {c['erros_leitura']} | sistemas detectados: "
          f"`{c['sistemas']}` | cancelado: {c['cancelado']}\n")
        w(f"- conferência SHA-256: {c['identicos_ao_gabarito']} gravado(s) "
          f"idêntico(s) ao alvo ({c['distintos_do_gabarito']} foto(s) "
          f"distinta(s)); {c['vivos_reencontrados']} foto(s) viva(s) "
          f"reencontrada(s) pelo carving ({c['vivos_distintos']} distinta(s)); "
          f"{c['fora_do_gabarito']} fora do gabarito\n")
        w(f"- relatórios CSV no destino (não contam como recuperação): "
          f"{c['relatorios_csv_no_destino']}\n")
        w(f"- recall: apagadas {c['recall_apagadas']} | órfãs "
          f"{c['recall_orfas']}\n")
        if c["avisos"]:
            w("- avisos: " + " | ".join(c["avisos"]) + "\n")
        if c["amostra_fora_do_gabarito"]:
            w("- amostra fora do gabarito: " +
              ", ".join(f"`{a2['arquivo']}` {a2['tamanho']} B"
                        for a2 in c["amostra_fora_do_gabarito"]) + "\n")
        w("- registro da execução:\n")
        for t, msg in c["marcos"][:16]:
            w(f"    - +{t}s — {msg}\n")
        w("\n")
    w("## Como interpretar\n\n")
    w("- **MB/s efetivo** = bytes varridos no cenário ÷ tempo total. O teto de "
      "leitura bruta medido acima é o limite físico do ambiente: tudo o que "
      "fica abaixo dele é análise de assinaturas, verificação estrutural, "
      "Pillow e gravação.\n")
    w("- **alvo** = as fotos apagadas do FAT32 + as órfãs da área não alocada. "
      "**fora do gabarito** = arquivo gravado cujo SHA-256 não corresponde a "
      "nenhuma foto plantada (nem viva, nem apagada, nem órfã): inclui "
      "pedaços parciais de fotos reais (estado `parcial`), que por definição "
      "não são byte a byte iguais ao original, e achados do acaso em dados "
      "aleatórios — o CSV de cada cenário diz o estado e o motivo de cada um.\n")
    w("- **vivos reencontrados** só aparece no modo cartão inteiro: sem a "
      "restrição de espaço livre o carving também devolve as fotos que nunca "
      "foram apagadas. É o comportamento esperado dessa opção.\n")
    w("- Com `apenas_espaco_livre` a etapa 2 varre somente os clusters livres "
      "do volume (contados lendo a FAT da imagem), e a etapa 1 só devolve "
      "entradas apagadas — por isso as fotos vivas não aparecem no cenário A.\n")
    w("- O cenário B encontra as órfãs da área não alocada; o A não as procura "
      "(por projeto: só o espaço livre do volume reconhecido). Isso é a "
      "diferença entre as duas opções na interface.\n")
    return s.getvalue()


def main(argv: list[str]) -> int:
    rapido = "--rapido" in argv
    cfg = CONFIG["rapido" if rapido else "completo"]
    os.makedirs(SAIDA, exist_ok=True)
    os.makedirs(TRABALHO, exist_ok=True)
    gab_path = os.path.join(SAIDA, "gabarito.json")
    reusar = ("--reusar" in argv and os.path.exists(IMAGEM)
              and os.path.exists(gab_path)
              and os.path.getsize(IMAGEM) == cfg["tamanho"])
    if reusar:
        print(f"[1/5] reaproveitando a imagem existente ({_mi(cfg['tamanho'])})")
        with open(gab_path, encoding="utf-8") as f:
            gab = json.load(f)
    else:
        gab = construir_imagem(cfg, IMAGEM)
        with open(gab_path, "w", encoding="utf-8") as f:
            json.dump(gab, f, ensure_ascii=False, indent=1)

    livre = medir_espaco_livre(gab)
    gab["volume"].update(livre)
    print(f"      espaço livre no volume: {livre['clusters_livres']} clusters "
          f"({_mi(livre['espaco_livre_bytes'])})")
    print("[4/5] medindo a leitura bruta da imagem...")
    bruta = medir_leitura_bruta(IMAGEM)
    print(f"      {bruta['mbps']:.0f} MB/s")

    print("[5/5] rodando os cenários de recuperação...")
    planos = [
        ("A — padrão da interface (só espaço livre)",
         dict(apenas_espaco_livre=True, verificar=True)),
        ("B — cartão inteiro com verificação",
         dict(cartao_inteiro=True, apenas_espaco_livre=False, verificar=True)),
        ("C — cartão inteiro sem verificação",
         dict(cartao_inteiro=True, apenas_espaco_livre=False, verificar=False,
              sem_verificar=True)),
    ]
    if rapido:
        planos = planos[:2]
    cenarios = []
    for i, (nome, op) in enumerate(planos):
        destino = os.path.join(TRABALHO, f"saida_{i}")
        c = rodar_cenario(nome, gab, destino, **op)
        print(f"      {nome}: {c['segundos']:.1f}s, "
              f"{c['recuperados']} recuperados, "
              f"{c['identicos_ao_gabarito']} idênticos, "
              f"{c['fora_do_gabarito']} fora do gabarito")
        if c["relatorio_csv"] and os.path.exists(c["relatorio_csv"]):
            shutil.copy2(c["relatorio_csv"],
                         os.path.join(SAIDA, f"relatorio_cenario_{i}.csv"))
        cenarios.append(c)
        gc.collect()

    rel = escrever_relatorio(gab, bruta, cenarios, argv)
    caminho_rel = os.path.join(SAIDA, "relatorio_benchmark.md")
    with open(caminho_rel, "w", encoding="utf-8", newline="\n") as f:
        f.write(rel)
    with open(os.path.join(SAIDA, "medidas.json"), "w", encoding="utf-8") as f:
        json.dump({"gabarito": gab, "leitura_bruta": bruta,
                   "cenarios": cenarios}, f, ensure_ascii=False, indent=1)
    print(f"\nRelatório: {caminho_rel}")
    print(f"Medidas:   {os.path.join(SAIDA, 'medidas.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
