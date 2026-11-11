# -*- coding: utf-8 -*-
"""
particoes.py — Detecção de partições e volumes em discos e imagens.

Reconhece:
  * Discos sem tabela de partições (volume diretamente no início, ex.: cartões).
  * MBR (incluindo partições lógicas via EBR dentro de estendida).
  * GPT (com validação de CRC32 do cabeçalho e da tabela; fallback para o GPT
    secundário quando o primário está danificado).
  * Setores lógicos de 512 e 4096 bytes.
  * Cópias de boot de reserva (FAT32 setor 6, exFAT setores 12..23, NTFS último
    setor do volume) usadas como fallback quando o boot primário está ilegível.
  * Busca por partições perdidas (varredura de assinaturas em offsets prováveis).

Nenhuma estrutura do disco é modificada: leitura estritamente somente-leitura.
"""

from __future__ import annotations

import binascii
import struct

from aquisicao import LeitorBase, LeitorRecorte

TIPOS_FAT_MBR = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E, 0x14, 0x16, 0x1B, 0x1C, 0x1E}
TIPOS_NTFS_MBR = {0x07}
TIPOS_EXFAT_MBR = {0x07}  # exFAT também usa 0x07; diferenciado pelo boot
TIPO_ESTENDIDA = {0x05, 0x0F, 0x85}
TIPO_GPT_PROTETIVO = 0xEE
TIPOS_LINUX = {0x83}

FIM = 0xFFFFFFFFFFFFFFFF


class Particao:
    """Descrição de uma partição/volume encontrado."""

    def __init__(self, inicio: int, tamanho: int, tipo: str, origem: str,
                 indice: int = -1, bootavel: bool = False,
                 setor: int = 512, confiavel: bool = True):
        self.inicio = inicio            # offset em bytes no disco
        self.tamanho = tamanho          # bytes
        self.tipo = tipo                # 'fat32'|'fat16'|'exfat'|'ntfs'|'desconhecido'
        self.origem = origem            # 'volume'|'mbr'|'gpt'|'ebr'|'backup'|'perdida'|'pasta'
        self.indice = indice
        self.bootavel = bootavel
        self.setor = setor
        self.confiavel = confiavel

    @property
    def fim(self) -> int:
        return self.inicio + self.tamanho

    def recorte(self, leitor: LeitorBase, nome: str = "") -> LeitorRecorte:
        return LeitorRecorte(leitor, self.inicio, self.tamanho,
                             nome or f"{leitor.nome}@{self.inicio}",
                             setor_size=self.setor)

    def __repr__(self):
        return (f"Particao({self.tipo}, {self.origem}, ini={self.inicio}, "
                f"tam={self.tamanho})")


# ---------------------------------------------------------------------------
# Boot sectors
# ---------------------------------------------------------------------------


def identificar_boot(setor: bytes):
    """Retorna (tipo, dict_info) a partir do setor de boot de um volume.
    tipo: 'fat32'|'fat16'|'exfat'|'ntfs'|None."""
    if len(setor) < 512:
        return None, {}
    assinatura_ok = setor[510:512] == b"\x55\xAA"
    if setor[3:11] == b"EXFAT   " and assinatura_ok:
        info = _info_exfat(setor)
        return ("exfat", info) if info else (None, {})
    if setor[3:11] == b"NTFS    " and assinatura_ok:
        info = _info_ntfs(setor)
        return ("ntfs", info) if info else (None, {})
    if setor[0] == 0xEB and setor[510:512] == b"\x55\xAA":
        info = _info_fat(setor)
        if info:
            return (info["tipo"], info)
    return None, {}


