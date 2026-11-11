# -*- coding: utf-8 -*-
"""
construtor.py — Construtores de imagens sintéticas INDEPENDENTES dos parsers.

IMPORTANTE: nada aqui reutiliza o código dos módulos de recuperação. As
estruturas são montadas byte a byte a partir das especificações (MS-FAT32,
MS-exFAT, MS-FSA/NTFS, MBR, GPT), para que os testes não mascarem erros
compartilhados entre construtor e parser.
"""

from __future__ import annotations

import binascii
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False


# ---------------------------------------------------------------------------
# Arquivos reais (Pillow) e sintéticos
# ---------------------------------------------------------------------------


def jpeg_real(largura=64, altura=48, cor=(200, 30, 30), qualidade=85,
              semente=None) -> bytes:
    """JPEG real gerado pelo Pillow (com dados entropy e RST quando grande)."""
    if not PIL_OK:
        raise RuntimeError("Pillow indisponível")
    if semente is not None:
        rnd = random.Random(semente)
        img = Image.new("RGB", (largura, altura))
        px = img.load()
        for y in range(altura):
            for x in range(largura):
                px[x, y] = tuple((c + rnd.randint(-40, 40)) % 256 for c in cor)
    else:
        img = Image.new("RGB", (largura, altura), cor)
    from io import BytesIO
    bio = BytesIO()
    # restart interval pequeno força marcadores RST em imagens maiores
    img.save(bio, "JPEG", quality=qualidade, restart_interval=2 if largura >= 64 else 0)
    return bio.getvalue()


def jpeg_grande_com_rst(semente=7, largura=320, altura=240, linhas=1) -> bytes:
    """JPEG grande com segmento DRI e marcadores RST0..RST7 reais.

    Pillow/libjpeg emite restart markers quando `restart_marker_rows` (ou
    `restart_marker_blocks`) é informado; usamos ruído aleatório para o arquivo
    ficar grande o bastante a ponto de conter vários grupos de RST.
    """
    if not PIL_OK:
        raise RuntimeError("Pillow indisponível")
    rnd = random.Random(semente)
    img = Image.new("RGB", (largura, altura))
    px = img.load()
    for y in range(altura):
        for x in range(largura):
            px[x, y] = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
    from io import BytesIO
    bio = BytesIO()
    img.save(bio, "JPEG", quality=92, restart_marker_rows=linhas)
    dados = bio.getvalue()
    assert b"\xFF\xDD" in dados, "Pillow não emitiu o segmento DRI"
    assert any(bytes([0xFF, 0xD0 + k]) in dados for k in range(8)), \
        "Pillow não emitiu marcadores RST"
    return dados


def png_real(largura=48, altura=32, cor=(30, 200, 60)) -> bytes:
    if not PIL_OK:
        raise RuntimeError("Pillow indisponível")
    img = Image.new("RGB", (largura, altura), cor)
    from io import BytesIO
    bio = BytesIO()
    img.save(bio, "PNG")
    return bio.getvalue()


def dados_pseudo_aleatorios(n: int, semente: int) -> bytes:
    return bytes(random.Random(semente).randrange(256) for _ in range(n)) \
        if n < 4096 else random.Random(semente).randbytes(n)


# ---------------------------------------------------------------------------
# FAT16/FAT32
# ---------------------------------------------------------------------------


