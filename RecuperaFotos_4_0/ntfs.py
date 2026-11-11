"""
ntfs.py — Interpretador e recuperador de volumes NTFS.

Implementa:
  * Boot sector NTFS e cópia de reserva no último setor do volume.
  * Master File Table: registros FILE, fixup arrays (USA), validação de
    estrutura, flags de uso e números de sequência.
  * Atributos residentes e não residentes: STANDARD_INFORMATION, FILE_NAME,
    DATA, ATTRIBUTE_LIST (extensões em outros registros), INDEX_ROOT.
  * Data runs: extents fragmentados, sparse e unidades de compressão.
  * Descompressão LZNT1 (atributos comprimidos).
  * Reconstrução de caminhos via referência ao diretório pai.
  * Recuperação de registros apagados e de MFT órfã (registros FILE antigos
    após formatação rápida, em áreas não sobrescritas), com validação estrita
    e comportamento conservador (verificação do $Bitmap para clusters
    realocados).
  * Registro de recusas com motivos para diagnostico_ntfs.csv.

Somente leitura.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta

from candidatos import METODO_MFT, METODO_MFT_ORFA, CandidatoArquivo, Fragmento
from particoes import _info_ntfs

# Tipos de atributo
ATTR_SI = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_OBJECT_ID = 0x40
ATTR_VOLUME_NAME = 0x60
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_BITMAP = 0xB0
ATTR_REPARSE = 0xC0
ATTR_FIM = 0xFFFFFFFF

EPOCA_NTFS = datetime(1601, 1, 1)
MAX_ATRIBUTOS = 512
MAX_RUNS = 1000000
MAX_CADEIA_PAIS = 64
REC_SIZE_MAX = 64 * 1024


def ntfs_para_datetime(v: int) -> datetime | None:
    if v == 0 or v >= (1 << 63):
        return None
    try:
        dt = EPOCA_NTFS + timedelta(microseconds=v // 10)
        if dt.year > 2200:
            return None
        return dt
    except (OverflowError, ValueError):
        return None


def aplicar_fixup(rec: bytearray, usa_ofs: int, usa_cnt: int,
                  setor: int = 512) -> bool:
    """Aplica o fixup array (USA) e valida o registro. Retorna False se o
    registro estiver corrompido (valor de fixup divergente)."""
    if usa_ofs + usa_cnt * 2 > len(rec) or usa_cnt < 1:
        return False
    original = struct.unpack_from("<H", rec, usa_ofs)[0]
    n_sectors = usa_cnt - 1
    for i in range(n_sectors):
        pos = (i + 1) * setor - 2     # últimos 2 bytes de cada setor
        if pos + 2 > len(rec) or pos < 0:
            return False
        if struct.unpack_from("<H", rec, pos)[0] != original:
            return False
        struct.pack_into("<H", rec, pos,
                         struct.unpack_from("<H", rec, usa_ofs + 2 + i * 2)[0])
    return True


class RunList:
    """Lista de data runs de um atributo não residente."""

    def __init__(self, runs: list[tuple[int, int, bool]]):
        # cada run: (lcn, n_clusters, sparse); lcn=-1 quando sparse
        self.runs = runs

    @staticmethod
    def parsear(dados: bytes, max_runs: int = MAX_RUNS) -> RunList | None:
        runs = []
        i = 0
        while i < len(dados) and len(runs) < max_runs:
            cab = dados[i]
            if cab == 0:
                break
            # convenção real do NTFS (MS-FNTFS 2.3.1 "Data Runs"):
            #   nibble BAIXO  = nº de bytes do campo LENGTH (nº de clusters)
            #   nibble ALTO   = nº de bytes do campo OFFSET (delta de LCN, com sinal)
            # offset com 0 bytes => run SPARSE (buraco, sem clusters físicos)
            tam_len = cab & 0x0F
            tam_off = (cab >> 4) & 0x0F
            i += 1
            if tam_len == 0 or i + tam_len + tam_off > len(dados):
                break
            n = int.from_bytes(dados[i:i + tam_len], "little")
            i += tam_len
            if tam_off == 0:
                if n == 0:
                    continue        # run degenerado: ignora
                runs.append((-1, n, True))  # sparse (sem clusters físicos)
                continue
            off_bruto = int.from_bytes(dados[i:i + tam_off], "little", signed=True)
            i += tam_off
            if n == 0:
                # run degenerado: ignora, mas o campo de offset JÁ foi consumido
                # (senão o parser perde a sincronia com os runs seguintes)
                continue
            if runs and runs[-1][0] >= 0:
                lcn = runs[-1][0] + off_bruto
            else:
                lcn = off_bruto
            if lcn < 0 and off_bruto >= 0:
                break
            runs.append((lcn, n, False))
        return RunList(runs)

    def total_clusters(self) -> int:
        return sum(n for _l, n, _s in self.runs)

    def fragmentos(self, offset_base: int, cluster_bytes: int,
                   tamanho: int | None = None) -> list[Fragmento]:
        """Converte runs em fragmentos físicos, mesclando runs contíguos.
        Runs sparse são descartadas (não há dados físicos)."""
        frags = []
        pos_vcn = 0
        for lcn, n, sparse in self.runs:
            if sparse or lcn < 0:
                pos_vcn += n
                continue
            if frags and frags[-1].offset + frags[-1].tamanho == offset_base + lcn * cluster_bytes:
                frags[-1].tamanho += n * cluster_bytes
            else:
                frags.append(Fragmento(offset_base + lcn * cluster_bytes,
                                       n * cluster_bytes))
            pos_vcn += n
        if tamanho is not None:
            # limita ao tamanho real do arquivo, do fim para frente
            restante = tamanho
            limitadas = []
            for f in frags:
                if restante <= 0:
                    break
                t = min(f.tamanho, restante)
                limitadas.append(Fragmento(f.offset, t))
                restante -= t
            return limitadas
        return frags


def descomprimir_lznt1(dados: bytes, limite_saida: int) -> bytes | None:
    """Descomprime um fluxo LZNT1 completo. Retorna None se a estrutura for
    inválida (comportamento conservador)."""
    saida = bytearray()
    i = 0
    n = len(dados)
    while i + 2 <= n and len(saida) < limite_saida:
        cab = struct.unpack_from("<H", dados, i)[0]
        if cab == 0:
            if saida:
                break   # zeros de preenchimento após o último chunk
            # fluxo inteiro zerado: inválido (MS-XCA exige tamanho >= 3)
            return None
        i += 2
        comprimido = bool(cab & 0x8000)
        # MS-XCA: campo de 12 bits = (tamanho total do chunk, incluindo o
        # cabeçalho de 2 bytes) - 3  =>  corpo = campo + 1
        tam_corpo = (cab & 0x0FFF) + 1
        if not comprimido:
            tam_corpo = min(tam_corpo, n - i, 4096)
            saida += dados[i:i + tam_corpo]
            i += tam_corpo
            continue
        if i + tam_corpo > n:
            return None
        chunk = dados[i:i + tam_corpo]
        i += tam_corpo
        des = _descomprimir_chunk(chunk)
        if des is None:
            return None
        saida += des
        if len(saida) > limite_saida:
            return None
    if len(saida) > limite_saida:
        return None
    return bytes(saida)


def _descomprimir_chunk(chunk: bytes) -> bytearray | None:
    out = bytearray()
    i = 0
    n = len(chunk)
    while i < n:
        flags = chunk[i]
        i += 1
        for bit in range(8):
            if i >= n:
                return out
            if flags & (1 << bit):
                if i + 2 > n:
                    return None
                par = struct.unpack_from("<H", chunk, i)[0]
                i += 2
                pos = len(out)
                if pos == 0:
                    return None
                # MS-XCA 2.1: a divisão offset/length do token depende da
                # posição atual na saída descomprimida:
                #   pos 0..15 -> 4 bits de offset / 12 de length
                #   pos 16..31 -> 5 / 11 ... pos 2048..4095 -> 12 / 4
                offset_bits = 4
                while (1 << offset_bits) <= pos and offset_bits < 12:
                    offset_bits += 1
                length_bits = 16 - offset_bits
                length = (par & ((1 << length_bits) - 1)) + 3
                offset = (par >> length_bits) + 1
                if offset > pos:
                    return None
                ini = pos - offset
                for k in range(length):
                    out.append(out[ini + k])
                if len(out) > 4096 * 16:
                    return None
            else:
                out.append(chunk[i])
                i += 1
    return out


class Atributo:
    __slots__ = ("tipo", "residente", "nome", "flags", "id", "conteudo",
                 "runlist", "tamanho_real", "tamanho_alocado", "vcn_ini",
                 "unidade_compressao")

    def __init__(self):
        self.tipo = 0
        self.residente = True
        self.nome = ""
        self.flags = 0
        self.id = 0
        self.conteudo = b""
        self.runlist = None
        self.tamanho_real = 0
        self.tamanho_alocado = 0
        self.vcn_ini = 0
        self.unidade_compressao = 0

    @property
    def comprimido(self) -> bool:
        return bool(self.flags & 0x0001)

    @property
    def sparse(self) -> bool:
        return bool(self.flags & 0x8000)


class RegistroMFT:
    """Registro FILE analisado (fixup já aplicado)."""

    def __init__(self, numero: int, dados: bytes):
        self.numero = numero
        self.dados = dados
        self.flags = 0
        self.seq = 0
        self.links = 0
        self.base_ref = 0
        self.atributos: list[Atributo] = []
        self.valido = False
        self.motivo_invalido = ""

    @property
    def em_uso(self) -> bool:
        return bool(self.flags & 1)

    @property
    def diretorio(self) -> bool:
        return bool(self.flags & 2)

    def nomes(self) -> list[dict]:
        saida = []
        for a in self.atributos:
            if a.tipo == ATTR_FILE_NAME and len(a.conteudo) >= 66:
                c = a.conteudo
                pai_ref = struct.unpack_from("<Q", c, 0)[0]
                pai_num = pai_ref & 0xFFFFFFFFFFFF
                pai_seq = (pai_ref >> 48) & 0xFFFF
                t_criacao = struct.unpack_from("<Q", c, 8)[0]
                t_mod = struct.unpack_from("<Q", c, 24)[0]
                t_mft = struct.unpack_from("<Q", c, 40)[0]
                tam_alocado = struct.unpack_from("<Q", c, 40)[0]
                tam_real = struct.unpack_from("<Q", c, 48)[0]
                flags_nome = struct.unpack_from("<I", c, 56)[0]
                tam_nome = c[64]
                namespace = c[65]
                nome = c[66:66 + tam_nome * 2].decode("utf-16-le", "replace")
                saida.append({
                    "pai": pai_num, "pai_seq": pai_seq,
                    "criacao": ntfs_para_datetime(t_criacao),
                    "modificacao": ntfs_para_datetime(t_mod),
                    "acesso_mft": ntfs_para_datetime(t_mft),
                    "tam_alocado": tam_alocado, "tam_real": tam_real,
                    "flags": flags_nome, "nome": nome, "namespace": namespace,
                })
        return saida

    def dados_principais(self) -> Atributo | None:
        """DATA não residente preferido; senão o residente."""
        candidatos = [a for a in self.atributos if a.tipo == ATTR_DATA]
        for a in candidatos:
            if not a.residente:
                return a
        return candidatos[0] if candidatos else None


def parsear_registro(numero: int, bruto: bytes, rec_size: int,
                     setor: int = 512) -> RegistroMFT | None:
    """Valida e analisa um registro FILE. Retorna None se inválido.

    O array de fixup (USA) do NTFS protege SEMPRE setores de 512 bytes,
    independentemente do tamanho físico de setor do volume (512 ou 4096).
    O parâmetro `setor` é aceito por compatibilidade e usado apenas como
    limite superior conservador.
    """
    del setor  # não interfere no fixup (sempre 512, ver acima)
    setor_fixup = 512
    if len(bruto) < rec_size or rec_size > REC_SIZE_MAX:
        return None
    if bruto[:4] != b"FILE":
        return None
    usa_ofs, usa_cnt = struct.unpack_from("<HH", bruto, 4)
    if not (0 < usa_ofs < rec_size - 8) or not (1 <= usa_cnt <= rec_size // setor_fixup + 1):
        return None
    dados = bytearray(bruto[:rec_size])
    if not aplicar_fixup(dados, usa_ofs, usa_cnt, setor_fixup):
        return None
    reg = RegistroMFT(numero, bytes(dados))
    (reg.seq, reg.links) = struct.unpack_from("<HH", dados, 16)
    attr_ofs, reg.flags = struct.unpack_from("<HH", dados, 20)
    usado, alocado = struct.unpack_from("<II", dados, 24)
    reg.base_ref = struct.unpack_from("<Q", dados, 32)[0] & 0xFFFFFFFFFFFF
    if attr_ofs < 42 or attr_ofs >= alocado or usado > alocado or alocado != rec_size:
        return None
    i = attr_ofs
    n_attrs = 0
    while i + 8 <= usado and n_attrs < MAX_ATRIBUTOS:
        tipo = struct.unpack_from("<I", dados, i)[0]
        if tipo == ATTR_FIM:
            break
        comp = struct.unpack_from("<I", dados, i + 4)[0]
        if comp < 24 or comp % 8 != 0 or i + comp > usado:
            return None
        n_attrs += 1
        a = Atributo()
        a.tipo = tipo
        a.residente = dados[i + 8] == 0
        tam_nome_a = dados[i + 9]
        nome_ofs = struct.unpack_from("<H", dados, i + 10)[0]
        a.flags = struct.unpack_from("<H", dados, i + 12)[0]
        a.id = struct.unpack_from("<H", dados, i + 14)[0]
        if tam_nome_a and nome_ofs + tam_nome_a * 2 <= comp:
            a.nome = dados[i + nome_ofs:i + nome_ofs + tam_nome_a * 2].decode(
                "utf-16-le", "replace")
        if a.residente:
            tam_c, ofs_c = struct.unpack_from("<IH", dados, i + 16)
            if tam_c > comp or ofs_c + tam_c > comp:
                return None
            a.conteudo = bytes(dados[i + ofs_c:i + ofs_c + tam_c])
            a.tamanho_real = tam_c
        else:
            vcn_ini, vcn_fim = struct.unpack_from("<QQ", dados, i + 16)
            runs_ofs = struct.unpack_from("<H", dados, i + 32)[0]
            a.unidade_compressao = struct.unpack_from("<H", dados, i + 34)[0]
            aloc, real, inic = struct.unpack_from("<QQQ", dados, i + 40)
            if runs_ofs >= comp or vcn_fim < vcn_ini and vcn_fim != 0:
                return None
            rl = RunList.parsear(bytes(dados[i + runs_ofs:i + comp]))
            if rl is None:
                return None
            a.runlist = rl
            a.vcn_ini = vcn_ini
            a.tamanho_real = real
            a.tamanho_alocado = aloc
            del inic
        reg.atributos.append(a)
        i += comp
    if n_attrs == 0:
        return None
    reg.valido = True
    return reg


class SistemaNTFS:
    def __init__(self, leitor, info: dict, offset_volume: int = 0,
                 tam_volume: int | None = None):
        self.leitor = leitor
        self.info = info
        self.tipo = "ntfs"
        self.setor = info["bytes_setor"]
        self.spc = info["spc"]
        self.vol_off = offset_volume
        self.vol_tam = tam_volume or info["total_setores"] * self.setor
        self.cluster_bytes = self.spc * self.setor
        self.rec_size = info["rec_size"]
        self.mft_off = offset_volume + info["mft_cluster"] * self.cluster_bytes
        self.mftmirr_off = offset_volume + info["mftmirr_cluster"] * self.cluster_bytes
        self.fim_vol = offset_volume + self.vol_tam
        self._n_registros = None
        self._bitmap_cache = {}

    # ------------------------------------------------------------- básico

    def offset_cluster(self, lcn: int) -> int:
        return self.vol_off + lcn * self.cluster_bytes

    def ler_registro_bruto(self, numero: int) -> bytes | None:
        off = self.mft_off + numero * self.rec_size
        if off + self.rec_size > self.fim_vol:
            return None
        return self.leitor.read(off, self.rec_size)

    def numero_registros(self) -> int:
        """Usa o $MFT (registro 0) para saber a extensão real da MFT."""
        if self._n_registros is None:
            reg0 = self.ler_registro(0)
            total_bytes = self.fim_vol - self.mft_off
            if reg0 and reg0.valido:
                data = reg0.dados_principais()
                if data and data.runlist:
                    total_bytes = data.runlist.total_clusters() * self.cluster_bytes
            self._n_registros = max(1, total_bytes // self.rec_size)
        return self._n_registros

    def ler_registro(self, numero: int) -> RegistroMFT | None:
        bruto = self.ler_registro_bruto(numero)
        if bruto is None:
            return None
        return parsear_registro(numero, bruto, self.rec_size, self.setor)

    # ------------------------------------------------------------- bitmap

    def _carregar_bitmap(self):
        if hasattr(self, "_bitmap_runs"):
            return
        self._bitmap_runs = None
        self._bitmap_tamanho = 0
        reg6 = self.ler_registro(6)  # $Bitmap
        if reg6 and reg6.valido:
            for a in reg6.atributos:
                if a.tipo == ATTR_DATA and a.runlist:
                    self._bitmap_runs = a.runlist
                    self._bitmap_tamanho = a.tamanho_real
                    return

    def cluster_alocado(self, lcn: int) -> bool | None:
        """Consulta o $Bitmap (registro 6). None se indisponível."""
        if not hasattr(self, "_bitmap_runs"):
            self._carregar_bitmap()
        if self._bitmap_runs is None:
            return None
        byte_idx = lcn // 8
        pagina = byte_idx >> 16  # cache de 64 KiB
        bloco = self._bitmap_cache.get(pagina)
        if bloco is None:
            if len(self._bitmap_cache) > 32:
                self._bitmap_cache.clear()
            bloco = self._ler_bitmap_bytes(pagina * 65536, 65536)
            self._bitmap_cache[pagina] = bloco
        if bloco is None:
            return None
        i = byte_idx - pagina * 65536
        if i >= len(bloco):
            return None
        return bool(bloco[i] & (1 << (lcn % 8)))

    def _ler_bitmap_bytes(self, inicio: int, tam: int) -> bytes | None:
        """Lê bytes do $Bitmap cobrindo runs (com suporte a fragmentação)."""
        rl = self._bitmap_runs
        if rl is None:
            return None
        dados = bytearray()
        pos = 0  # posição em bytes dentro do bitmap
        for lcn, n, sparse in rl.runs:
            run_ini = pos
            run_fim = pos + n * self.cluster_bytes
            if run_fim <= inicio:
                pos = run_fim
                continue
            if run_ini >= inicio + tam:
                break
            off = max(inicio, run_ini)
            fim = min(inicio + tam, run_fim)
            if sparse or lcn < 0:
                dados += b"\x00" * (fim - off)
            else:
                delta = off - run_ini
                dados += self.leitor.read(
                    self.offset_cluster(lcn) + delta, fim - off)
            pos = run_fim
            if len(dados) >= tam:
                break
        return bytes(dados[:tam]) if dados else None

    # ------------------------------------------------- ATTRIBUTE_LIST

    def _coletar_data_runs(self, reg: RegistroMFT) -> tuple[list[tuple[int, RunList]], bool]:
        """Retorna (lista[(vcn_ini, runlist)], comprimido) para o atributo
        DATA, reunindo extensões via ATTRIBUTE_LIST quando necessário."""
        resultado = []
        comprimido = False
        tem_lista = False
        for a in reg.atributos:
            if a.tipo == ATTR_DATA and not a.residente and a.runlist:
                resultado.append((a.vcn_ini, a.runlist))
                comprimido = comprimido or a.comprimido
            if a.tipo == ATTR_ATTRIBUTE_LIST:
                tem_lista = True
        if tem_lista:
            conteudo = None
            for a in reg.atributos:
                if a.tipo == ATTR_ATTRIBUTE_LIST:
                    if a.residente:
                        conteudo = a.conteudo
                    elif a.runlist:
                        conteudo = self.ler_dados_runlist(
                            a.runlist, a.tamanho_real, comprimido=False)
                    break
            if conteudo:
                # Layout da entrada (28 bytes + nome, múltiplo de 8):
                #   0x00 tipo | 0x04 comprimento | 0x06 tam_nome
                #   0x07 off_nome | 0x08 flags | 0x0A id
                #   0x0C menor VCN | 0x14 referência ao registro
                i = 0
                while i + 28 <= len(conteudo) and i < 64 * 1024:
                    tipo = struct.unpack_from("<I", conteudo, i)[0]
                    comp = struct.unpack_from("<H", conteudo, i + 4)[0]
                    if tipo == ATTR_FIM:
                        break
                    if comp < 28 or comp % 8 != 0 or i + comp > len(conteudo):
                        break
                    if tipo == ATTR_DATA:
                        vcn = struct.unpack_from("<Q", conteudo, i + 12)[0]
                        ref = struct.unpack_from("<Q", conteudo, i + 20)[0]
                        num_ext = ref & 0xFFFFFFFFFFFF
                        seq_ext = (ref >> 48) & 0xFFFF
                        if num_ext != reg.numero:
                            reg_ext = self.ler_registro(num_ext)
                            if (reg_ext and reg_ext.valido
                                    and (not seq_ext or reg_ext.seq == seq_ext)):
                                for a2 in reg_ext.atributos:
                                    if (a2.tipo == ATTR_DATA and not a2.residente
                                            and a2.runlist and a2.vcn_ini == vcn):
                                        resultado.append((a2.vcn_ini, a2.runlist))
                                        comprimido = comprimido or a2.comprimido
                    i += comp
        resultado.sort(key=lambda x: x[0])
        return resultado, comprimido

    def ler_dados_runlist(self, rl: RunList, tamanho: int,
                          comprimido: bool = False,
                          unidade: int = 16) -> bytes | None:
        """Lê (e descomprime, se necessário) os dados cobertos por runs."""
        if comprimido:
            return self._ler_comprimido(rl, tamanho, unidade)
        frags = rl.fragmentos(self.vol_off, self.cluster_bytes)
        dados = bytearray()
        for f in frags:
            off = f.offset
            falta = tamanho - len(dados)
            if falta <= 0:
                break
            tam = min(f.tamanho, falta)
            if off + tam > self.fim_vol:
                return None
            dados += self.leitor.read(off, tam)
        # lacunas (sparse entre runs) não são preenchidas aqui: só usamos quando
        # runs cobrem todo o tamanho
        if len(dados) < tamanho:
            return None
        return bytes(dados)

    def _ler_comprimido(self, rl: RunList, tamanho: int, unidade: int) -> bytes | None:
        """Descomprime LZNT1 por unidades de compressão (padrão 16 clusters).
        Se a unidade está totalmente ocupada por runs não sparse, ela foi
        gravada descomprimida e é copiada diretamente."""
        if unidade == 0:
            unidade = 16
        tam_unidade = unidade * self.cluster_bytes
        segmentos = []  # (vcn_cluster_ini, n_clusters, lcn ou None se sparse)
        vcn = 0
        for lcn, n, sparse in rl.runs:
            segmentos.append((vcn, n, None if (sparse or lcn < 0) else lcn))
            vcn += n
        total_clusters = vcn
        saida = bytearray()
        u = 0
        while u * unidade < total_clusters and len(saida) < tamanho:
            uni_ini = u * unidade
            uni_fim = min(uni_ini + unidade, total_clusters)
            fisicos = bytearray()
            n_fis = 0
            n_sparse = 0
            for v, n, lcn in segmentos:
                a = max(v, uni_ini)
                b = min(v + n, uni_fim)
                if b <= a:
                    continue
                if lcn is None:
                    n_sparse += (b - a)
                    continue
                off = self.offset_cluster(lcn + (a - v))
                pedaco = min((b - a) * self.cluster_bytes,
                             max(0, self.fim_vol - off))
                fisicos += self.leitor.read(off, pedaco)
                n_fis += (b - a)
            if n_sparse == 0 and n_fis == uni_fim - uni_ini:
                # unidade integralmente física => gravada descomprimida
                saida += bytes(fisicos)
            else:
                des = descomprimir_lznt1(bytes(fisicos), tam_unidade)
                if des is None:
                    return None
                saida += des
            u += 1
        return bytes(saida[:tamanho])

    def _fragmentos_de_runs(self, runs_ordenados, tamanho: int) -> list[Fragmento]:
        frags = []
        for _vcn, rl in runs_ordenados:
            for f in rl.runs:
                lcn, n, sparse = f
                if sparse or lcn < 0:
                    continue
                off = self.offset_cluster(lcn)
                if frags and frags[-1].offset + frags[-1].tamanho == off:
                    frags[-1].tamanho += n * self.cluster_bytes
                else:
                    frags.append(Fragmento(off, n * self.cluster_bytes))
        if tamanho:
            resto = tamanho
            lim = []
            for f in frags:
                if resto <= 0:
                    break
                t = min(f.tamanho, resto)
                lim.append(Fragmento(f.offset, t))
                resto -= t
            return lim
        return frags

    # --------------------------------------------------------- recuperação

    def recuperar(self, cancelar=None, incluir_vivos: bool = False,
                  orfaos: bool = True, progresso=None,
                  filtro_caminho: str | None = None) -> tuple[list[CandidatoArquivo], list[dict]]:
        """Percorre a MFT e recupera candidatos. Retorna (candidatos, recusas),
        onde recusas alimenta o diagnostico_ntfs.csv."""
        self._carregar_bitmap()
        candidatos = []
        recusas = []
        nomes_cache = {}
        n_regs = self.numero_registros()
        passo = max(1, (4 * 1024 * 1024) // self.rec_size)
        regs_por_bloco = passo
        i = 0
        while i < n_regs:
            if cancelar is not None and cancelar.is_set():
                break
            off = self.mft_off + i * self.rec_size
            tam = min(regs_por_bloco, n_regs - i) * self.rec_size
            bloco = self.leitor.read(off, tam)
            n_no_bloco = len(bloco) // self.rec_size
            for k in range(n_no_bloco):
                bruto = bloco[k * self.rec_size:(k + 1) * self.rec_size]
                num = i + k
                if bruto[:4] != b"FILE":
                    continue
                reg = parsear_registro(num, bruto, self.rec_size, self.setor)
                if reg is None:
                    if bruto[:4] == b"FILE":
                        recusas.append({"registro_MFT": num, "nome": "",
                                        "motivo": "registro com estrutura inválida "
                                                  "(fixup/tamanho/atributos)"})
                    continue
                if reg.diretorio:
                    continue
                if reg.base_ref and reg.base_ref != num:
                    continue  # registro de extensão (ATTRIBUTE_LIST)
                if reg.em_uso and not incluir_vivos:
                    continue
                apagado = not reg.em_uso
                cand = self._candidato_de_registro(reg, apagado, nomes_cache,
                                                   recusas, METODO_MFT)
                if cand:
                    if filtro_caminho and not self._caminho_coincide(
                            cand.caminho_original, filtro_caminho):
                        continue
                    candidatos.append(cand)
            i += n_no_bloco
            if progresso:
                progresso(min(1.0, i / max(n_regs, 1)), 1.0, "mft")
        if orfaos:
            cand_orf, rec_orf = self.recuperar_mft_orfa(cancelar, nomes_cache,
                                                        progresso)
            candidatos.extend(cand_orf)
            recusas.extend(rec_orf)
        return candidatos, recusas

    def _caminho_coincide(self, caminho: str, filtro: str) -> bool:
        if not filtro:
            return True
        c = caminho.lower().replace("\\", "/").strip("/")
        f = filtro.lower().replace("\\", "/").strip("/")
        return c == f or c.startswith(f + "/") or f.endswith(c) or (
            "/" + f + "/") in ("/" + c + "/")

    def _nome_registro(self, reg: RegistroMFT) -> dict | None:
        nomes = reg.nomes()
        if not nomes:
            return None
        # prioriza Win32 (1) e Win32+DOS (3), depois POSIX (0) e DOS (2)
        for ns in (1, 3, 0, 2):
            for nm in nomes:
                if nm["namespace"] == ns:
                    return nm
        return nomes[0]

    def _caminho_completo(self, num_pai: int, nomes_cache: dict) -> str:
        partes = []
        atual = num_pai
        vistos = set()
        for _ in range(MAX_CADEIA_PAIS):
            if atual in (0, 5) or atual in vistos:  # raiz do volume
                break
            vistos.add(atual)
            reg = nomes_cache.get(atual)
            if reg is None:
                reg_p = self.ler_registro(atual)
                nm = self._nome_registro(reg_p) if reg_p and reg_p.valido else None
                reg = nm
                nomes_cache[atual] = nm
            if reg is None:
                break
            nome = reg["nome"]
            if not nome or nome == ".":
                break
            partes.append(nome)
            atual = reg["pai"]
        partes.reverse()
        return "\\".join(partes)

    def _candidato_de_registro(self, reg: RegistroMFT, apagado: bool,
                               nomes_cache: dict, recusas: list[dict],
                               metodo: str) -> CandidatoArquivo | None:
        nm = self._nome_registro(reg)
        if nm is None:
            recusas.append({"registro_MFT": reg.numero, "nome": "",
                            "motivo": "registro sem FILE_NAME utilizável"})
            return None
        nome = nm["nome"].strip()
        if not nome or nome.startswith("$"):
            return None
        data = reg.dados_principais()
        if data is None:
            recusas.append({"registro_MFT": reg.numero, "nome": nome,
                            "motivo": "registro sem atributo DATA"})
            return None
        tamanho = nm["tam_real"] or data.tamanho_real
        if tamanho <= 0 or tamanho > (1 << 42):
            recusas.append({"registro_MFT": reg.numero, "nome": nome,
                            "motivo": f"tamanho inválido ({tamanho})"})
            return None
        caminho = self._caminho_completo(nm["pai"], nomes_cache)

        motivo = ""
        confiavel = True
        if data.residente:
            conteudo = data.conteudo
            if len(conteudo) < tamanho:
                recusas.append({"registro_MFT": reg.numero, "nome": nome,
                                "motivo": "DATA residente truncado"})
                return None
            cand = CandidatoArquivo(
                nome, [], metodo, "ntfs",
                caminho_original=(caminho + "\\" + nome).strip("\\"),
                tamanho=tamanho, data=nm["modificacao"], confiavel=True,
                extra={"residente": True, "conteudo": conteudo[:tamanho],
                       "apagado": apagado, "registro": reg.numero})
            return cand

        runs, comprimido = self._coletar_data_runs(reg)
        if not runs:
            recusas.append({"registro_MFT": reg.numero, "nome": nome,
                            "motivo": "DATA não residente sem data runs"})
            return None
        # validação conservadora: runs devem cair dentro do volume
        for _vcn, rl in runs:
            for lcn, n, sparse in rl.runs:
                if sparse or lcn < 0:
                    continue
                ini = self.offset_cluster(lcn)
                fim = ini + n * self.cluster_bytes
                if ini < self.vol_off or fim > self.fim_vol:
                    recusas.append({
                        "registro_MFT": reg.numero, "nome": nome,
                        "motivo": f"data runs fora do volume (LCN {lcn}, "
                                  f"{n} cluster(s))"})
                    return None
        if comprimido:
            # dados comprimidos exigem descompressão na extração
            frags = self._fragmentos_de_runs(runs, None)
            cand = CandidatoArquivo(
                nome, frags, metodo, "ntfs",
                caminho_original=(caminho + "\\" + nome).strip("\\"),
                tamanho=tamanho, data=nm["modificacao"],
                extra={"comprimido": True, "runs": runs, "apagado": apagado,
                       "registro": reg.numero,
                       "unidade_compressao": data.unidade_compressao or 16})
            cand.motivo = "atributo comprimido (LZNT1)"
            return cand

        frags = self._fragmentos_de_runs(runs, tamanho)
        coberto = sum(f.tamanho for f in frags)
        if coberto < tamanho:
            motivo = f"data runs cobrem {coberto} de {tamanho} bytes"
            confiavel = False
        # verificação conservadora: clusters realocados no $Bitmap
        if apagado:
            ambiguo = self._clusters_realocados(runs)
            if ambiguo:
                motivo = (motivo + "; " if motivo else "") + (
                    f"clusters realocados no $Bitmap (ex.: LCN {ambiguo[0]}); "
                    "dados podem estar sobrescritos")
                confiavel = False
        n_extents = sum(1 for _vcn, rl in runs
                        for lcn, _n, sp in rl.runs if not sp and lcn >= 0)
        cand = CandidatoArquivo(
            nome, frags, metodo, "ntfs",
            caminho_original=(caminho + "\\" + nome).strip("\\"),
            tamanho=tamanho, data=nm["modificacao"],
            confiavel=confiavel, motivo=motivo,
            extra={"runs": runs, "apagado": apagado, "registro": reg.numero,
                   "n_extents": n_extents, "n_atributos_data": len(runs)})
        return cand

    def _clusters_realocados(self, runs, max_verif=64) -> list[int]:
        ambiguo = []
        verificados = 0
        for _vcn, rl in runs:
            for lcn, n, sparse in rl.runs:
                if sparse or lcn < 0:
                    continue
                for k in range(min(n, max_verif - verificados)):
                    aloc = self.cluster_alocado(lcn + k)
                    verificados += 1
                    if aloc is True:
                        ambiguo.append(lcn + k)
                        return ambiguo
                if verificados >= max_verif:
                    return ambiguo
        return ambiguo

    # --------------------------------------------------------- MFT órfã

    def recuperar_mft_orfa(self, cancelar=None, nomes_cache=None,
                           progresso=None) -> tuple[list[CandidatoArquivo], list[dict]]:
        """Procura registros FILE antigos fora da região da MFT atual
        (sobreviventes de formatação rápida). Validação estrita + recusas."""
        nomes_cache = nomes_cache if nomes_cache is not None else {}
        candidatos = []
        recusas = []
        mft_ini = self.mft_off
        n_regs = self.numero_registros()
        mft_fim = min(mft_ini + n_regs * self.rec_size, self.fim_vol)
        bloco = 8 * 1024 * 1024
        passo = self.rec_size
        # varre [início do volume, MFT) e [fim da MFT, fim do volume):
        # a região da MFT atual já foi analisada registro a registro
        faixas = []
        if mft_ini > self.vol_off:
            faixas.append((self.vol_off, min(mft_ini, self.fim_vol)))
        if mft_fim < self.fim_vol:
            faixas.append((max(mft_fim, self.vol_off), self.fim_vol))
        for faixa_ini, faixa_fim in faixas:
            off = faixa_ini
            while off + passo <= faixa_fim:
                if cancelar is not None and cancelar.is_set():
                    break
                dados = self.leitor.read(off, min(bloco, faixa_fim - off))
                if not dados:
                    break
                off = self._varrer_faixa_orfa(dados, off, candidatos, recusas,
                                              nomes_cache)
                if progresso:
                    progresso(min(1.0, off / max(self.fim_vol, 1)), 1.0, "mft_orfa")
        return candidatos, recusas

    def _varrer_faixa_orfa(self, dados: bytes, off_base: int,
                           candidatos: list, recusas: list,
                           nomes_cache: dict) -> int:
        passo = self.rec_size
        i = 0
        while i + passo <= len(dados):
            if dados[i:i + 4] == b"FILE":
                bruto = dados[i:i + passo]
                reg = parsear_registro(-1, bruto, self.rec_size, self.setor)
                if reg is None:
                    recusas.append({
                        "registro_MFT": f"orfa@{off_base + i}", "nome": "",
                        "motivo": "assinatura FILE com estrutura inválida"})
                    i += passo
                    continue
                if not reg.diretorio and not reg.base_ref:
                    cand = self._candidato_de_registro(
                        reg, True, nomes_cache, recusas, METODO_MFT_ORFA)
                    if cand:
                        cand.extra["offset_orfa"] = off_base + i
                        cand.motivo = (cand.motivo + "; " if cand.motivo else "") + \
                            "registro encontrado fora da MFT atual"
                        candidatos.append(cand)
                i += passo
            else:
                i += 512  # alinhamento conservador
        return off_base + len(dados)

def criar_desde_boot(leitor, setor_boot: bytes, offset_volume: int = 0,
                     tam_volume: int | None = None) -> SistemaNTFS | None:
    info = _info_ntfs(setor_boot)
    if not info:
        return None
    return SistemaNTFS(leitor, info, offset_volume, tam_volume)