def _info_fat(s: bytes):
    try:
        (bytes_setor,) = struct.unpack_from("<H", s, 11)
        (spc,) = struct.unpack_from("<B", s, 13)
        (reservados,) = struct.unpack_from("<H", s, 14)
        (nfats,) = struct.unpack_from("<B", s, 16)
        (raiz_entradas,) = struct.unpack_from("<H", s, 17)
        (total16,) = struct.unpack_from("<H", s, 19)
        (fat16_size,) = struct.unpack_from("<H", s, 22)
        (ocultos,) = struct.unpack_from("<I", s, 28)
        (total32,) = struct.unpack_from("<I", s, 32)
        (fat32_size,) = struct.unpack_from("<I", s, 36)
        (raiz_cluster,) = struct.unpack_from("<I", s, 44)
        (backup,) = struct.unpack_from("<H", s, 50)
    except struct.error:
        return None
    if bytes_setor not in (512, 1024, 2048, 4096) or spc == 0 or reservados == 0 or nfats == 0:
        return None
    if not (0 < spc <= 128 and (spc & (spc - 1)) == 0):
        return None
    total = total32 if total16 == 0 else total16
    if total == 0:
        return None
    fat_size = fat32_size if fat16_size == 0 else fat16_size
    if fat_size == 0:
        return None
    dir_sectors = (raiz_entradas * 32 + bytes_setor - 1) // bytes_setor
    dados_ini = reservados + nfats * fat_size + dir_sectors
    clusters = (total - dados_ini) // spc
    if clusters <= 0:
        return None
    # Discriminação FAT12/FAT16 x FAT32 conforme MS-FAT32: em FAT32 o número
    # de entradas da raiz é 0 e BPB_FATSz16 é 0 (usa-se BPB_FATSz32). Em
    # FAT12/16 esses campos são diferentes de zero. Os 4 bytes no deslocamento
    # 36 de um boot FAT16 pertencem a drive/serial, portanto NÃO servem para
    # distinguir o tipo.
    if raiz_entradas != 0 and fat16_size != 0:
        tipo = "fat16" if clusters >= 4085 else "fat12"
        if fat32_size == 0 and raiz_cluster >= 2 and clusters >= 65525:
            tipo = "fat32"     # boot híbrido inconsistente: confia no nº clusters
    else:
        tipo = "fat32" if (raiz_cluster >= 2 and fat32_size > 0) else "fat16"
    if tipo == "fat12":
        return None  # fora do escopo anunciado
    return {
        "tipo": tipo, "bytes_setor": bytes_setor, "spc": spc,
        "reservados": reservados, "nfats": nfats, "fat_size": fat_size,
        "raiz_entradas": raiz_entradas, "raiz_cluster": raiz_cluster,
        "total_setores": total, "backup_boot": backup,
        "clusters": clusters, "dados_inicio_setor": dados_ini,
        "rotulo": s[43:54].decode("latin-1").strip() if tipo == "fat16"
                else s[47:58].decode("latin-1").strip(),
    }


def _info_exfat(s: bytes):
    """Campos conforme MS-exFAT / driver Linux: BytesPerSectorShift=108,
    SectorsPerClusterShift=109, NumberOfFats=110, PercentInUse=112."""
    try:
        (offset_part,) = struct.unpack_from("<Q", s, 64)
        (total,) = struct.unpack_from("<Q", s, 72)
        (fat_off,) = struct.unpack_from("<I", s, 80)
        (fat_len,) = struct.unpack_from("<I", s, 84)
        (heap_off,) = struct.unpack_from("<I", s, 88)
        (n_clusters,) = struct.unpack_from("<I", s, 92)
        (raiz_cluster,) = struct.unpack_from("<I", s, 96)
        (serial,) = struct.unpack_from("<I", s, 100)
        bps_power = s[108]
        spc_power = s[109]
        n_fats = s[110]
        percent_in_use = s[112]
    except (struct.error, IndexError):
        return None
    if not (9 <= bps_power <= 12) or not (0 <= spc_power <= 25) or total == 0:
        return None
    if (1 << bps_power) * (1 << spc_power) > (32 << 20):
        return None  # cluster maior que 32 MiB: fora da especificação
    if n_clusters == 0 or n_clusters > 0xFFFFFFFF - 1:
        return None
    return {
        "tipo": "exfat", "bytes_setor": 1 << bps_power, "spc": 1 << spc_power,
        "offset_part": offset_part, "total_setores": total,
        "fat_off": fat_off, "fat_len": fat_len, "heap_off": heap_off,
        "n_clusters": n_clusters, "raiz_cluster": raiz_cluster,
        "serial": serial, "n_fats": n_fats,
        "percent_in_use": percent_in_use,
    }


def _info_ntfs(s: bytes):
    try:
        (bytes_setor,) = struct.unpack_from("<H", s, 11)
        (spc,) = struct.unpack_from("<B", s, 13)
        (total,) = struct.unpack_from("<Q", s, 40)
        (mft_cluster,) = struct.unpack_from("<Q", s, 48)
        (mftmirr_cluster,) = struct.unpack_from("<Q", s, 56)
        cpmft = struct.unpack_from("<b", s, 64)[0]
        cpidx = struct.unpack_from("<b", s, 68)[0]
        (serial,) = struct.unpack_from("<Q", s, 72)
    except struct.error:
        return None
    if bytes_setor not in (512, 1024, 2048, 4096) or spc == 0 or total == 0:
        return None
    rec_size = (1 << (-cpmft)) if cpmft < 0 else cpmft * spc * bytes_setor
    if rec_size not in (1024, 2048, 4096, 512, 8192, 16384):
        return None
    idx_size = (1 << (-cpidx)) if cpidx < 0 else cpidx * spc * bytes_setor
    return {
        "tipo": "ntfs", "bytes_setor": bytes_setor, "spc": spc,
        "total_setores": total, "mft_cluster": mft_cluster,
        "mftmirr_cluster": mftmirr_cluster, "rec_size": rec_size,
        "idx_size": idx_size, "serial": serial,
    }