def _dos_data_hora(ano=2024, mes=6, dia=15, h=10, m=30, s=20):
    d = (((ano - 1980) & 0x7F) << 9) | (mes << 5) | dia
    t = (h << 11) | (m << 5) | (s // 2)
    return d, t


def _entrada_dir(nome_curto11: bytes, attr: int, primeiro: int, tamanho: int,
                 apagado: bool, data=None) -> bytes:
    e = bytearray(32)
    e[0:11] = nome_curto11
    if apagado:
        e[0] = 0xE5
    e[11] = attr
    d, t = _dos_data_hora() if data is None else _dos_data_hora(*data)
    struct.pack_into("<H", e, 18, d)          # data de acesso
    struct.pack_into("<H", e, 20, (primeiro >> 16) & 0xFFFF)
    struct.pack_into("<H", e, 22, t)
    struct.pack_into("<H", e, 24, d)
    struct.pack_into("<H", e, 26, primeiro & 0xFFFF)
    struct.pack_into("<I", e, 28, tamanho)
    if apagado:
        struct.pack_into("<H", e, 20, (primeiro >> 16) & 0xFFFF)
    return bytes(e)


def _checksum_lfn(nome11: bytes) -> int:
    s = 0
    for b in nome11:
        s = ((s >> 1) | ((s & 1) << 7)) & 0xFF
        s = (s + b) & 0xFF
    return s


def _entradas_lfn(nome: str, chk: int, apagado: bool) -> list[bytes]:
    """Entradas LFN em ordem n..1 (como aparecem no diretório).
    Layout: 5 chars UTF-16 em 1-10, 6 chars em 14-25, 2 chars em 28-31."""
    utf = nome.encode("utf-16-le")
    raw = utf + b"\x00\x00" + b"\xFF" * max(0, 26 - len(utf) - 2)
    pedacos = [raw[i:i + 26] for i in range(0, len(raw), 26)]
    entradas = []
    for idx, bloco in enumerate(pedacos, start=1):
        e = bytearray(32)
        seq = idx | (0x40 if idx == len(pedacos) else 0)
        e[0] = 0xE5 if apagado else seq
        e[11] = 0x0F
        e[13] = chk
        b = bloco.ljust(26, b"\xFF")
        e[1:11] = b[0:10]
        e[14:26] = b[10:22]
        e[28:32] = b[22:26]
        entradas.append(bytes(e))
    entradas.reverse()
    return entradas


def _nome_curto_para(nome: str, usados: set) -> bytes:
    base = "".join(c if c.isalnum() else "_" for c in os.path.splitext(nome)[0]).upper()
    ext = "".join(c if c.isalnum() else "_" for c in os.path.splitext(nome)[1][1:]).upper()
    base = base[:8] or "ARQUIVO"
    n = 1
    cand = f"{base[:8]}{ext[:3]:<3}"
    while cand.encode("latin-1", "replace")[:11] in usados:
        sufixo = f"~{n}"
        cand = f"{base[:8 - len(sufixo)]}{sufixo}{ext[:3]:<3}"
        n += 1
    usados.add(cand.encode("latin-1", "replace")[:11])
    return cand[:11].encode("latin-1", "replace")[:11].ljust(11, b" ")[:11]


class ArquivoTeste:
    def __init__(self, nome: str, dados: bytes, apagado: bool = False,
                 fragmentado: bool = False, data=None, subpasta: str = ""):
        self.nome = nome
        self.dados = dados
        self.apagado = apagado
        self.fragmentado = fragmentado
        self.data = data            # (ano, mes, dia, h, m, s)
        self.subpasta = subpasta


def construir_fat32(arquivos: list[ArquivoTeste], tam_setores: int = 32768,
                    spc: int = 8, tam_setor: int = 512,
                    corromper_boot: bool = False, sem_mbr: bool = True) -> bytes:
    """Monta uma imagem FAT32 completa (boot + backup no setor 6 + FAT x2 +
    dados). Arquivos apagados têm a cadeia da FAT zerada (como o Windows faz).

    tam_setores: número total de setores do volume.
    """
    reservados = 32
    nfats = 2
    clusters_disponiveis = (tam_setores - reservados - nfats * 64) // spc
    assert clusters_disponiveis > 8
    fat_setores = 64  # suporta até 16384 clusters
    dados_inicio_setor = reservados + nfats * fat_setores

    img = bytearray(tam_setores * tam_setor)

    # -- boot sector
    b = bytearray(tam_setor)
    b[0:3] = b"\xEB\x58\x90"
    b[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", b, 11, tam_setor)
    b[13] = spc
    struct.pack_into("<H", b, 14, reservados)
    b[16] = nfats
    struct.pack_into("<H", b, 17, 0)
    b[21] = 0xF8
    struct.pack_into("<H", b, 22, 0)
    struct.pack_into("<I", b, 32, tam_setores)
    struct.pack_into("<I", b, 36, fat_setores)
    struct.pack_into("<I", b, 44, 2)   # cluster raiz
    struct.pack_into("<H", b, 48, 1)   # FSInfo
    struct.pack_into("<H", b, 50, 6)   # backup boot
    b[66] = 0x29
    struct.pack_into("<I", b, 67, 0x12345678)
    # VolLab tem EXATAMENTE 11 bytes: um rótulo de 12 empurraria o campo
    # seguinte (FilSysType em 82) e faria o setor de boot ter 513 bytes.
    b[71:82] = b"TESTE      "
    b[82:90] = b"FAT32   "
    assert len(b) == tam_setor, len(b)
    b[510:512] = b"\x55\xAA"
    img[0:tam_setor] = b
    # backup no setor 6 (e FSInfo)
    img[6 * tam_setor:7 * tam_setor] = b
    fsi = bytearray(tam_setor)
    struct.pack_into("<I", fsi, 0, 0x41615252)
    struct.pack_into("<I", fsi, 484, 0x61417272)
    struct.pack_into("<I", fsi, 488, clusters_disponiveis - 4)
    struct.pack_into("<I", fsi, 492, 3)
    fsi[510:512] = b"\x55\xAA"
    img[tam_setor:2 * tam_setor] = fsi

    fat_off = reservados * tam_setor
    dados_off = dados_inicio_setor * tam_setor
    cluster_bytes = spc * tam_setor

    # -- FAT inicial
    fat = bytearray(fat_setores * tam_setor)
    struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
    struct.pack_into("<I", fat, 4, 0x0FFFFFFF)

    proximo_cluster = 2
    alocacoes = {}   # nome -> (lista de clusters, apagado)

    def alocar_clusters(nome: str, dados: bytes, fragmentado: bool, apagado: bool):
        nonlocal proximo_cluster
        n = max(1, (len(dados) + cluster_bytes - 1) // cluster_bytes)
        clusters = []
        for k in range(n):
            if fragmentado and k > 0:
                proximo_cluster += 1  # buraco proposital
            clusters.append(proximo_cluster)
            proximo_cluster += 1
            if proximo_cluster >= clusters_disponiveis + 2:
                raise RuntimeError("imagem pequena demais")
        alocacoes[nome] = clusters
        return clusters

    # reserva clusters da raiz (cluster 2)
    raiz_cluster = 2
    proximo_cluster = 3

    # primeiro aloca todos os dados
    for a in arquivos:
        alocar_clusters(a.nome, a.dados, a.fragmentado, a.apagado)

    # escreve dados
    for a in arquivos:
        clusters = alocacoes[a.nome]
        resto = a.dados
        for c in clusters:
            off = dados_off + (c - 2) * cluster_bytes
            pedaco = resto[:cluster_bytes]
            resto = resto[cluster_bytes:]
            img[off:off + len(pedaco)] = pedaco

    # preenche FAT: vivos têm cadeia; apagados contíguos têm cadeia ZERADA
    # (comportamento típico do Windows); apagados fragmentados mantêm a
    # cadeia, simulando o caso real em que a FAT não foi limpa.
    for a in arquivos:
        if a.apagado and not a.fragmentado:
            continue
        clusters = alocacoes[a.nome]
        for i, c in enumerate(clusters):
            v = 0x0FFFFFFF if i == len(clusters) - 1 else clusters[i + 1]
            struct.pack_into("<I", fat, c * 4, v)
    # cadeia da raiz
    struct.pack_into("<I", fat, raiz_cluster * 4, 0x0FFFFFFF)

    # -- diretório raiz (cluster 2)
    usados = set()
    entradas = bytearray()
    for a in arquivos:
        nome11 = _nome_curto_para(a.nome, usados)
        chk = _checksum_lfn(nome11)
        clusters = alocacoes[a.nome]
        precisa_lfn = (a.nome.upper().replace(" ", "") !=
                       nome11.decode("latin-1").strip().replace(".", ""))
        if len(os.path.splitext(a.nome)[0]) > 8 or "." in os.path.splitext(a.nome)[0] \
                or a.nome != a.nome.upper():
            precisa_lfn = True
        if precisa_lfn:
            for e in _entradas_lfn(a.nome, chk, a.apagado):
                entradas += e
        entradas += _entrada_dir(nome11, 0x20, clusters[0], len(a.dados),
                                 a.apagado, a.data)
    entradas += b"\x00" * 32
    off_raiz = dados_off + (raiz_cluster - 2) * cluster_bytes
    img[off_raiz:off_raiz + len(entradas)] = entradas

    # duas FATs
    img[fat_off:fat_off + len(fat)] = fat
    img[fat_off + fat_setores * tam_setor:
        fat_off + 2 * fat_setores * tam_setor] = fat

    if corromper_boot:
        img[0:8] = b"\x00" * 8
    return bytes(img)


def construir_fat16(arquivos: list[ArquivoTeste], tam_setores: int = 32768,
                    spc: int = 4, tam_setor: int = 512) -> bytes:
    reservados = 8
    nfats = 2
    fat_setores = 32   # 8192 clusters máx (16 bits / 2 bytes = 256 setores; 32 setores = 8192 entradas? 32*512/2=8192 ok)
    raiz_entradas = 512
    raiz_setores = raiz_entradas * 32 // tam_setor
    dados_inicio_setor = reservados + nfats * fat_setores + raiz_setores
    clusters_disponiveis = (tam_setores - dados_inicio_setor) // spc
    assert 4085 <= clusters_disponiveis < 65525, f"clusters={clusters_disponiveis}"

    img = bytearray(tam_setores * tam_setor)
    b = bytearray(tam_setor)
    b[0:3] = b"\xEB\x3C\x90"
    b[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", b, 11, tam_setor)
    b[13] = spc
    struct.pack_into("<H", b, 14, reservados)
    b[16] = nfats
    struct.pack_into("<H", b, 17, raiz_entradas)
    struct.pack_into("<H", b, 19, tam_setores)
    b[21] = 0xF8
    struct.pack_into("<H", b, 22, fat_setores)
    struct.pack_into("<H", b, 24, 63)
    struct.pack_into("<H", b, 26, 255)
    struct.pack_into("<I", b, 28, 0)
    b[36] = 0x00
    b[38] = 0x29
    struct.pack_into("<I", b, 39, 0xABCDEF01)
    b[43:54] = b"TESTE16    "
    b[54:62] = b"FAT16   "
    assert len(b) == tam_setor, len(b)
    b[510:512] = b"\x55\xAA"
    img[0:tam_setor] = b

    fat_off = reservados * tam_setor
    raiz_off = (reservados + nfats * fat_setores) * tam_setor
    dados_off = dados_inicio_setor * tam_setor
    cluster_bytes = spc * tam_setor

    fat = bytearray(fat_setores * tam_setor)
    struct.pack_into("<H", fat, 0, 0xFFF8)
    struct.pack_into("<H", fat, 2, 0xFFFF)

    proximo = 2
    alocacoes = {}
    for a in arquivos:
        n = max(1, (len(a.dados) + cluster_bytes - 1) // cluster_bytes)
        clusters = []
        for k in range(n):
            if a.fragmentado and k > 0:
                proximo += 1
            clusters.append(proximo)
            proximo += 1
            assert proximo < clusters_disponiveis + 2
        alocacoes[a.nome] = clusters
        for i, c in enumerate(clusters):
            off = dados_off + (c - 2) * cluster_bytes
            pedaco = a.dados[i * cluster_bytes:(i + 1) * cluster_bytes]
            img[off:off + len(pedaco)] = pedaco
        if not a.apagado:
            for i, c in enumerate(clusters):
                v = 0xFFFF if i == len(clusters) - 1 else clusters[i + 1]
                struct.pack_into("<H", fat, c * 2, v)

    usados = set()
    entradas = bytearray()
    for a in arquivos:
        nome11 = _nome_curto_para(a.nome, usados)
        chk = _checksum_lfn(nome11)
        if len(os.path.splitext(a.nome)[0]) > 8 or a.nome != a.nome.upper():
            for e in _entradas_lfn(a.nome, chk, a.apagado):
                entradas += e
        entradas += _entrada_dir(nome11, 0x20, alocacoes[a.nome][0],
                                 len(a.dados), a.apagado, a.data)
    img[raiz_off:raiz_off + len(entradas)] = entradas
    img[fat_off:fat_off + len(fat)] = fat
    img[fat_off + fat_setores * tam_setor:
        fat_off + 2 * fat_setores * tam_setor] = fat
    return bytes(img)


def formatar_rapido_fat32(img: bytes, tam_setor: int = 512) -> bytes:
    """Simula formatação rápida: zera a FAT (mantendo as 2 primeiras
    entradas) e apaga as entradas do diretório raiz (primeiro byte 0x00).
    Os dados e as entradas antigas em clusters de diretório permanecem."""
    img = bytearray(img)
    (reservados,) = struct.unpack_from("<H", img, 14)
    nfats = img[16]
    (fat_setores,) = struct.unpack_from("<I", img, 36)
    fat_off = reservados * tam_setor
    for copia in range(nfats):
        base = fat_off + copia * fat_setores * tam_setor
        nova = bytearray(fat_setores * tam_setor)
        struct.pack_into("<I", nova, 0, 0x0FFFFFF8)
        struct.pack_into("<I", nova, 4, 0x0FFFFFFF)
        img[base:base + len(nova)] = nova
    # diretório raiz (cluster 2): uma formatação rápida recria a raiz vazia.
    (spc,) = struct.unpack_from("<B", img, 13)
    dados_inicio = reservados + nfats * fat_setores
    raiz_off = dados_inicio * tam_setor
    img[raiz_off:raiz_off + spc * tam_setor] = b"\x00" * (spc * tam_setor)
    return bytes(img)


# ---------------------------------------------------------------------------
# exFAT
# ---------------------------------------------------------------------------


def _entrada_exfat_file(n_secundarias: int, apagado: bool, data=None) -> bytes:
    e = bytearray(32)
    e[0] = 0x05 if apagado else 0x85
    struct.pack_into("<H", e, 2, n_secundarias)
    struct.pack_into("<H", e, 4, 0x20)  # archive
    if data:
        ano, mes, dia, h, m, s = data
        from datetime import datetime, timezone
        ep = datetime(1980, 1, 1, tzinfo=timezone.utc)
        dt = datetime(ano, mes, dia, h, m, s, tzinfo=timezone.utc)
        struct.pack_into("<I", e, 8, int((dt - ep).total_seconds()))
        e[11] = 0x80  # UTC flag + sub-sec 0
    return bytes(e)


def _entrada_exfat_stream(primeiro: int, data_len: int, tam_nome: int,
                          nome_hash: int, apagado: bool, no_fat: bool) -> bytes:
    e = bytearray(32)
    e[0] = 0x40 if apagado else 0xC0
    e[1] = (0x02 if no_fat else 0x00) | (0 if apagado else 0)
    e[3] = tam_nome
    struct.pack_into("<H", e, 4, nome_hash)
    struct.pack_into("<Q", e, 8, data_len)
    struct.pack_into("<I", e, 20, primeiro)
    struct.pack_into("<Q", e, 24, data_len)
    return bytes(e)


def _entrada_exfat_nome(parte: str, apagado: bool) -> bytes:
    e = bytearray(32)
    e[0] = 0x41 if apagado else 0xC1
    raw = parte.encode("utf-16-le")[:28].ljust(28, b"\x00")
    e[4:32] = raw
    return bytes(e)


def _hash_nome_exfat(nome: str) -> int:
    h = 0
    for ch in nome.upper().encode("utf-16-le"):
        h = ((h << 15) | (h >> 1)) & 0xFFFF
        h = (h + ch) & 0xFFFF
    return h


def construir_exfat(arquivos: list[ArquivoTeste], tam_setores: int = 32768,
                    spc_power: int = 3, bps_power: int = 9,
                    corromper_boot: bool = False) -> bytes:
    tam_setor = 1 << bps_power
    spc = 1 << spc_power
    cluster_bytes = tam_setor * spc
    img = bytearray(tam_setores * tam_setor)

    fat_off_setor = 128          # após região de boot (0..23) com folga
    fat_setores = 64
    heap_off_setor = fat_off_setor + fat_setores
    n_clusters = (tam_setores - heap_off_setor) // spc
    assert n_clusters > 16

    # layout do heap: cluster 2 = bitmap, cluster 3 = raiz, resto = dados
    bitmap_cluster = 2
    raiz_cluster = 3
    proximo = 4

    b = bytearray(tam_setor)
    b[0:3] = b"\xEB\x76\x90"
    b[3:11] = b"EXFAT   "
    struct.pack_into("<Q", b, 64, 0)                 # partition offset
    struct.pack_into("<Q", b, 72, tam_setores)       # volume length
    struct.pack_into("<I", b, 80, fat_off_setor)
    struct.pack_into("<I", b, 84, fat_setores)
    struct.pack_into("<I", b, 88, heap_off_setor)
    struct.pack_into("<I", b, 92, n_clusters)
    struct.pack_into("<I", b, 96, raiz_cluster)
    struct.pack_into("<I", b, 100, 0xDEADBEEF)
    struct.pack_into("<H", b, 104, 0x0100)
    struct.pack_into("<H", b, 106, 0)                # VolumeFlags (excluído do checksum)
    b[108] = bps_power
    b[109] = spc_power
    b[110] = 1                                        # NumberOfFats
    b[112] = 10                                       # PercentInUse (excluído)
    b[510:512] = b"\x55\xAA"

    # setores 1..8 estendidos (zeros), 9..10 zeros
    regiao_boot = bytearray(11 * tam_setor)
    regiao_boot[0:tam_setor] = b
    chk = 0
    for i in range(11 * tam_setor):
        if i in (106, 107, 112):
            continue
        chk = (((chk << 15) | (chk >> 1)) & 0xFFFF)
        chk = (chk + regiao_boot[i]) & 0xFFFF
    setor_chk = bytearray(tam_setor)
    struct.pack_into("<I", setor_chk, 0, chk)
    regiao_completa = bytes(regiao_boot) + bytes(setor_chk)
    img[0:12 * tam_setor] = regiao_completa
    # backup nos setores 12..23
    img[12 * tam_setor:24 * tam_setor] = regiao_completa

    dados_off = heap_off_setor * tam_setor
    fat_off = fat_off_setor * tam_setor
    fat = bytearray(fat_setores * tam_setor)

    # bitmap (cluster 2)
    bitmap_bytes = bytearray((n_clusters + 7) // 8)

    def marcar(c, usado=True):
        idx = c - 2
        if usado:
            bitmap_bytes[idx // 8] |= 1 << (idx % 8)
        else:
            bitmap_bytes[idx // 8] &= ~(1 << (idx % 8))

    marcar(bitmap_cluster)
    marcar(raiz_cluster)

    alocacoes = {}
    for a in arquivos:
        n = max(1, (len(a.dados) + cluster_bytes - 1) // cluster_bytes)
        clusters = []
        for k in range(n):
            if a.fragmentado and k > 0:
                proximo += 1
            clusters.append(proximo)
            proximo += 1
            assert proximo < n_clusters + 2
        alocacoes[a.nome] = clusters
        for i, c in enumerate(clusters):
            off = dados_off + (c - 2) * cluster_bytes
            pedaco = a.dados[i * cluster_bytes:(i + 1) * cluster_bytes]
            img[off:off + len(pedaco)] = pedaco
            if not a.apagado:
                marcar(c)
            # cadeia FAT: mantida para vivos; para apagados fragmentados a
            # cadeia sobrevive (caso real), apagados contíguos têm FAT zerada
            if not a.apagado or a.fragmentado:
                struct.pack_into("<I", fat, c * 4,
                                 0xFFFFFFFF if i == len(clusters) - 1 else clusters[i + 1])
    # raiz: fim de cadeia
    struct.pack_into("<I", fat, raiz_cluster * 4, 0xFFFFFFFF)

    # bitmap no heap
    off_bm = dados_off + (bitmap_cluster - 2) * cluster_bytes
    img[off_bm:off_bm + len(bitmap_bytes)] = bitmap_bytes

    # diretório raiz: entrada do bitmap + arquivos
    dir_bytes = bytearray()
    e = bytearray(32)
    e[0] = 0x81          # allocation bitmap
    e[1] = 0             # flags (0 = bitmap 0)
    struct.pack_into("<I", e, 20, bitmap_cluster)
    struct.pack_into("<Q", e, 8, len(bitmap_bytes))
    dir_bytes += e

    for a in arquivos:
        clusters = alocacoes[a.nome]
        nome = a.nome
        partes = [nome[i:i + 14] for i in range(0, len(nome), 14)]
        n_sec = 1 + len(partes)
        dir_bytes += _entrada_exfat_file(n_sec, a.apagado, a.data)
        no_fat = not a.fragmentado
        dir_bytes += _entrada_exfat_stream(clusters[0], len(a.dados), len(nome),
                                           _hash_nome_exfat(nome), a.apagado, no_fat)
        for p in partes:
            dir_bytes += _entrada_exfat_nome(p, a.apagado)

    off_raiz = dados_off + (raiz_cluster - 2) * cluster_bytes
    img[off_raiz:off_raiz + len(dir_bytes)] = dir_bytes
    img[fat_off:fat_off + len(fat)] = fat

    if corromper_boot:
        img[0:11] = b"\x00" * 11
    return bytes(img)


# ---------------------------------------------------------------------------
# NTFS
# ---------------------------------------------------------------------------

NTFS_REC_SIZE = 1024
NTFS_USA_MAGIC = 0x000A


def _fixup_record(rec: bytearray, usa_ofs: int, rec_size: int = NTFS_REC_SIZE,
                  setor: int = 512):
    n_setores = rec_size // setor
    struct.pack_into("<H", rec, usa_ofs, NTFS_USA_MAGIC)
    for i in range(n_setores):
        fim = (i + 1) * setor
        struct.pack_into("<H", rec, usa_ofs + 2 + i * 2,
                         struct.unpack_from("<H", rec, fim - 2)[0])
        struct.pack_into("<H", rec, fim - 2, NTFS_USA_MAGIC)


def _ntfs_tempo(ano=2024, mes=6, dia=15, h=10, m=30, s=20) -> int:
    from datetime import datetime
    dt = datetime(ano, mes, dia, h, m, s)
    ep = datetime(1601, 1, 1)
    return int((dt - ep).total_seconds() * 10_000_000)


def _attr_file_name(nome: str, pai_ref: int, namespace: int = 1,
                    tam_real: int = 0, tempo=None) -> bytes:
    t = tempo if tempo else _ntfs_tempo()
    raw_nome = nome.encode("utf-16-le")
    c = bytearray(66 + len(raw_nome) + 2)
    struct.pack_into("<Q", c, 0, pai_ref)
    struct.pack_into("<Q", c, 8, t)     # criação
    struct.pack_into("<Q", c, 16, t)    # modificação
    struct.pack_into("<Q", c, 24, t)    # MFT
    struct.pack_into("<Q", c, 32, t)    # acesso
    struct.pack_into("<Q", c, 40, tam_real)
    struct.pack_into("<Q", c, 48, tam_real)
    struct.pack_into("<I", c, 56, 0x20)  # flags archive
    struct.pack_into("<I", c, 60, 0)
    c[64] = len(nome)
    c[65] = namespace
    c[66:66 + len(raw_nome)] = raw_nome
    comp = 24 + ((len(c) + 7) // 8) * 8
    attr = bytearray(comp)
    struct.pack_into("<I", attr, 0, 0x30)
    struct.pack_into("<I", attr, 4, comp)
    attr[8] = 0  # form code: 0 = residente
    attr[9] = 0
    struct.pack_into("<H", attr, 10, 0)
    struct.pack_into("<H", attr, 12, 0)
    struct.pack_into("<H", attr, 14, 2)
    struct.pack_into("<I", attr, 16, len(c))
    struct.pack_into("<H", attr, 20, 24)
    attr[24:24 + len(c)] = c
    return bytes(attr)


def _attr_data_residente(dados: bytes) -> bytes:
    comp = 24 + ((len(dados) + 7) // 8) * 8
    attr = bytearray(comp)
    struct.pack_into("<I", attr, 0, 0x80)
    struct.pack_into("<I", attr, 4, comp)
    attr[8] = 0  # residente
    struct.pack_into("<H", attr, 14, 1)
    struct.pack_into("<I", attr, 16, len(dados))
    struct.pack_into("<H", attr, 20, 24)
    attr[24:24 + len(dados)] = dados
    return bytes(attr)


def _tam_bytes_assinado(v: int) -> int:
    """Menor número de bytes que representa v como inteiro little-endian
    COM SINAL (os offsets de data runs são deltas, podem ser negativos)."""
    n = 1
    while n < 8:
        try:
            v.to_bytes(n, "little", signed=True)
            return n
        except OverflowError:
            n += 1
    return 8


def _runs_bytes(runs: list[tuple[int, int]]) -> bytes:
    """runs: lista de (delta_lcn, n_clusters); delta relativo ao anterior."""
    out = bytearray()
    for delta, n in runs:
        len_bytes = max(1, (n.bit_length() + 7) // 8)
        if delta == 0:
            off_bytes, val = 0, b""
        else:
            off_bytes = _tam_bytes_assinado(delta)
            val = delta.to_bytes(off_bytes, "little", signed=True)
        # convenção NTFS real: nibble baixo = bytes do length,
        # nibble alto = bytes do offset (delta de LCN, com sinal)
        out.append(len_bytes | (off_bytes << 4))
        out += n.to_bytes(len_bytes, "little")
        out += val
    out.append(0)
    return bytes(out)


def _attr_attribute_list(entradas: list[tuple[int, int, int, int, int]]) -> bytes:
    """entradas: [(tipo, vcn_ini, reg_num, seq, attr_id)]. Atributo
    ATTRIBUTE_LIST residente (tipo 0x20). Entrada de 32 bytes (28 + padding),
    conforme o layout real: VCN em 0x0C e referência ao registro em 0x14."""
    corpo = bytearray()
    for k, (tipo, vcn, reg_num, seq, attr_id) in enumerate(entradas):
        e = bytearray(32)
        struct.pack_into("<I", e, 0, tipo)
        struct.pack_into("<H", e, 4, 32)      # comprimento da entrada
        e[6] = 0                              # tamanho do nome
        e[7] = 28                             # deslocamento do nome
        struct.pack_into("<H", e, 8, 0)       # flags
        struct.pack_into("<H", e, 10, attr_id)
        struct.pack_into("<Q", e, 12, vcn)    # menor VCN
        struct.pack_into("<Q", e, 20, (seq << 48) | reg_num)
        corpo += e
    comp = 24 + ((len(corpo) + 7) // 8) * 8
    attr = bytearray(comp)
    struct.pack_into("<I", attr, 0, 0x20)
    struct.pack_into("<I", attr, 4, comp)
    attr[8] = 0             # residente
    struct.pack_into("<H", attr, 14, 3)
    struct.pack_into("<I", attr, 16, len(corpo))
    struct.pack_into("<H", attr, 20, 24)
    attr[24:24 + len(corpo)] = corpo
    return bytes(attr)


def _attr_data_nao_residente(runs: list[tuple[int, int]], tam_alocado: int,
                             tam_real: int, comprimido: bool = False,
                             unidade: int = 16, sparse: list[tuple[int, int]] | None = None,
                             vcn_ini: int = 0, tam_total_vcn: int | None = None) -> bytes:
    """sparse: lista de (n_clusters_sparse) intercalados após cada run."""
    todos = []
    for i, (delta, n) in enumerate(runs):
        todos.append((delta, n, False))
        if sparse and i < len(sparse) and sparse[i]:
            todos.append((0, sparse[i], True))
    rb = bytearray()
    for delta, n, sp in todos:
        len_bytes = max(1, (n.bit_length() + 7) // 8)
        if sp:
            # run sparse: nibble baixo = tamanho do length, nibble alto = 0
            # (sem campo de offset: não há clusters físicos)
            rb.append(len_bytes)
            rb += n.to_bytes(len_bytes, "little")
        else:
            off_bytes = max(1, _tam_bytes_assinado(delta))
            val = delta.to_bytes(off_bytes, "little", signed=True)
            rb.append(len_bytes | (off_bytes << 4))
            rb += n.to_bytes(len_bytes, "little")
            rb += val
    rb.append(0)
    vcn_fim = vcn_ini + sum(n for _d, n, _s in todos) - 1
    runs_ofs = 72
    comp = runs_ofs + ((len(rb) + 7) // 8) * 8
    attr = bytearray(comp)
    struct.pack_into("<I", attr, 0, 0x80)
    struct.pack_into("<I", attr, 4, comp)
    attr[8] = 1  # não residente
    struct.pack_into("<H", attr, 12, 0x0001 if comprimido else 0)
    struct.pack_into("<H", attr, 14, 1)
    struct.pack_into("<Q", attr, 16, vcn_ini)           # VCN inicial
    struct.pack_into("<Q", attr, 24, max(0, vcn_fim))   # VCN final
    struct.pack_into("<H", attr, 32, runs_ofs)
    struct.pack_into("<H", attr, 34, unidade if comprimido else 0)
    struct.pack_into("<Q", attr, 40, tam_alocado)
    struct.pack_into("<Q", attr, 48, tam_real)
    struct.pack_into("<Q", attr, 56, tam_real)
    attr[runs_ofs:runs_ofs + len(rb)] = rb
    return bytes(attr)


def _registro_mft(numero: int, em_uso: bool, diretorio: bool,
                  atributos: list[bytes], rec_size: int = NTFS_REC_SIZE,
                  seq: int = 1, base_ref: int = 0) -> bytes:
    rec = bytearray(rec_size)
    rec[0:4] = b"FILE"
    usa_ofs = 48
    n_setores = rec_size // 512
    struct.pack_into("<H", rec, 4, usa_ofs)
    struct.pack_into("<H", rec, 6, n_setores + 1)
    struct.pack_into("<Q", rec, 8, 0)         # LSN
    struct.pack_into("<H", rec, 16, seq)
    struct.pack_into("<H", rec, 18, 1)        # links
    attr_ofs = usa_ofs + 2 * (n_setores + 1)
    attr_ofs = (attr_ofs + 7) & ~7
    struct.pack_into("<H", rec, 20, attr_ofs)
    flags = (1 if em_uso else 0) | (2 if diretorio else 0)
    struct.pack_into("<H", rec, 22, flags)
    pos = attr_ofs
    for a in atributos:
        rec[pos:pos + len(a)] = a
        pos += len(a)
    struct.pack_into("<I", rec, pos, 0xFFFFFFFF)
    pos += 4
    struct.pack_into("<I", rec, 24, pos)          # bytes usados
    struct.pack_into("<I", rec, 28, rec_size)     # alocado
    struct.pack_into("<Q", rec, 32, base_ref)     # referência ao registro base
    struct.pack_into("<I", rec, 44, numero)       # número do registro
    _fixup_record(rec, usa_ofs, rec_size)
    return bytes(rec)


def lznt1_comprimir_unidade(dados: bytes) -> bytes:
    """Comprime UMA unidade de 4096 bytes em LZNT1 (MS-XCA).
    Estratégia determinística para testes: dados com período de repetição
    <= 16 (distância máx. do token com 4 bits de offset) viram chunk
    comprimido; caso contrário, chunk não comprimido (flag 0)."""
    assert len(dados) == 4096
    periodo = 0
    for p in range(1, 17):
        if 4096 % p == 0 and dados[:p] * (4096 // p) == dados:
            periodo = p
            break
    if periodo == 0:
        # chunk não comprimido: campo de 12 bits = (len(corpo) - 1)
        cab = struct.pack("<H", (4096 - 1) & 0x0FFF)
        return cab + dados
    # MS-XCA: um grupo de literais só pode ser curto se for o ÚLTIMO do chunk,
    # então emite dois grupos completos de 8 literais (16 bytes) antes das
    # cópias — distância do token <= periodo <= 16 cabe no campo de offset.
    corpo = bytearray()
    for ini in (0, 8):
        corpo.append(0)              # flags 0 => 8 literais
        corpo += dados[ini:ini + 8]
    pos = 16                         # posição na saída descomprimida
    i = 16
    while i < 4096:
        flags = 0
        grupo = bytearray()
        n_tokens = 0
        while i < 4096 and n_tokens < 8:
            # a divisão offset/length do token depende da posição atual
            offset_bits = 4
            while (1 << offset_bits) <= pos and offset_bits < 12:
                offset_bits += 1
            length_bits = 16 - offset_bits
            max_len = (1 << length_bits) + 2
            length = min(max_len, 4096 - i)
            token = ((periodo - 1) << length_bits) | (length - 3)
            grupo.append(token & 0xFF)
            grupo.append((token >> 8) & 0xFF)
            flags |= 1 << n_tokens
            n_tokens += 1
            i += length
            pos += length
        corpo.append(flags)
        corpo += grupo
    tam_corpo = len(corpo)
    if tam_corpo > 4094:
        cab = struct.pack("<H", (4096 - 1) & 0x0FFF)
        return cab + dados
    cab = struct.pack("<H", 0x8000 | ((tam_corpo - 1) & 0x0FFF))
    return cab + bytes(corpo)


def construir_ntfs(arquivos: list[ArquivoTeste], tam_setores: int = 16384,
                   spc: int = 8, tam_setor: int = 512,
                   corromper_boot: bool = False,
                   registro_orfao: tuple | None = None) -> bytes:
    """Monta volume NTFS sintético com MFT funcional.

    registro_orfao: (nome, dados_offset_cluster, n_clusters, tamanho, numero)
      para simular um registro FILE antigo esquecido fora da MFT.
    """
    cluster_bytes = spc * tam_setor
    img = bytearray(tam_setores * tam_setor)
    rec_size = NTFS_REC_SIZE

    mft_cluster = 2
    n_registros = 64
    mft_clusters = (n_registros * rec_size + cluster_bytes - 1) // cluster_bytes
    mftmirr_cluster = mft_cluster + mft_clusters
    bitmap_cluster = mftmirr_cluster + 4
    total_clusters = tam_setores // spc

    # -- boot
    b = bytearray(tam_setor)
    b[0:3] = b"\xEB\x52\x90"
    b[3:11] = b"NTFS    "
    struct.pack_into("<H", b, 11, tam_setor)
    b[13] = spc
    struct.pack_into("<Q", b, 40, tam_setores)
    struct.pack_into("<Q", b, 48, mft_cluster)
    struct.pack_into("<Q", b, 56, mftmirr_cluster)
    b[64] = (-10) & 0xFF   # 2^10 = 1024 bytes por registro
    struct.pack_into("<b", b, 64, -10)
    struct.pack_into("<b", b, 68, -10)
    struct.pack_into("<Q", b, 72, 0x1122334455667788)
    b[510:512] = b"\x55\xAA"
    img[0:tam_setor] = b
    # backup no último setor
    img[(tam_setores - 1) * tam_setor:tam_setores * tam_setor] = b

    mft_off = mft_cluster * cluster_bytes
    registros = {}

    # registro 0: $MFT
    attr0 = [
        _attr_file_name("$MFT", (5 << 0) | 0, namespace=3),
        _attr_data_nao_residente([(mft_cluster, mft_clusters)],
                                 mft_clusters * cluster_bytes,
                                 n_registros * rec_size),
    ]
    registros[0] = _registro_mft(0, True, False, attr0)

    # registro 5: diretório raiz
    attr5 = [
        _attr_file_name(".", (5 << 48) | 5, namespace=1, tam_real=0),
    ]
    registros[5] = _registro_mft(5, True, True, attr5)

    # alocação de clusters para arquivos de usuário
    proximo = bitmap_cluster + 1
    alocacoes = {}
    dados_usuario = {}
    for a in arquivos:
        n = max(1, (len(a.dados) + cluster_bytes - 1) // cluster_bytes)
        clusters = []
        if a.fragmentado:
            c1 = proximo
            proximo += n // 2 + 1
            c2 = proximo
            proximo += n - n // 2 + 2
            runs = [(c1, n // 2), (c2 - c1, n - n // 2)]
            clusters = list(range(c1, c1 + n // 2)) + list(range(c2, c2 + n - n // 2))
        else:
            c1 = proximo
            proximo += n + 1
            runs = [(c1, n)]
            clusters = list(range(c1, c1 + n))
        alocacoes[a.nome] = (runs, clusters)
        dados_usuario[a.nome] = a.dados
        for i, c in enumerate(clusters):
            off = c * cluster_bytes
            pedaco = a.dados[i * cluster_bytes:(i + 1) * cluster_bytes]
            img[off:off + len(pedaco)] = pedaco

    # registros de usuário
    numero = 24
    dirs_criados = {}   # nome da subpasta -> número do registro
    for a in arquivos:
        runs, clusters = alocacoes[a.nome]
        tam = len(a.dados)
        pai_ref = (1 << 48) | 5
        if a.subpasta:
            if a.subpasta not in dirs_criados:
                # número alto para não colidir com arquivos
                num_dir = 100 + len(dirs_criados)
                dirs_criados[a.subpasta] = num_dir
                registros[num_dir] = _registro_mft(
                    num_dir, True, True,
                    [_attr_file_name(a.subpasta, (1 << 48) | 5, namespace=1)])
            pai_ref = (1 << 48) | dirs_criados[a.subpasta]
        atributos = [
            _attr_file_name(a.nome, pai_ref, namespace=1, tam_real=tam,
                            tempo=_ntfs_tempo(*(a.data or ()))),
        ]
        extra = a.__dict__.get("extra_ntfs") or {}
        if extra.get("residente"):
            atributos.append(_attr_data_residente(a.dados))
        elif extra.get("comprimido"):
            comp = lznt1_comprimir_unidade(a.dados[:4096].ljust(4096, b"\x00"))
            n_fis = (len(comp) + cluster_bytes - 1) // cluster_bytes
            c1 = proximo
            proximo += n_fis + 1
            for i in range(n_fis):
                off = (c1 + i) * cluster_bytes
                pedaco = comp[i * cluster_bytes:(i + 1) * cluster_bytes]
                img[off:off + len(pedaco)] = pedaco
            atributos.append(_attr_data_nao_residente(
                [(c1, n_fis)], 16 * cluster_bytes, tam,
                comprimido=True, unidade=16,
                sparse=[16 - n_fis]))
        elif extra.get("attr_list") and a.fragmentado and len(runs) == 2:
            # DATA dividido em dois registros via ATTRIBUTE_LIST
            n1 = runs[0][1]
            num_base = numero
            num_ext = numero + 50   # não colide (poucos arquivos por imagem)
            c2_abs = runs[0][0] + runs[1][0]
            atributos.append(_attr_attribute_list([
                (0x80, 0, num_base, 1, 4),
                (0x80, n1, num_ext, 1, 5),
            ]))
            atributos.append(_attr_data_nao_residente(
                [runs[0]], n1 * cluster_bytes, tam, vcn_ini=0))
            registros[num_ext] = _registro_mft(
                num_ext, not a.apagado, False,
                [_attr_data_nao_residente([(c2_abs, runs[1][1])],
                                          runs[1][1] * cluster_bytes, tam,
                                          vcn_ini=n1)],
                base_ref=(1 << 48) | num_base)
        else:
            atributos.append(_attr_data_nao_residente(
                runs, len(clusters) * cluster_bytes, tam))
        em_uso = not a.apagado
        registros[numero] = _registro_mft(numero, em_uso, False, atributos)
        a.__dict__["registro"] = numero
        numero += 1

    # $Bitmap (registro 6): cluster bitmap_cluster
    bitmap = bytearray((total_clusters + 7) // 8)
    for c in range(0, bitmap_cluster + 1):
        bitmap[c // 8] |= 1 << (c % 8)
    for nome, (_runs, clusters) in alocacoes.items():
        for c in clusters:
            bitmap[c // 8] |= 1 << (c % 8)
    off_bm = bitmap_cluster * cluster_bytes
    img[off_bm:off_bm + len(bitmap)] = bitmap
    n_bm_clusters = max(1, (len(bitmap) + cluster_bytes - 1) // cluster_bytes)
    registros[6] = _registro_mft(6, True, False, [
        _attr_file_name("$Bitmap", (1 << 48) | 5, namespace=3),
        _attr_data_nao_residente([(bitmap_cluster, n_bm_clusters)],
                                 n_bm_clusters * cluster_bytes, len(bitmap)),
    ])

    # registro órfão (simula sobra de formatação fora da MFT):
    # registro_orfao = (nome, dados, cluster_do_registro, cluster_dos_dados,
    #                   numero_do_registro)
    if registro_orfao:
        nome, dados_orf, rec_cluster, dados_cluster, num_orf = registro_orfao
        n_orf = max(1, (len(dados_orf) + cluster_bytes - 1) // cluster_bytes)
        off_dados = dados_cluster * cluster_bytes
        img[off_dados:off_dados + len(dados_orf)] = dados_orf
        attr_orf = [
            _attr_file_name(nome, (1 << 48) | 5, namespace=1,
                            tam_real=len(dados_orf)),
            _attr_data_nao_residente([(dados_cluster, n_orf)],
                                     n_orf * cluster_bytes, len(dados_orf)),
        ]
        reg_orf = _registro_mft(num_orf, False, False, attr_orf)
        off_rec = rec_cluster * cluster_bytes
        img[off_rec:off_rec + len(reg_orf)] = reg_orf

    # grava MFT
    for num, reg in registros.items():
        off = mft_off + num * rec_size
        img[off:off + rec_size] = reg
    # MFTMirr: cópia dos 4 primeiros registros
    for num in range(4):
        if num in registros:
            off = mftmirr_cluster * cluster_bytes + num * rec_size
            img[off:off + rec_size] = registros[num]

    if corromper_boot:
        img[0:8] = b"\x00" * 8
    return bytes(img)


def formatar_rapido_ntfs(img: bytes, tam_setor: int = 512, spc: int = 8) -> bytes:
    """Simula formatação rápida NTFS: mantém boot, zera parte da MFT de
    usuário (registros >= 24) e libera clusters no bitmap. Registros antigos
    permanecem apenas se movidos para área órfã pelo chamador."""
    img = bytearray(img)
    rec_size = NTFS_REC_SIZE
    mft_cluster = 2
    cluster_bytes = spc * tam_setor
    mft_off = mft_cluster * cluster_bytes
    for num in range(24, 64):
        off = mft_off + num * rec_size
        img[off:off + rec_size] = b"\x00" * rec_size
    return bytes(img)


# ---------------------------------------------------------------------------
# MBR / GPT / EBR
# ---------------------------------------------------------------------------


def construir_mbr(particoes: list[tuple[int, int, int]], tam_setor: int = 512,
                  disco_setores: int = 0) -> bytes:
    """particoes: [(lba_inicio, n_setores, tipo)]. Retorna setor MBR (512B)."""
    mbr = bytearray(tam_setor)
    mbr[0:3] = b"\xEB\x58\x90"
    mbr[3:11] = b"SYSLINUX"
    for i, (lba, n, tipo) in enumerate(particoes[:4]):
        off = 446 + i * 16
        mbr[off] = 0
        mbr[off + 4] = tipo
        struct.pack_into("<I", mbr, off + 8, lba)
        struct.pack_into("<I", mbr, off + 12, n)
    mbr[510:512] = b"\x55\xAA"
    return bytes(mbr)


def construir_gpt(particoes: list[tuple[int, int, str]], disco_setores: int,
                  tam_setor: int = 512) -> tuple[bytes, bytes, bytes]:
    """Retorna (setor1_protetivo_lba1? , header_gpt, tabela).
    GPT primário no LBA 1, tabela nos LBAs 2..33. Chamador monta o disco."""
    guid_tipo_base = bytes.fromhex("EBD0A0A2B9E5443387C068B6B72699C7")
    entradas = bytearray()
    n_entradas = 128
    tam_entrada = 128
    for i, (ini, fim, nome) in enumerate(particoes):
        e = bytearray(tam_entrada)
        e[0:16] = guid_tipo_base
        e[16:32] = random.Random(i).randbytes(16)
        struct.pack_into("<Q", e, 32, ini)
        struct.pack_into("<Q", e, 40, fim)
        struct.pack_into("<Q", e, 48, 0)
        nm = nome.encode("utf-16-le")[:72]
        e[56:56 + len(nm)] = nm
        entradas += e
    while len(entradas) < n_entradas * tam_entrada:
        entradas += b"\x00" * tam_entrada
    crc_tabela = binascii.crc32(bytes(entradas)) & 0xFFFFFFFF

    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<I", header, 16, 0)  # CRC preenchido depois
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, disco_setores - 1)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, disco_setores - 34)
    header[56:72] = random.Random(42).randbytes(16)
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, n_entradas)
    struct.pack_into("<I", header, 84, tam_entrada)
    struct.pack_into("<I", header, 88, crc_tabela)
    padded = bytes(header) + b"\x00" * (tam_setor - 92)
    crc_cab = binascii.crc32(padded[:16] + b"\x00\x00\x00\x00" + padded[20:92]) & 0xFFFFFFFF
    header_final = bytearray(padded)
    struct.pack_into("<I", header_final, 16, crc_cab)
    # MBR protetivo
    protetivo = bytearray(tam_setor)
    protetivo[446 + 4] = 0xEE
    struct.pack_into("<I", protetivo, 446 + 8, 1)
    struct.pack_into("<I", protetivo, 446 + 12, min(disco_setores - 1, 0xFFFFFFFF))
    protetivo[510:512] = b"\x55\xAA"
    return bytes(protetivo), bytes(header_final), bytes(entradas)


def montar_disco_gpt(particoes: list[tuple[int, int, bytes, str]],
                     disco_setores: int, tam_setor: int = 512) -> bytes:
    """particoes: [(lba_ini, lba_fim, conteudo_volume, nome)]."""
    img = bytearray(disco_setores * tam_setor)
    protetivo, header, tabela = construir_gpt(
        [(ini, fim, nome) for ini, fim, _c, nome in particoes],
        disco_setores, tam_setor)
    img[0:tam_setor] = protetivo
    img[tam_setor:2 * tam_setor] = header
    img[2 * tam_setor:2 * tam_setor + len(tabela)] = tabela
    # GPT secundário: tabela nos últimos 33 setores antes do header final
    tam_tabela_setores = (len(tabela) + tam_setor - 1) // tam_setor
    fim = disco_setores
    img[(fim - 1) * tam_setor:fim * tam_setor] = header  # ajustado abaixo
    # header secundário correto
    h2 = bytearray(header)
    struct.pack_into("<Q", h2, 16, 0)
    struct.pack_into("<Q", h2, 24, fim - 1)
    struct.pack_into("<Q", h2, 32, 1)
    struct.pack_into("<Q", h2, 72, fim - 1 - tam_tabela_setores)
    padded = bytes(h2) + b"\x00" * (tam_setor - len(h2))
    crc2 = binascii.crc32(padded[:16] + b"\x00\x00\x00\x00" + padded[20:92]) & 0xFFFFFFFF
    h2f = bytearray(padded)
    struct.pack_into("<I", h2f, 16, crc2)
    img[(fim - 1) * tam_setor:fim * tam_setor] = bytes(h2f[:tam_setor])
    img[(fim - 1 - tam_tabela_setores) * tam_setor:(fim - 1) * tam_setor] = \
        tabela.ljust(tam_tabela_setores * tam_setor, b"\x00")
    for ini, fim2, conteudo, _nome in particoes:
        off = ini * tam_setor
        img[off:off + len(conteudo)] = conteudo
    return bytes(img)


def montar_disco_mbr(particoes: list[tuple[int, int, bytes, int]],
                     disco_setores: int, tam_setor: int = 512,
                     apagar_mbr: bool = False) -> bytes:
    """particoes: [(lba_ini, n_setores, conteudo, tipo)]."""
    img = bytearray(disco_setores * tam_setor)
    mbr = construir_mbr([(ini, n, t) for ini, n, _c, t in particoes],
                        tam_setor, disco_setores)
    if not apagar_mbr:
        img[0:tam_setor] = mbr
    for ini, n, conteudo, _t in particoes:
        off = ini * tam_setor
        img[off:off + len(conteudo)] = conteudo
    return bytes(img)


def montar_disco_ebr(volume_estendido_ini: int, volume_estendido_fim: int,
                     logicas: list[tuple[int, int, bytes, int]],
                     disco_setores: int, tam_setor: int = 512) -> bytes:
    """Cria MBR com uma partição estendida e uma cadeia de EBRs.
    logicas: [(lba_abs_ini, n_setores, conteudo, tipo)]."""
    img = bytearray(disco_setores * tam_setor)
    mbr = construir_mbr([(volume_estendido_ini,
                          volume_estendido_fim - volume_estendido_ini + 1, 0x0F)],
                        tam_setor, disco_setores)
    img[0:tam_setor] = mbr
    ebr_lba = volume_estendido_ini
    for idx, (ini, n, conteudo, tipo) in enumerate(logicas):
        off_ebr = ebr_lba * tam_setor
        e = bytearray(tam_setor)
        # entrada 0: partição lógica; LBA relativo ao início da estendida
        e[446 + 4] = tipo
        struct.pack_into("<I", e, 446 + 8, ini - volume_estendido_ini)
        struct.pack_into("<I", e, 446 + 12, n)
        if idx + 1 < len(logicas):
            prox_ini = logicas[idx + 1][0] - tam_setor // tam_setor  # EBR seguinte
            # o próximo EBR fica 1 setor antes da próxima partição
            prox_ebr = logicas[idx + 1][0] - 1
            e[446 + 16 + 4] = 0x0F
            struct.pack_into("<I", e, 446 + 16 + 8,
                             prox_ebr - volume_estendido_ini)
            struct.pack_into("<I", e, 446 + 16 + 12, 1)
            del prox_ini
        e[510:512] = b"\x55\xAA"
        img[off_ebr:off_ebr + tam_setor] = e
        off = ini * tam_setor
        img[off:off + len(conteudo)] = conteudo
        ebr_lba = logicas[idx + 1][0] - 1 if idx + 1 < len(logicas) else 0
        if not ebr_lba:
            break
    return bytes(img)


def gravar(caminho: str, dados: bytes) -> str:
    with open(caminho, "wb") as f:
        f.write(dados)
    return caminho
