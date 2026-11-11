# -*- coding: utf-8 -*-
"""
fatfs.py — Interpretador e recuperador de sistemas de arquivos FAT16/FAT32.

Implementa:
  * Boot sector / BPB (validado por particoes.identificar_boot).
  * Leitura da FAT (16 e 32 bits) e de cadeias de clusters.
  * Entradas de diretório de 32 bytes, nomes longos (LFN) e checksum LFN.
  * Recuperação de arquivos apagados (entrada 0xE5): quando a cadeia da FAT foi
    zerada, assume alocação contígua a partir do primeiro cluster e marca como
    ambíguo (não confiável) qualquer trecho cujo cluster esteja alocado por
    outro arquivo — dados ambíguos nunca são declarados íntegros.
  * Diretórios apagados e órfãos (inclusive após formatação rápida, via
    varredura profunda do heap de clusters à procura de clusters de diretório).
  * Uso de metadados: nome original, extensão, tamanho, data e localização.

Somente leitura: nenhuma estrutura da mídia é alterada.
"""

from __future__ import annotations

import struct
from datetime import datetime

from candidatos import (CandidatoArquivo, Fragmento, METODO_PASTA,
                        METODO_VARREDURA, dos_para_datetime)
from particoes import _info_fat

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = 0x0F
ATTR_LONG_NAME_MASK = 0x3F

FAT16_LIVRE = 0x0000
FAT16_FIM = 0xFFF8
FAT32_LIVRE = 0x00000000
FAT32_FIM = 0x0FFFFFF8
FAT16_BAD = 0x0FF7
FAT32_BAD = 0x0FFFFFF7

MAX_CADEIA = 4 * 1024 * 1024  # limite defensivo de clusters por cadeia