def checksum_exfat(dados_11_setores: bytes, bytes_setor: int = 512) -> int:
    """Checksum de boot do exFAT: rotação de 16 bits sobre os setores 0..10,
    excluindo VolumeFlags (106-107) e PercentInUse (112) do setor 0."""
    n = 11 * bytes_setor
    p = 0
    for i in range(min(n, len(dados_11_setores))):
        if i in (106, 107, 112):
            continue
        p = (((p << 15) | (p >> 1)) & 0xFFFF)
        p = (p + dados_11_setores[i]) & 0xFFFF
    return p


# ---------------------------------------------------------------------------
# MBR / GPT / EBR
# ---------------------------------------------------------------------------


def _entradas_mbr(s: bytes):
    """Lista (bootavel, tipo, lba_inicio, n_setores) das 4 entradas."""
    ents = []
    for i in range(4):
        off = 446 + i * 16
        b = s[off]
        tipo = s[off + 4]
        lba, n = struct.unpack_from("<II", s, off + 8)
        ents.append((b == 0x80, tipo, lba, n))
    return ents


def ler_gpt(leitor: LeitorBase, lba: int = 1, setor: int = 512):
    """Lê e valida um cabeçalho GPT no LBA indicado. Retorna dict ou None."""
    dados = leitor.read(lba * setor, setor)
    if len(dados) < 92 or dados[:8] != b"EFI PART":
        return None
    (rev,) = struct.unpack_from("<I", dados, 8)
    (tam_cab,) = struct.unpack_from("<I", dados, 12)
    (crc_cab,) = struct.unpack_from("<I", dados, 16)
    (lba_atual,) = struct.unpack_from("<Q", dados, 24)
    (lba_backup,) = struct.unpack_from("<Q", dados, 32)
    (lba_inicio_uso,) = struct.unpack_from("<Q", dados, 40)
    (lba_fim_uso,) = struct.unpack_from("<Q", dados, 48)
    disco_guid = dados[56:72]
    (lba_tabela,) = struct.unpack_from("<Q", dados, 72)
    (n_entradas,) = struct.unpack_from("<I", dados, 80)
    (tam_entrada,) = struct.unpack_from("<I", dados, 84)
    (crc_tabela,) = struct.unpack_from("<I", dados, 88)
    if tam_cab < 92 or not (0 < n_entradas <= 512) or not (16 <= tam_entrada <= 4096):
        return None
    cab = bytearray(dados[:tam_cab])
    cab[16:20] = b"\x00\x00\x00\x00"
    cab += b"\x00" * (92 - len(cab))
    if binascii.crc32(bytes(cab[:92])) & 0xFFFFFFFF != crc_cab:
        return None
    tam_tab = n_entradas * tam_entrada
    tabela = leitor.read(lba_tabela * setor, tam_tab)
    if len(tabela) < tam_tab:
        return None
    if binascii.crc32(tabela) & 0xFFFFFFFF != crc_tabela:
        return None
    particoes = []
    for i in range(n_entradas):
        e = tabela[i * tam_entrada:(i + 1) * tam_entrada]
        guid_tipo = e[:16]
        if guid_tipo == b"\x00" * 16:
            continue
        lba_ini, lba_fim = struct.unpack_from("<QQ", e, 32)
        atrib = struct.unpack_from("<Q", e, 48)[0]
        nome = e[56:tam_entrada].decode("utf-16-le", "replace").rstrip("\x00")
        if lba_fim < lba_ini:
            continue
        particoes.append({
            "lba_ini": lba_ini, "lba_fim": lba_fim, "nome": nome,
            "guid_tipo": guid_tipo.hex(), "atrib": atrib,
        })
    return {
        "lba_atual": lba_atual, "lba_backup": lba_backup,
        "lba_inicio_uso": lba_inicio_uso, "lba_fim_uso": lba_fim_uso,
        "lba_tabela": lba_tabela, "disco_guid": disco_guid.hex(),
        "particoes": particoes, "valido": True, "setor": setor,
    }


