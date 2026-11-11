# -*- coding: utf-8 -*-
"""
exfat.py — Interpretador e recuperador de volumes exFAT.

Implementa:
  * Boot sector exFAT (campos de 64 bits, potência de setor/cluster).
  * FAT exFAT (respeitando o sinal NoFatChain dos streams).
  * Allocation bitmap (entrada 0x81) para saber o que está livre.
  * Entradas de diretório: File (0x85), Stream (0xC0), File Name (0xC1),
    versões apagadas (0x05/0x40/0x41), nomes Unicode UTF-16 e hash de nome.
  * Extensão contígua (NoFatChain) e fragmentada (seguindo a FAT).
  * Recuperação de arquivos apagados, inclusive após formatação rápida, com
    comportamento conservador: se a cadeia não está disponível e a região
    contígua contém clusters alocados pelo bitmap, o candidato é marcado como
    ambíguo (nunca declarado íntegro).

Somente leitura.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

from candidatos import (CandidatoArquivo, Fragmento, METODO_PASTA,
                        METODO_VARREDURA)

ENT_FILE = 0x85
ENT_FILE_DEL = 0x05
ENT_STREAM = 0xC0
ENT_STREAM_DEL = 0x40
ENT_NAME = 0xC1
ENT_NAME_DEL = 0x41
ENT_BITMAP = 0x81
ENT_BITMAP_DEL = 0x01
ENT_UPCASE = 0x82
ENT_TEXFAT = 0x81

FLAG_NO_FAT_CHAIN = 0x02
FLAG_IN_USE = 0x80

EPOCA_EXFAT = datetime(1980, 1, 1, tzinfo=timezone.utc)
MAX_CADEIA = 4 * 1024 * 1024


def exfat_para_datetime(v: int, offset_10ms: int = 0) -> datetime | None:
    if v == 0 or v == 0xFFFFFFFF:
        return None
    try:
        # bit alto: flag UTC (0x80000000) presente na especificação
        v2 = v & 0x7FFFFFFF
        dt = EPOCA_EXFAT + timedelta(seconds=v2)
        if offset_10ms and offset_10ms != 0xFF:
            dt += timedelta(milliseconds=10 * (offset_10ms & 0x7F))
        return dt.replace(tzinfo=None)
    except (OverflowError, ValueError):
        return None


class SistemaExFAT:
    def __init__(self, leitor, info: dict, offset_volume: int = 0,
                 tam_volume: int | None = None):
        self.leitor = leitor
        self.info = info
        self.tipo = "exfat"
        self.setor = info["bytes_setor"]
        self.spc = info["spc"]
        self.vol_off = offset_volume
        self.vol_tam = tam_volume
        self.fat_off = offset_volume + info["fat_off"] * self.setor
        self.fat_len = info["fat_len"] * self.setor
        self.heap_off = offset_volume + info["heap_off"] * self.setor
        self.n_clusters = info["n_clusters"]
        self.cluster_bytes = self.spc * self.setor
        self.raiz_cluster = info["raiz_cluster"]
        self._bitmap_bytes = None
        self._cache_fat = {}

    # --------------------------------------------------------------- FAT

    def _ler_fat(self, cluster: int) -> int | None:
        pag = cluster >> 10
        bloco = self._cache_fat.get(pag)
        if bloco is None:
            if len(self._cache_fat) > 64:
                self._cache_fat.clear()
            off = self.fat_off + pag * 4096
            if off + 4096 > self.fat_off + self.fat_len:
                tam = max(0, self.fat_off + self.fat_len - off)
                bloco = self.leitor.read(off, tam)
            else:
                bloco = self.leitor.read(off, 4096)
            self._cache_fat[pag] = bloco
        idx = (cluster % 1024) * 4
        if idx + 4 > len(bloco):
            return None
        return struct.unpack_from("<I", bloco, idx)[0] & 0x0FFFFFFF

    def offset_cluster(self, cluster: int) -> int:
        return self.heap_off + (cluster - 2) * self.cluster_bytes

    @property
    def fim_heap(self) -> int:
        return self.heap_off + self.n_clusters * self.cluster_bytes

    def ler_cadeia(self, primeiro: int, max_clusters: int = MAX_CADEIA) -> list[int]:
        cadeia = []
        vistos = set()
        c = primeiro
        while 2 <= c < self.n_clusters + 2 and len(cadeia) < max_clusters:
            if c in vistos:
                break
            vistos.add(c)
            cadeia.append(c)
            v = self._ler_fat(c)
            if v is None or v < 2 or v >= 0xFFFFFFF8:
                break
            c = v
        return cadeia

    # ------------------------------------------------------- bitmap

    def carregar_bitmap(self):
        """Localiza a allocation bitmap pela raiz (entradas 0x81)."""
        if self._bitmap_bytes is not None:
            return
        dados = self.diretorio_bytes(self.raiz_cluster, max_clusters=16)
        for i in range(0, len(dados) - 32, 32):
            e = dados[i:i + 32]
            if e[0] in (ENT_BITMAP, ENT_BITMAP_DEL):
                flags = e[1]
                primeiro = struct.unpack_from("<I", e, 20)[0]
                tam = struct.unpack_from("<Q", e, 8)[0]
                if primeiro < 2 or tam == 0 or tam > (self.n_clusters + 7) // 8:
                    continue
                cadeia = [primeiro]
                if not (flags & FLAG_NO_FAT_CHAIN):
                    cadeia = self.ler_cadeia(primeiro, (tam + self.cluster_bytes - 1) // self.cluster_bytes)
                buf = bytearray()
                for c in cadeia:
                    buf += self.leitor.read(self.offset_cluster(c), self.cluster_bytes)
                    if len(buf) >= tam:
                        break
                self._bitmap_bytes = bytes(buf[:tam])
                self._bitmap_no_fat = bool(flags & FLAG_NO_FAT_CHAIN)
                return
        self._bitmap_bytes = b""

    def cluster_alocado(self, cluster: int) -> bool | None:
        """True/False segundo o bitmap; None se desconhecido."""
        self.carregar_bitmap()
        idx = cluster - 2
        if idx < 0 or idx // 8 >= len(self._bitmap_bytes):
            return None
        return bool(self._bitmap_bytes[idx // 8] & (1 << (idx % 8)))

    # ------------------------------------------------- parsing de diretórios

    def _parsear_diretorio(self, dados: bytes, caminho: str,
                           incluir_vivos: bool = False, metodo=METODO_PASTA):
        conjuntos: list[bytes] = []
        atual: list[bytes] = []
        i = 0
        n = len(dados)
        while i + 32 <= n:
            e = dados[i:i + 32]
            t = e[0]
            if t == 0x00:
                if all(x == 0 for x in dados[i:i + min(512, n - i)]):
                    break
                if atual:
                    conjuntos.append(b"".join(atual))
                    atual = []
                i += 32
                continue
            apagado = t in (ENT_FILE_DEL, ENT_STREAM_DEL, ENT_NAME_DEL) or (
                t == 0x01)
            vivo = t in (ENT_FILE, ENT_STREAM, ENT_NAME, ENT_BITMAP, ENT_UPCASE)
            if not apagado and not vivo:
                if atual:
                    conjuntos.append(b"".join(atual))
                    atual = []
                i += 32
                continue
            if t in (ENT_FILE, ENT_FILE_DEL):
                if atual:
                    conjuntos.append(b"".join(atual))
                atual = [e]
            else:
                atual.append(e)
            i += 32
        if atual:
            conjuntos.append(b"".join(atual))

        for conj in conjuntos:
            resultado = self._interpretar_conjunto(conj, caminho, incluir_vivos, metodo)
            if resultado is not None:
                yield resultado

    def _interpretar_conjunto(self, conj: bytes, caminho: str,
                              incluir_vivos: bool, metodo: str):
        if not conj:
            return None
        cab = conj[0:32]
        if cab[0] not in (ENT_FILE, ENT_FILE_DEL):
            return None
        apagado = cab[0] == ENT_FILE_DEL
        n_entradas = struct.unpack_from("<H", cab, 2)[0]
        if n_entradas < 2 or n_entradas > 255 or len(conj) < 64:
            return None
        attr = struct.unpack_from("<H", cab, 4)[0]
        t_mod = struct.unpack_from("<I", cab, 8)[0]
        off_10ms = cab[11]
        stream = conj[32:64]
        if stream[0] not in (ENT_STREAM, ENT_STREAM_DEL):
            return None
        flags = stream[1]
        tam_nome = stream[3]
        nome_hash = struct.unpack_from("<H", stream, 4)[0]
        val_len = struct.unpack_from("<Q", stream, 8)[0]
        primeiro = struct.unpack_from("<I", stream, 20)[0]
        data_len = struct.unpack_from("<Q", stream, 24)[0]
        if attr & 0x10:  # diretório
            return ("dir", primeiro, apagado, flags)
        if not apagado and not incluir_vivos:
            return None
        if val_len == 0 or data_len == 0 or primeiro < 2:
            return None
        if val_len > data_len or data_len > (1 << 42):
            return None
        nomes = []
        completo = True
        # conj = File + n_entradas secundárias (Stream + nomes)
        for k in range(2, min(n_entradas + 1, len(conj) // 32)):
            ent = conj[k * 32:(k + 1) * 32]
            if ent[0] not in (ENT_NAME, ENT_NAME_DEL):
                completo = False
                break
            txt = ent[4:32].decode("utf-16-le", "replace")
            nomes.append(txt.rstrip("\x00"))
        nome = "".join(nomes).strip()
        if not nome:
            return None
        nome = "".join(ch if ch.isprintable() else "_" for ch in nome)
        # hash do nome (validação fraca, apenas diagnóstico)
        hash_ok = None
        if nome_hash and completo:
            hash_ok = (self._hash_nome(nome) == nome_hash)
        dt = exfat_para_datetime(t_mod, off_10ms)
        frag, motivo, confiavel = self._localizar(primeiro, data_len, flags, apagado)
        if frag is None:
            return None
        cand = CandidatoArquivo(
            nome, frag, metodo, "exfat",
            caminho_original=(caminho + "\\" + nome).strip("\\"),
            tamanho=val_len, data=dt,
            confiavel=confiavel, motivo=motivo,
            extra={"apagado": apagado, "cluster": primeiro,
                   "no_fat_chain": bool(flags & FLAG_NO_FAT_CHAIN),
                   "hash_nome_ok": hash_ok})
        return ("arq", cand)

    @staticmethod
    def _hash_nome(nome: str) -> int:
        h = 0
        for ch in nome.upper().encode("utf-16-le"):
            h = (((h << 15) | (h >> 1)) & 0xFFFF)
            h = (h + ch) & 0xFFFF
        return h

    def _localizar(self, primeiro: int, data_len: int, flags: int, apagado: bool):
        if primeiro < 2 or primeiro >= self.n_clusters + 2:
            return None, "cluster inicial inválido", False
        clusters_nec = (data_len + self.cluster_bytes - 1) // self.cluster_bytes
        no_fat = bool(flags & FLAG_NO_FAT_CHAIN)

        if not no_fat:
            # cadeia FAT disponível (arquivo vivo, ou apagado cuja cadeia
            # sobreviveu à exclusão em arquivo fragmentado)
            cadeia = self.ler_cadeia(primeiro, clusters_nec + 16)
            if len(cadeia) >= clusters_nec:
                frag = self._cadeia_para_fragmentos(cadeia, data_len)
                if sum(f.tamanho for f in frag) >= data_len:
                    if apagado:
                        ambiguo = [c for c in cadeia[:clusters_nec]
                                   if self.cluster_alocado(c) is True]
                        if ambiguo:
                            return frag, (
                                f"clusters da cadeia realocados no bitmap "
                                f"(ex.: {ambiguo[0]}); dados podem estar "
                                "sobrescritos"), False
                        return frag, "cadeia FAT sobrevivente à exclusão", True
                    return frag, "", True
            if not apagado:
                return None, "cadeia FAT incompleta", False

        # apagado ou contíguo: região a partir do primeiro cluster
        off = self.offset_cluster(primeiro)
        if off >= self.fim_heap:
            return None, "cluster fora do heap", False
        tam = min(data_len, self.fim_heap - off)
        ambiguo = []
        for k in range(clusters_nec):
            c = primeiro + k
            if c >= self.n_clusters + 2:
                break
            aloc = self.cluster_alocado(c)
            if aloc is True:
                ambiguo.append(c)
                break
        if tam < data_len:
            return [Fragmento(off, tam)], f"heap insuficiente ({tam}/{data_len})", False
        if ambiguo:
            return ([Fragmento(off, tam)],
                    f"clusters contíguos realocados no bitmap (a partir de {ambiguo[0]}); "
                    "localização ambígua", False)
        motivo = "" if no_fat or apagado else "NoFatChain ausente; contiguidade assumida"
        return [Fragmento(off, tam)], motivo, True

    def _cadeia_para_fragmentos(self, cadeia: list[int], data_len: int) -> list[Fragmento]:
        frags = []
        resto = data_len
        i = 0
        while i < len(cadeia) and resto > 0:
            j = i
            while (j + 1 < len(cadeia) and cadeia[j + 1] == cadeia[j] + 1
                   and resto > (j + 1 - i) * self.cluster_bytes):
                j += 1
            tam = min(resto, (j - i + 1) * self.cluster_bytes)
            frags.append(Fragmento(self.offset_cluster(cadeia[i]), tam))
            resto -= tam
            i = j + 1
        return frags

    # -------------------------------------------------------------- público

    def diretorio_bytes(self, cluster: int, max_clusters: int = 512) -> bytes:
        cadeia = self.ler_cadeia(cluster, max_clusters)
        if not cadeia or len(cadeia) == 1:
            cadeia = [cluster]
        dados = bytearray()
        for c in cadeia[:max_clusters]:
            off = self.offset_cluster(c)
            if off + self.cluster_bytes > self.fim_heap:
                break
            dados += self.leitor.read(off, self.cluster_bytes)
        return bytes(dados)

    def recuperar(self, cancelar=None, incluir_vivos: bool = False,
                  profunda: bool = False, progresso=None) -> list[CandidatoArquivo]:
        self.carregar_bitmap()
        achados: list[CandidatoArquivo] = []
        fila: list[tuple[str, int]] = [("", self.raiz_cluster)]
        vistos = set()
        limite_dirs = 4096
        n_dirs = 0
        while fila and n_dirs < limite_dirs:
            caminho, cluster = fila.pop(0)
            n_dirs += 1
            if cancelar is not None and cancelar.is_set():
                break
            if cluster in vistos or not (2 <= cluster < self.n_clusters + 2):
                continue
            vistos.add(cluster)
            dados = self.diretorio_bytes(cluster)
            for item in self._parsear_diretorio(dados, caminho, incluir_vivos):
                if item is None:
                    continue
                if item[0] == "dir":
                    _t, cl_dir, _apag, _flags = item
                    if cl_dir not in vistos and 2 <= cl_dir < self.n_clusters + 2:
                        fila.append((caminho + f"\\dir{cl_dir}", cl_dir))
                else:
                    achados.append(item[1])
        if profunda:
            achados.extend(self.varredura_dirs_orfaos(cancelar, vistos, progresso))
        return achados

    def varredura_dirs_orfaos(self, cancelar=None, vistos=None, progresso=None):
        """Procura conjuntos File/Stream/Name apagados no heap (restos após
        formatação). Método 'varredura'; resultados conservadores."""
        vistos = vistos or set()
        achados = []
        bloco = 8 * 1024 * 1024
        off = self.heap_off
        fim = self.fim_heap
        while off < fim:
            if cancelar is not None and cancelar.is_set():
                break
            dados = self.leitor.read(off, min(bloco, fim - off))
            for i in range(0, len(dados) - 96, 32):
                if dados[i] != ENT_FILE_DEL:
                    continue
                if dados[i + 32] != ENT_STREAM_DEL:
                    continue
                if dados[i + 64] not in (ENT_NAME_DEL, ENT_NAME):
                    continue
                n_ent = struct.unpack_from("<H", dados, i + 2)[0]
                if not (2 <= n_ent <= 32):
                    continue
                conj = dados[i:i + 32 * n_ent]
                if len(conj) < 32 * n_ent:
                    continue
                res = self._interpretar_conjunto(conj, "", False, METODO_VARREDURA)
                if res and res[0] == "arq":
                    cand = res[1]
                    cand.confiavel = False
                    cand.motivo = (cand.motivo + "; " if cand.motivo else "") + \
                        "conjunto órfão encontrado por varredura"
                    achados.append(cand)
            off += len(dados) or bloco
            if progresso:
                progresso(min(1.0, off / max(fim, 1)), 1.0, "varredura_exfat")
        del vistos
        return achados