class SistemaFAT:
    def __init__(self, leitor, info: dict, offset_volume: int = 0,
                 tam_volume: int | None = None):
        self.leitor = leitor
        self.info = info
        self.tipo = info["tipo"]               # 'fat16' | 'fat32'
        self.setor = info["bytes_setor"]
        self.spc = info["spc"]
        self.vol_off = offset_volume
        self.vol_tam = tam_volume
        self.fat_off = offset_volume + info["reservados"] * self.setor
        self.fat_bytes = info["fat_size"] * self.setor
        self.nfats = info["nfats"]
        self.dados_off = offset_volume + info["dados_inicio_setor"] * self.setor
        self.clusters = info["clusters"]
        self.cluster_bytes = self.spc * self.setor
        self.raiz_cluster = info.get("raiz_cluster", 0)
        self.raiz_entradas = info.get("raiz_entradas", 0)
        self._dir_raiz16_off = self.fat_off + self.nfats * self.fat_bytes
        self._dir_raiz16_tam = self.raiz_entradas * 32
        self._cache_fat = {}

    # ------------------------------------------------------------------ FAT

    def _ler_valor_fat(self, cluster: int) -> int | None:
        pag = cluster >> 10
        bloco = self._cache_fat.get(pag)
        if bloco is None:
            if len(self._cache_fat) > 64:
                self._cache_fat.clear()
            off = self.fat_off + (pag * 1024 * (4 if self.tipo == "fat32" else 2))
            tam = 1024 * (4 if self.tipo == "fat32" else 2)
            bloco = self.leitor.read(off, tam)
            self._cache_fat[pag] = bloco
        if self.tipo == "fat32":
            idx = (cluster % 1024) * 4
            if idx + 4 > len(bloco):
                return None
            return struct.unpack_from("<I", bloco, idx)[0] & 0x0FFFFFFF
        idx = (cluster % 1024) * 2
        if idx + 2 > len(bloco):
            return None
        return struct.unpack_from("<H", bloco, idx)[0]

    def cluster_livre(self, cluster: int) -> bool:
        v = self._ler_valor_fat(cluster)
        return v is not None and v == (FAT16_LIVRE if self.tipo == "fat16" else FAT32_LIVRE)

    def _e_fim(self, v: int) -> bool:
        return v >= (FAT16_FIM if self.tipo == "fat16" else FAT32_FIM)

    def _e_bad(self, v: int) -> bool:
        return v == (FAT16_BAD if self.tipo == "fat16" else FAT32_BAD)

    def offset_cluster(self, cluster: int) -> int:
        return self.dados_off + (cluster - 2) * self.cluster_bytes

    def ler_cadeia(self, primeiro: int, max_clusters: int = MAX_CADEIA) -> list[int]:
        """Segue a cadeia de clusters pela FAT."""
        cadeia = []
        vistos = set()
        c = primeiro
        while 2 <= c < self.clusters + 2 and len(cadeia) < max_clusters:
            if c in vistos:
                break
            vistos.add(c)
            cadeia.append(c)
            v = self._ler_valor_fat(c)
            if v is None or v == 0 or self._e_fim(v) or self._e_bad(v):
                break
            c = v
        return cadeia

    # ------------------------------------------------------------- leitura

    def ler_area(self, off: int, tam: int) -> bytes:
        return self.leitor.read(off, tam)

    # -------------------------------------------------- análise de entradas

    @staticmethod
    def _nome_curto(entrada: bytes) -> str:
        bruto = entrada[0:11]
        base = bruto[0:8].rstrip(b" ").decode("latin-1")
        ext = bruto[8:11].rstrip(b" ").decode("latin-1")
        base = base.replace("\x05", "\xe5")
        return f"{base}.{ext}" if ext else base

    @staticmethod
    def _chars_lfn(entrada: bytes) -> str:
        chars = []
        for faixa in ((1, 11), (14, 26), (28, 32)):
            for i in range(faixa[0], faixa[1], 2):
                c = struct.unpack_from("<H", entrada, i)[0]
                if c == 0x0000 or c == 0xFFFF:
                    return "".join(chars)
                chars.append(chr(c))
        return "".join(chars)

    @staticmethod
    def _checksum_lfn(nome_curto_11: bytes) -> int:
        s = 0
        for b in nome_curto_11:
            s = ((s >> 1) | ((s & 1) << 7)) & 0xFF
            s = (s + b) & 0xFF
        return s

    def _data_entrada(self, e: bytes) -> datetime | None:
        t, d = struct.unpack_from("<HH", e, 22)
        dt = dos_para_datetime(d, t)
        if dt is None:
            d2 = struct.unpack_from("<H", e, 18)[0]
            dt = dos_para_datetime(d2, 0)
        return dt

    def _parsear_diretorio(self, dados: bytes, caminho: str,
                           deletados: bool = True, incluir_vivos: bool = False,
                           metodo: str = METODO_PASTA):
        """Gera candidatos a partir do conteúdo bruto de um diretório."""
        lfn_grupo: list[tuple[int, bytes]] = []
        i = 0
        n = len(dados)
        while i + 32 <= n:
            e = dados[i:i + 32]
            b0 = e[0]
            attr = e[11]
            if b0 == 0x00:
                # resto do diretório vazio
                if all(x == 0 for x in dados[i:i + min(512, n - i)]):
                    break
                lfn_grupo = []
                i += 32
                continue
            if (attr & ATTR_LONG_NAME_MASK) == ATTR_LONG_NAME:
                seq = b0 & 0x3F if b0 != 0xE5 else -1
                lfn_grupo.append((seq, e))
                i += 32
                continue
            if b0 == 0xE5 or incluir_vivos:
                apagado = b0 == 0xE5
                if attr & ATTR_VOLUME_ID or attr & ATTR_LONG_NAME_MASK == ATTR_LONG_NAME:
                    lfn_grupo = []
                    i += 32
                    continue
                primeiro_hi = struct.unpack_from("<H", e, 20)[0]
                primeiro_lo = struct.unpack_from("<H", e, 26)[0]
                tamanho = struct.unpack_from("<I", e, 28)[0]
                cluster = (primeiro_hi << 16) | primeiro_lo
                nome = self._montar_nome(lfn_grupo, e, apagado)
                lfn_grupo = []
                i += 32
                if attr & ATTR_DIRECTORY:
                    if cluster < 2 or cluster >= self.clusters + 2:
                        continue
                    yield ("dir", nome, cluster, apagado, caminho)
                    continue
                if not apagado and not incluir_vivos:
                    continue
                if cluster < 2 or tamanho == 0 or tamanho > (1 << 40):
                    continue
                dt = self._data_entrada(e)
                frag, motivo, confiavel = self._localizar_dados(cluster, tamanho, apagado)
                if frag is None:
                    continue
                yield ("arq", CandidatoArquivo(
                    nome, frag, metodo, self.tipo,
                    caminho_original=(caminho + "\\" + nome).strip("\\"),
                    tamanho=tamanho, data=dt, confiavel=confiavel, motivo=motivo,
                    extra={"apagado": apagado, "cluster": cluster}), )
            else:
                lfn_grupo = []
                i += 32

    def _montar_nome(self, grupo: list[tuple[int, bytes]], entrada: bytes,
                     apagado: bool) -> str:
        if grupo:
            # entradas LFN aparecem em ordem n..1 no diretório
            partes = []
            for _seq, e in reversed(grupo):
                partes.append(self._chars_lfn(e))
            nome = "".join(partes).rstrip("\x00").strip()
            if nome:
                if not apagado:
                    curto11 = bytearray(entrada[0:11])
                    chk_grupo = grupo[-1][1][13]
                    if chk_grupo == self._checksum_lfn(bytes(curto11)):
                        return nome
                    return nome  # checksum divergente: mantém LFN, motor registra
                return nome
        nome_curto = self._nome_curto(entrada)
        if apagado:
            nome_curto = "?" + nome_curto[1:]
        return nome_curto

    def _localizar_dados(self, primeiro: int, tamanho: int, apagado: bool):
        """Retorna (fragmentos, motivo, confiavel). Para arquivos vivos segue a
        FAT; para apagados, assume contiguidade e confere se os clusters do
        trecho continuam livres (ambiguidade => não confiável)."""
        if primeiro < 2 or primeiro >= self.clusters + 2:
            return None, "cluster inicial inválido", False
        clusters_nec = (tamanho + self.cluster_bytes - 1) // self.cluster_bytes
        if clusters_nec <= 0:
            return None, "tamanho zero", False

        if not apagado:
            cadeia = self.ler_cadeia(primeiro, max(clusters_nec, 1) + 16)
            if not cadeia:
                return None, "cadeia de clusters ilegível", False
            frag = self._cadeia_para_fragmentos(cadeia, tamanho)
            total_frag = sum(f.tamanho for f in frag)
            motivo = ""
            if total_frag < tamanho:
                motivo = f"cadeia cobre {total_frag} de {tamanho} bytes"
            return frag, motivo, total_frag >= tamanho

        # apagado: em geral a FAT foi zerada, mas a cadeia pode ter
        # sobrevivido (exclusão que removeu só a entrada, ou formatação
        # rápida parcial). Tenta a cadeia antes de assumir contiguidade.
        cadeia = self.ler_cadeia(primeiro, clusters_nec + 16)
        if len(cadeia) > 1:
            frag = self._cadeia_para_fragmentos(cadeia, tamanho)
            coberto = sum(f.tamanho for f in frag)
            realocados = [c for c in cadeia[:clusters_nec]
                          if not self.cluster_livre(c)]
            if coberto >= tamanho and not realocados:
                return frag, "cadeia FAT sobrevivente à exclusão", True
            motivo = ""
            if coberto < tamanho:
                motivo = f"cadeia FAT parcial ({coberto} de {tamanho} bytes)"
            if realocados:
                motivo = (motivo + "; " if motivo else "") + (
                    f"clusters da cadeia realocados (ex.: {realocados[0]}); "
                    "dados podem estar sobrescritos")
            if coberto > 0:
                return frag, motivo, False

        # cadeia ausente/zerada: contíguo a partir do primeiro cluster
        off = self.offset_cluster(primeiro)
        fim_area = self.dados_off + self.clusters * self.cluster_bytes
        if off >= fim_area:
            return None, "cluster inicial fora da área de dados", False
        disponivel = fim_area - off
        tam_efetivo = min(tamanho, disponivel)
        ambiguo = []
        limite_verif = min(clusters_nec, 4096)
        for c in range(primeiro + 1, primeiro + limite_verif):
            if c >= self.clusters + 2:
                break
            if not self.cluster_livre(c):
                ambiguo.append(c)
                break
        if tam_efetivo < tamanho:
            return ([Fragmento(off, tam_efetivo)],
                    f"tamanho excede a área de dados ({tam_efetivo}/{tamanho})", False)
        if ambiguo:
            return ([Fragmento(off, tam_efetivo)],
                    f"clusters seguintes realocados (a partir de {ambiguo[0]}); "
                    "arquivo pode ter estado fragmentado ou sobrescrito", False)
        return [Fragmento(off, tam_efetivo)], (
            f"cadeia da FAT zerada pela exclusão; conteúdo contíguo a partir do "
            f"cluster {primeiro} e clusters seguintes livres"), True

    def _cadeia_para_fragmentos(self, cadeia: list[int], tamanho: int) -> list[Fragmento]:
        frags = []
        resto = tamanho
        i = 0
        while i < len(cadeia) and resto > 0:
            ini = cadeia[i]
            j = i
            while (j + 1 < len(cadeia) and cadeia[j + 1] == cadeia[j] + 1
                   and resto > (j + 1 - i) * self.cluster_bytes):
                j += 1
            n_clusters = j - i + 1
            tam = min(resto, n_clusters * self.cluster_bytes)
            frags.append(Fragmento(self.offset_cluster(ini), tam))
            resto -= tam
            i = j + 1
        return frags

    # ------------------------------------------------------------- público

    def raiz_bytes(self) -> bytes:
        if self.tipo == "fat16":
            return self.ler_area(self._dir_raiz16_off, self._dir_raiz16_tam)
        cadeia = self.ler_cadeia(self.raiz_cluster)
        dados = bytearray()
        for c in cadeia:
            dados += self.ler_area(self.offset_cluster(c), self.cluster_bytes)
        return bytes(dados)

    def diretorio_bytes(self, cluster: int, max_clusters: int = 1024) -> bytes:
        cadeia = self.ler_cadeia(cluster, max_clusters)
        if not cadeia:
            # diretório apagado: FAT zerada, assume contíguo (limitado)
            cadeia = [cluster]
        dados = bytearray()
        for c in cadeia:
            off = self.offset_cluster(c)
            if off + self.cluster_bytes > self.dados_off + self.clusters * self.cluster_bytes:
                break
            dados += self.ler_area(off, self.cluster_bytes)
            if len(dados) >= max_clusters * self.cluster_bytes:
                break
        return bytes(dados)

    def recuperar(self, cancelar=None, incluir_vivos: bool = False,
                  profunda: bool = False, progresso=None) -> list[CandidatoArquivo]:
        """Recupera candidatos por metadados, percorrendo a árvore de
        diretórios (incluindo apagados) a partir da raiz."""
        achados: list[CandidatoArquivo] = []
        vistos_clusters = set()
        fila: list[tuple[str, int | None, bool]] = [("", None, False)]  # (caminho, cluster, deletado)
        limite_dirs = 4096
        n_dirs = 0
        while fila and n_dirs < limite_dirs:
            caminho, cluster, _del = fila.pop(0)
            n_dirs += 1
            if cancelar is not None and cancelar.is_set():
                break
            if cluster is None:
                dados = self.raiz_bytes()
            else:
                if cluster in vistos_clusters:
                    continue
                vistos_clusters.add(cluster)
                dados = self.diretorio_bytes(cluster)
            for item in self._parsear_diretorio(dados, caminho,
                                                incluir_vivos=incluir_vivos):
                if item[0] == "dir":
                    _t, nome, cl_dir, apagado, _cam = item
                    if cl_dir not in vistos_clusters and 2 <= cl_dir < self.clusters + 2:
                        sub = (caminho + "\\" + nome).strip("\\")
                        fila.append((sub, cl_dir, apagado))
                else:
                    achados.append(item[1])
        if profunda:
            achados.extend(self.varredura_dirs_orfaos(cancelar, vistos_clusters,
                                                      progresso))
        return achados

    # --------------------------------------------- varredura profunda (FAT)

    def varredura_dirs_orfaos(self, cancelar=None, vistos=None, progresso=None):
        """Procura clusters de diretório órfãos no heap (restos de metadados
        após formatação). Método 'varredura', resultados conservadores."""
        vistos = vistos or set()
        achados = []
        bloco_clusters = max(1, (8 * 1024 * 1024) // self.cluster_bytes)
        total = self.clusters
        c = 2
        max_off = self.dados_off + total * self.cluster_bytes
        while c < total + 2:
            if cancelar is not None and cancelar.is_set():
                break
            n = min(bloco_clusters, total + 2 - c)
            off = self.offset_cluster(c)
            dados = self.leitor.read(off, n * self.cluster_bytes)
            for k in range(n):
                cl = c + k
                if cl in vistos:
                    continue
                chunk = dados[k * self.cluster_bytes:(k + 1) * self.cluster_bytes]
                if self._parece_diretorio_apagado(chunk):
                    vistos.add(cl)
                    for item in self._parsear_diretorio(chunk, "", metodo=METODO_VARREDURA):
                        if item[0] == "arq":
                            cand = item[1]
                            cand.confiavel = False
                            cand.motivo = (cand.motivo + "; " if cand.motivo else "") + \
                                "diretório órfão encontrado por varredura"
                            achados.append(cand)
            c += n
            if progresso:
                progresso(min(1.0, (off + len(dados)) / max(max_off, 1)), 1.0,
                          "varredura_fat")
        return achados

    @staticmethod
    def _parece_diretorio_apagado(chunk: bytes) -> bool:
        """Heurística: pelo menos uma entrada 0xE5 plausível e nenhuma entrada
        claramente inválida nos primeiros 512 bytes."""
        if len(chunk) < 32:
            return False
        apagadas = 0
        validas = 0
        limite = min(len(chunk), 1024)
        for i in range(0, limite, 32):
            e = chunk[i:i + 32]
            b0 = e[0]
            attr = e[11]
            if b0 == 0x00:
                continue
            if attr & ~0x3F and (attr & ATTR_LONG_NAME_MASK) != ATTR_LONG_NAME:
                return False
            if b0 == 0xE5:
                if attr == 0 or (attr & ATTR_VOLUME_ID):
                    return False
                apagadas += 1
                # tamanho plausível para arquivo
                if not (attr & ATTR_DIRECTORY):
                    tam = struct.unpack_from("<I", e, 28)[0]
                    if tam > (1 << 31):
                        return False
                    c_hi, c_lo = struct.unpack_from("<HH", e, 20)
                    if c_lo < 2 and tam > 0:
                        return False
                    del c_hi
            elif 0x20 <= b0 < 0xFF or b0 == 0x05:
                validas += 1
            else:
                return False
        return apagadas >= 2 and apagadas + validas >= 3


def criar_desde_boot(leitor, setor_boot: bytes, offset_volume: int = 0,
                     tam_volume: int | None = None) -> SistemaFAT | None:
    info = _info_fat(setor_boot)
    if not info:
        return None
    return SistemaFAT(leitor, info, offset_volume, tam_volume)