def ler_mbr(leitor: LeitorBase, setor: int = 512):
    s = leitor.read(0, setor)
    if len(s) < setor or s[510:512] != b"\x55\xAA":
        return None
    return _entradas_mbr(s)


def detectar_particoes(leitor: LeitorBase, setor: int = 512,
                       buscar_perdidas: bool = False,
                       limite_perdidas: int | None = None,
                       progresso=None, cancelar=None) -> list[Particao]:
    """Detecta todas as partições/volumes do disco.

    Ordem: GPT primário -> GPT secundário (se primário inválido) -> MBR
    (+ EBRs de estendida) -> volume direto no início do disco (cartões sem
    tabela). Se nada for encontrado e buscar_perdidas=True, varre assinaturas.
    """
    achadas: list[Particao] = []
    tam_disco = leitor.tamanho

    def _tipo_por_boot(off: int, tam: int, indice: int, origem: str,
                        bootavel: bool = False, confiavel: bool = True):
        s = leitor.read(off, setor)
        t, info = identificar_boot(s)
        if t is None and setor == 512:
            # tenta setor de 4096
            s = leitor.read(off, 4096)
            t, info = identificar_boot(s)
            if t:
                setor_p = 4096
            else:
                setor_p = setor
        else:
            setor_p = setor
        if t is None:
            return None
        # valida contra cópias de reserva quando o tipo permite
        return Particao(off, tam, t, origem, indice, bootavel, setor_p, confiavel)

    gpt = ler_gpt(leitor, 1, setor)
    gpt_backup = None
    if gpt is None and tam_disco > 2 * setor:
        ultimo_lba = tam_disco // setor - 1
        if ultimo_lba > 1:
            gpt_backup = ler_gpt(leitor, ultimo_lba, setor)
            if gpt_backup:
                gpt = gpt_backup
                gpt["usado_backup"] = True
    if gpt:
        for i, p in enumerate(gpt["particoes"]):
            off = p["lba_ini"] * setor
            tam = (p["lba_fim"] - p["lba_ini"] + 1) * setor
            if off + tam > tam_disco + setor:
                continue
            part = _tipo_por_boot(off, tam, i, "gpt")
            if part:
                achadas.append(part)
        if achadas:
            if progresso:
                progresso(1.0, 1.0, "particoes")
            return achadas

    mbr = ler_mbr(leitor, setor)
    if mbr:
        estendida = None
        for i, (boot, tipo, lba, n) in enumerate(mbr):
            if tipo == 0:
                continue
            if tipo in TIPO_ESTENDIDA:
                estendida = (lba, n)
                continue
            off = lba * setor
            tam = n * setor
            if off == 0 or tam == 0 or off >= tam_disco:
                continue
            part = _tipo_por_boot(off, tam, i, "mbr", bootavel=boot)
            if part is None and tipo in (TIPOS_FAT_MBR | TIPOS_NTFS_MBR | TIPOS_LINUX):
                part = Particao(off, tam, "desconhecido", "mbr", i, boot, setor,
                                confiavel=False)
            if part:
                achadas.append(part)
        if estendida:
            achadas.extend(_ler_ebr(leitor, estendida, setor, tam_disco))

    if not achadas:
        # volume direto (cartões de câmera normalmente não têm tabela)
        part = _tipo_por_boot(0, tam_disco, 0, "volume")
        if part:
            achadas.append(part)

    if not achadas and buscar_perdidas:
        achadas.extend(buscar_particoes_perdidas(
            leitor, setor, limite_perdidas, progresso, cancelar))
    elif not achadas and setor == 512:
        # MBR apagado etc.: tenta automaticamente a busca de partições perdidas
        achadas.extend(buscar_particoes_perdidas(
            leitor, setor, limite_perdidas, progresso, cancelar, automatico=True))

    if progresso:
        progresso(1.0, 1.0, "particoes")
    return achadas


def _ler_ebr(leitor: LeitorBase, estendida, setor: int, tam_disco: int):
    """Percorre a cadeia de EBRs de uma partição estendida."""
    resultado = []
    lba_est, n_est = estendida
    lba_atual = lba_est
    vistos = set()
    for _ in range(128):  # limite de segurança contra cadeias cíclicas
        if lba_atual in vistos or lba_atual == 0:
            break
        vistos.add(lba_atual)
        off = lba_atual * setor
        if off >= tam_disco:
            break
        s = leitor.read(off, setor)
        if len(s) < setor or s[510:512] != b"\x55\xAA":
            break
        ents = _entradas_mbr(s)
        boot, tipo, lba, n = ents[0]
        if tipo and lba and n:
            p_off = (lba_est + lba) * setor if lba_atual != lba_est else (lba_est + lba) * setor
            # entradas de EBR são relativas ao início da estendida
            p_off = (lba_est + lba) * setor
            p_tam = n * setor
            if 0 < p_off < tam_disco:
                sb = leitor.read(p_off, setor)
                t, _info = identificar_boot(sb)
                resultado.append(Particao(p_off, p_tam, t or "desconhecido",
                                          "ebr", len(resultado), boot, setor,
                                          confiavel=t is not None))
        boot2, tipo2, lba2, n2 = ents[1]
        if tipo2 in TIPO_ESTENDIDA and lba2:
            lba_atual = lba_est + lba2
        else:
            break
    return resultado


# ---------------------------------------------------------------------------
# Busca por partições perdidas
# ---------------------------------------------------------------------------

OFFSETS_PROVAVEIS = [1 * 1024 * 1024, 63 * 512, 32 * 512, 8192 * 512,
                     2048 * 4096, 16065 * 512, 0]


def buscar_particoes_perdidas(leitor: LeitorBase, setor: int = 512,
                              limite: int | None = None, progresso=None,
                              cancelar=None, automatico: bool = False) -> list[Particao]:
    """Procura boots de volume em offsets prováveis e, se necessário, em uma
    varredura alinhada. Valida tamanho e coerência antes de aceitar."""
    achadas: list[Particao] = []
    tam_disco = leitor.tamanho
    vistos = set()

    def tentar(off: int):
        if off in vistos or off >= tam_disco:
            return
        vistos.add(off)
        s = leitor.read(off, 512)
        t, info = identificar_boot(s)
        if t is None:
            s = leitor.read(off, 4096)
            t, info = identificar_boot(s)
        if not t:
            return
        bs = info.get("bytes_setor", 512)
        n_setores = info.get("total_setores", 0)
        tam = n_setores * bs
        if tam <= 0 or off + tam > tam_disco + bs:
            return  # limites físicos incoerentes
        achadas.append(Particao(off, tam, t, "perdida", len(achadas),
                                setor=bs, confiavel=False))

    for off in OFFSETS_PROVAVEIS:
        tentar(off)

    if not achadas:
        # varredura ampla, alinhada em 512, amostrando offsets múltiplos de 512
        passo = 1024 * 1024
        max_off = tam_disco - 512 if tam_disco > 512 else 0
        off = 0
        while off <= max_off and (limite is None or len(achadas) < limite):
            if cancelar is not None and cancelar.is_set():
                break
            tentar(off)
            off += passo
            if progresso:
                progresso(min(1.0, off / max(max_off, 1)), 1.0, "particoes_perdidas")
    if achadas and progresso:
        progresso(1.0, 1.0, "particoes_perdidas")
    return achadas


def ler_boot_com_backup(leitor: LeitorBase, part: Particao):
    """Lê o boot do volume; se inválido, tenta as cópias de reserva.
    Retorna (setor_bytes, tipo, info, origem_boot)."""
    s = leitor.read(part.inicio, part.setor)
    t, info = identificar_boot(s)
    if t:
        return s, t, info, "primario"
    tam_disco = part.tamanho

    # FAT32: backup normalmente no setor 6
    for candidato in (6, 7, 8):
        off = part.inicio + candidato * part.setor
        if off + part.setor <= part.inicio + tam_disco:
            s = leitor.read(off, part.setor)
            t, info = identificar_boot(s)
            if t and t.startswith("fat"):
                return s, t, info, f"backup_setor_{candidato}"
    # exFAT: região de backup nos setores 12..23 (valida checksum no setor 23)
    off = part.inicio + 12 * part.setor
    dados = leitor.read(off, 12 * part.setor)
    if len(dados) >= 12 * part.setor and dados[3:11] == b"EXFAT   ":
        calc = checksum_exfat(dados[:11 * part.setor], part.setor)
        (arm,) = struct.unpack_from("<I", dados, 11 * part.setor)
        if calc == (arm & 0xFFFF):
            t, info = identificar_boot(dados[:part.setor])
            if t:
                return dados[:part.setor], t, info, "backup_exfat"
    # NTFS: cópia no último setor do volume
    ult = part.inicio + tam_disco - part.setor
    if ult > part.inicio:
        s = leitor.read(ult, part.setor)
        t, info = identificar_boot(s)
        if t == "ntfs":
            return s, t, info, "backup_ntfs"
    return None, None, {}, "nenhum"
