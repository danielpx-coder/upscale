"""
test_regressions.py — travas de regressão do Recupera Fotos 4.0.

Cada teste aqui existe porque um defeito REAL foi encontrado durante o
desenvolvimento e corrigido. O nome do teste diz qual defeito ele impede de
voltar. Muitos são de nível unitário (rápidos e precisos); os que precisam do
motor usam imagens sintéticas pequenas.

Defeitos cobertos:
  * data runs do NTFS com a convenção de nibbles invertida (nibble baixo é o
    tamanho do LENGTH, não do OFFSET — MS-FNTFS);
  * divisão offset/length dos tokens LZNT1 errada nas posições que são
    potência de dois (MS-XCA 2.1);
  * USA/fixup do NTFS aplicado com o tamanho do setor do volume em vez de
    512 bytes fixos;
  * entradas de ATTRIBUTE_LIST com layout inventado (VCN em 0x0C, referência
    em 0x14, nome em 0x1C);
  * discriminação FAT16 x FAT32 lendo FATSz32 no offset 36 (no boot FAT16
    esse byte é o número da unidade);
  * setor de boot aceito sem a assinatura 0x55AA;
  * cópias de reserva de boot não usadas (FAT32 setor 6, exFAT 12..23 com
    checksum, NTFS último setor);
  * arquivo apagado e fragmentado tratado como contíguo (a cadeia da FAT que
    sobrevive à exclusão precisa ser seguida);
  * GIF sem trailer detectado / MP4 atravessando o fim da região virando
    arquivo gigantesco;
  * TIFF/CR2/ORF/RW2/DNG medidos sem seguir StripOffsets/TileOffsets;
  * JPEG truncado descartado (ou marcado "ok") e JPEG truncado engolindo o
    resto do cartão;
  * índice MPF incoerente usado para "completar" o arquivo;
  * HEIC/AVIF (fotografias em contêiner ISO-BMFF) desativados junto com os
    vídeos; M2TS de 192 bytes não encontrado;
  * leitura além do fim físico preenchida com zeros (dado inventado);
  * bloco de varredura maior que o limite de memória de 8 MiB;
  * cancelamento na etapa 2 não refletido no resultado;
  * busca de partições perdidas feita sem avisar o usuário;
  * rótulo do volume FAT32 com 12 bytes em campo de 11, que fazia o
    setor de boot ter 513 bytes e TODA imagem de volume sair 2 bytes
    maior que tam_setores * tam_setor (achado pelo benchmark);
  * FileNameLength do exFAT lido e descartado: nome com entradas faltando
    era montado incompleto e ainda "validado" pelo hash (achado pelo ruff).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aquisicao
import assinaturas
import auxiliar as A
import exfat
import fatfs
import motor
import ntfs
import particoes
import verificacao
from candidatos import ESTADO_CORROMPIDO, ESTADO_OK, ESTADO_PARCIAL
from construtor import (
    ArquivoTeste,
    _attr_attribute_list,
    _attr_data_nao_residente,
    _attr_file_name,
    _registro_mft,
    _runs_bytes,
    _tam_bytes_assinado,
    construir_exfat,
    construir_fat16,
    construir_fat32,
    construir_ntfs,
    gravar,
    jpeg_grande_com_rst,
    jpeg_real,
    lznt1_comprimir_unidade,
    montar_disco_mbr,
    png_real,
)
from verificacao import LeitorBytes


class LeitorMemoria(aquisicao.LeitorBase):
    """Leitor somente-leitura sobre bytes em memória (com o mesmo contrato:
    além do fim, preenche com zeros — como os leitores reais do programa)."""

    def __init__(self, dados: bytes):
        super().__init__()
        self._dados = dados

    @property
    def tamanho(self) -> int:
        return len(self._dados)

    def read(self, offset: int, size: int) -> bytes:
        if offset >= len(self._dados) or size <= 0:
            return b""
        pedaço = self._dados[offset:offset + size]
        if len(pedaço) < size:
            pedaço += b"\x00" * (size - len(pedaço))
        return pedaço


class BaseImagem(unittest.TestCase):
    """Auxiliar para os testes que precisam rodar o motor."""

    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_reg_")
        self.jp = jpeg_real(64, 48, semente=77)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    def imagem(self, dados: bytes, nome="origem.img") -> str:
        return gravar(os.path.join(self.pasta, nome), dados)

    def executar(self, caminho: str, **kw):
        destino = kw.pop("destino", None) or tempfile.mkdtemp(prefix="rf4_reg_d_",
                                                              dir=self.pasta)
        kw.setdefault("min_kb", 0)
        opcoes = motor.Opcoes(origem=caminho, destino=destino,
                              permitir_mesmo_disco=True, **kw)
        res = motor.Motor(opcoes).executar()
        res._destino_teste = destino
        return res


# ===========================================================================
# 1. Data runs do NTFS — convenção real dos nibbles (MS-FNTFS 2.3.1)
# ===========================================================================


class TesteConvencoesDeRunsNTFS(unittest.TestCase):
    def test_nibble_baixo_e_o_length_e_alto_e_o_offset(self):
        """REGRESSÃO: a convenção estava invertida (nibble alto = length).
        Cabeçalho 0x21 = 1 byte de length + 2 bytes de offset."""
        dados = (bytes([0x21, 22]) + (49).to_bytes(2, "little")
                 + bytes([0x12]) + (32).to_bytes(2, "little") + bytes([5])
                 + bytes([0]))
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [(49, 22, False), (54, 32, False)])

    def test_exemplo_documentado_21_16_00_31_20_05(self):
        """Sequência clássica de documentação: 21 16 0031 / 31 20 000005."""
        dados = (bytes([0x21, 0x16]) + (0x0031).to_bytes(2, "little")
                 + bytes([0x31, 0x20]) + (5).to_bytes(3, "little")
                 + bytes([0]))
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs[0], (49, 22, False))
        self.assertEqual(rl.runs[1][0], 49 + 5)
        self.assertEqual(rl.runs[1][1], 32)

    def test_run_sparse_tem_offset_de_zero_bytes(self):
        """REGRESSÃO: sparse era codificado como 0x10 (nibble alto). O real é
        nibble ALTO zero (sem campo de offset) e nibble baixo = length."""
        dados = (bytes([0x11, 3]) + (7).to_bytes(1, "little")
                 + bytes([0x02]) + (5).to_bytes(2, "little") + bytes([0]))
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [(7, 3, False), (-1, 5, True)])
        self.assertEqual(rl.total_clusters(), 8)

    def test_delta_negativo_usa_largura_minima_assinada(self):
        self.assertEqual(_tam_bytes_assinado(-1), 1)
        self.assertEqual(_tam_bytes_assinado(-128), 1)
        self.assertEqual(_tam_bytes_assinado(-129), 2)
        dados = (bytes([0x11, 4]) + (100).to_bytes(1, "little")
                 + bytes([0x11, 4]) + (-30).to_bytes(1, "little", signed=True)
                 + bytes([0]))
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual([r[0] for r in rl.runs], [100, 70])

    def test_ida_e_volta_construtor_parser(self):
        """O construtor de imagens de teste e o parser precisam concordar —
        foi a discordância entre eles que mascarou a convenção invertida."""
        runs = [(100, 4), (5, 300), (-20, 2), (70000, 65536), (0, 8)]
        bruto = _runs_bytes([(d, n) for d, n, *_ in
                             [(r[0], r[1]) for r in runs]])
        rl = ntfs.RunList.parsear(bruto)
        esperado, acum = [], 0
        for delta, n in runs:
            if delta == 0:
                # delta 0 => sem campo de offset => run SPARSE (buraco)
                esperado.append((-1, n, True))
                continue
            acum += delta
            esperado.append((acum, n, False))
        self.assertEqual(rl.runs, esperado)

    def test_run_degenerado_n_zero_e_ignorado(self):
        """Length zero é ignorado, mas o campo de offset continua sendo
        consumido — senão o parser perde a sincronia com os runs seguintes."""
        dados = (bytes([0x11, 0]) + (9).to_bytes(1, "little")
                 + bytes([0x11, 2]) + (4).to_bytes(1, "little") + bytes([0]))
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [(4, 2, False)])

    def test_run_truncado_interrompe_sem_inventar(self):
        rl = ntfs.RunList.parsear(bytes([0x34]))   # 4+3 bytes que não existem
        self.assertEqual(rl.runs, [])

    def test_max_runs_limita_memoria(self):
        dados = bytes([0x11, 1, 0]) * 5000 + bytes([0])
        rl = ntfs.RunList.parsear(dados, max_runs=8)
        self.assertLessEqual(len(rl.runs), 8)

    def test_fragmentos_usam_base_e_cluster_do_volume(self):
        rl = ntfs.RunList([(10, 2, False), (12, 3, False)])
        frags = rl.fragmentos(1048576, 4096)
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0].offset, 1048576 + 10 * 4096)
        self.assertEqual(frags[0].tamanho, 5 * 4096)


# ===========================================================================
# 2. USA/fixup — sempre 512 bytes, nunca o setor do volume
# ===========================================================================


class TesteFixupNTFS(unittest.TestCase):
    def _registro(self):
        attrs = [_attr_file_name("fix.jpg", (1 << 48) | 5)]
        return bytearray(_registro_mft(30, True, False, attrs, rec_size=1024))

    def test_fixup_aplicado_com_setor_512(self):
        rec = self._registro()
        usa_ofs, usa_cnt = struct.unpack_from("<HH", rec, 4)
        self.assertTrue(ntfs.aplicar_fixup(bytearray(rec), usa_ofs, usa_cnt, 512))

    def test_fixup_usa_512_mesmo_em_volume_de_4096(self):
        """REGRESSÃO: o fixup era aplicado com o tamanho do setor do volume.
        Um registro de 1024 bytes tem SEMPRE 2 setores de 512 no USA."""
        rec = self._registro()
        usa_ofs, usa_cnt = struct.unpack_from("<HH", rec, 4)
        self.assertEqual(usa_cnt, 3)          # USA + 2 setores de 512
        self.assertFalse(ntfs.aplicar_fixup(bytearray(rec), usa_ofs, usa_cnt, 4096))
        self.assertTrue(ntfs.aplicar_fixup(bytearray(rec), usa_ofs, usa_cnt, 512))

    def test_registro_com_fixup_invalido_e_recusado(self):
        rec = self._registro()
        rec[511] ^= 0xFF                      # estraga o último byte do setor 0
        usa_ofs, usa_cnt = struct.unpack_from("<HH", rec, 4)
        reg = ntfs.parsear_registro(30, bytes(rec), 1024, setor=512)
        self.assertIsNone(reg)
        del usa_ofs, usa_cnt

    def test_assinatura_FILE_obrigatoria(self):
        rec = self._registro()
        rec[0:4] = b"XILE"
        self.assertIsNone(ntfs.parsear_registro(30, bytes(rec), 1024, setor=512))


# ===========================================================================
# 3. LZNT1 — divisão offset/length por posição (MS-XCA 2.1)
# ===========================================================================


class TesteLZNT1(unittest.TestCase):
    @staticmethod
    def _chunk(tokens_lit: list) -> bytes:
        """tokens_lit: lista de grupos; cada grupo é (flags, [itens])."""
        corpo = bytearray()
        for flags, itens in tokens_lit:
            corpo.append(flags)
            for it in itens:
                corpo += it
        cab = struct.pack("<H", 0x8000 | ((len(corpo) - 1) & 0x0FFF))
        return cab + bytes(corpo)

    def test_tabela_de_bits_pos_16_usa_5_de_offset(self):
        """REGRESSÃO: em pos=16 a divisão correta é 5/11 (MS-XCA), não 4/12.
        O token abaixo só decodifica com 5 bits de offset."""
        literais = bytes(range(16))                       # 2 grupos de 8
        # distância 16 (copia desde o início), comprimento 20
        token = ((16 - 1) << 11) | (20 - 3)
        corpo = bytearray([0x00]) + literais[:8]
        corpo += bytes([0x00]) + literais[8:16]
        corpo += bytes([0x01]) + struct.pack("<H", token)
        cab = struct.pack("<H", 0x8000 | ((len(corpo) - 1) & 0x0FFF))
        saida = ntfs.descomprimir_lznt1(bytes(cab + corpo), 65536)
        self.assertIsNotNone(saida)
        # cópia de 20 bytes a 16 de distância: sobrepõe a própria saída
        self.assertEqual(saida, literais + (literais + literais)[:20])

    def test_tabela_de_bits_pos_2048_usa_12_de_offset(self):
        literais = b"ABCDEFGH" * 256                     # 2048 bytes
        corpo = bytearray()
        for k in range(0, 2048, 8):
            corpo += bytes([0x00]) + literais[k:k + 8]
        # em pos=2048: 12 bits de offset / 4 de length => length máx = 18
        token = ((8 - 1) << 4) | (18 - 3)
        corpo += bytes([0x01]) + struct.pack("<H", token)
        cab = struct.pack("<H", 0x8000 | ((len(corpo) - 1) & 0x0FFF))
        saida = ntfs.descomprimir_lznt1(bytes(cab + corpo), 65536)
        self.assertIsNotNone(saida)
        self.assertEqual(len(saida), 2048 + 18)
        # distância 8 a partir de pos=2048: repete o período de 8 bytes
        self.assertEqual(saida[2048:], b"ABCDEFGH" * 2 + b"AB")

    def test_ida_e_volta_para_padroes_periodicos(self):
        for padrao in (b"AB" * 2048, b"CICLO123" * 512, b"X" * 4096,
                       bytes(range(16)) * 256, b"\x00" * 4096):
            comp = lznt1_comprimir_unidade(padrao)
            self.assertEqual(ntfs.descomprimir_lznt1(comp, 65536), padrao,
                             f"falha no padrão {padrao[:8]!r}")

    def test_grupo_de_literais_curto_so_no_fim(self):
        """Um grupo de literais curto no MEIO do chunk é malformado: o
        descompressor não pode continuar decodificando em cima dele."""
        corpo = bytes([0x00]) + b"ABCDEFGH" + bytes([0x00]) + b"IJ"
        cab = struct.pack("<H", 0x8000 | ((len(corpo) - 1) & 0x0FFF))
        saida = ntfs.descomprimir_lznt1(cab + corpo, 65536)
        # o grupo curto final é aceito; nada além dele é inventado
        self.assertEqual(saida, b"ABCDEFGHIJ")

    def test_zeros_apos_o_ultimo_chunk_sao_preenchimento(self):
        comp = lznt1_comprimir_unidade(b"Z" * 4096) + b"\x00" * 512
        self.assertEqual(ntfs.descomprimir_lznt1(comp, 65536), b"Z" * 4096)

    def test_fluxo_inteiro_zerado_e_invalido(self):
        self.assertIsNone(ntfs.descomprimir_lznt1(b"\x00" * 64, 65536))

    def test_offset_maior_que_a_saida_e_recusado(self):
        # 8 literais e então um token que aponta 16 bytes para trás (pos=8)
        corpo = bytes([0x00]) + bytes(range(8)) + bytes([0x01]) \
            + struct.pack("<H", (15 << 12) | 0)
        cab = struct.pack("<H", 0x8000 | ((len(corpo) - 1) & 0x0FFF))
        self.assertIsNone(ntfs.descomprimir_lznt1(cab + corpo, 65536))

    def test_chunk_nao_comprimido(self):
        dados = bytes(range(256)) * 16
        cab = struct.pack("<H", (len(dados) - 1) & 0x0FFF)
        self.assertEqual(ntfs.descomprimir_lznt1(cab + dados, 65536), dados)

    def test_limite_de_saida_respeitado(self):
        comp = lznt1_comprimir_unidade(b"K" * 4096)
        self.assertIsNone(ntfs.descomprimir_lznt1(comp, 100))


# ===========================================================================
# 4. ATTRIBUTE_LIST — layout real da entrada
# ===========================================================================


class TesteAttributeList(unittest.TestCase):
    def test_layout_da_entrada_de_28_bytes_mais_nome(self):
        """REGRESSÃO: o layout estava inventado. O real: comprimento em 0x04,
        VCN em 0x0C, referência do registro em 0x14, nome em 0x1C."""
        entradas = [(0x80, 0, 30, 1, 0x49), (0x80, 16, 31, 1, 0x4A)]
        attr = _attr_attribute_list(entradas)
        corpo_ofs = struct.unpack_from("<H", attr, 20)[0]
        corpo = attr[corpo_ofs:]
        (comp_entrada,) = struct.unpack_from("<H", corpo, 4)
        self.assertEqual(comp_entrada, 32)          # 28 + nome + alinhamento
        for k, (_tipo, vcn, reg, seq, aid) in enumerate(entradas):
            e = corpo[k * 32:(k + 1) * 32]
            self.assertEqual(struct.unpack_from("<I", e, 0)[0], 0x80)
            self.assertEqual(struct.unpack_from("<Q", e, 0x0C)[0], vcn)
            (ref,) = struct.unpack_from("<Q", e, 0x14)
            self.assertEqual(ref & 0x0000FFFFFFFFFFFF, reg)
            self.assertEqual(ref >> 48, seq)
            self.assertEqual(struct.unpack_from("<H", e, 0x0A)[0], aid)

    def test_atributo_data_nao_residente_cabecalario(self):
        attr = _attr_data_nao_residente([(50, 4)], 16384, 14000)
        self.assertEqual(struct.unpack_from("<I", attr, 0)[0], 0x80)
        self.assertEqual(attr[8], 1)                       # não residente
        runs_ofs = struct.unpack_from("<H", attr, 32)[0]
        rl = ntfs.RunList.parsear(attr[runs_ofs:])
        self.assertEqual(rl.runs, [(50, 4, False)])
        self.assertEqual(struct.unpack_from("<Q", attr, 40)[0], 16384)   # alocado
        self.assertEqual(struct.unpack_from("<Q", attr, 48)[0], 14000)   # real

    def test_atributo_comprimido_tem_flag_e_unidade(self):
        attr = _attr_data_nao_residente([(50, 16)], 65536, 4096,
                                        comprimido=True, unidade=16)
        (flags,) = struct.unpack_from("<H", attr, 12)
        self.assertTrue(flags & 0x0001)
        self.assertEqual(struct.unpack_from("<H", attr, 34)[0], 16)


# ===========================================================================
# 5. Identificação de sistemas de arquivos
# ===========================================================================


class TesteIdentificacaoDeBoot(unittest.TestCase):
    def test_fat16_nao_e_confundido_com_fat32(self):
        """REGRESSÃO: o byte 36 do boot FAT16 é o número da unidade física,
        não FATSz32. A discriminação correta usa RootEntCnt e FATSz16."""
        img16 = construir_fat16([])
        tipo, info = particoes.identificar_boot(img16[:512])
        self.assertEqual(tipo, "fat16")
        self.assertEqual(info["raiz_entradas"], 512)
        self.assertEqual(img16[36], 0)                     # drive number
        img32 = construir_fat32([], tam_setores=4096)
        tipo32, info32 = particoes.identificar_boot(img32[:512])
        self.assertEqual(tipo32, "fat32")
        self.assertEqual(info32["raiz_entradas"], 0)
        self.assertGreater(struct.unpack_from("<I", img32, 36)[0], 0)

    def test_assinatura_55aa_e_obrigatoria(self):
        img = bytearray(construir_fat32([], tam_setores=4096)[:512])
        self.assertEqual(particoes.identificar_boot(bytes(img))[0], "fat32")
        img[510:512] = b"\x00\x00"
        self.assertIsNone(particoes.identificar_boot(bytes(img))[0])

    def test_exfat_e_ntfs_pelo_oem(self):
        exf = construir_exfat([])
        self.assertEqual(exf[3:11], b"EXFAT   ")
        self.assertEqual(particoes.identificar_boot(exf[:512])[0], "exfat")
        ntf = construir_ntfs([])
        self.assertEqual(ntf[3:11], b"NTFS    ")
        self.assertEqual(particoes.identificar_boot(ntf[:512])[0], "ntfs")

    def test_setores_de_4096_sao_lidos_do_boot(self):
        img = construir_fat32([], tam_setores=4096, tam_setor=4096, spc=2)
        _tipo, info = particoes.identificar_boot(img[:512])
        self.assertEqual(info["bytes_setor"], 4096)
        self.assertEqual(info["spc"], 2)

    def test_lixo_nao_e_sistema_de_arquivos(self):
        aleatorio = bytearray(os.urandom(512))
        aleatorio[510:512] = b"\x00\x00"
        for lixo in (b"\x00" * 512, b"\xEB\x3C\x90" + b"\x11" * 507 + b"\x55\xAA",
                     bytes(aleatorio)):
            tipo, _info = particoes.identificar_boot(lixo)
            self.assertIsNone(tipo)


# ===========================================================================
# 6. Cópias de reserva do setor de boot
# ===========================================================================


class TesteBootDeReserva(unittest.TestCase):
    def test_fat32_usa_setor_6(self):
        img = construir_fat32([], tam_setores=4096, corromper_boot=True)
        part = particoes.Particao(0, len(img), "fat32", "teste")
        _s, tipo, _info, origem = particoes.ler_boot_com_backup(LeitorMemoria(img), part)
        self.assertEqual(tipo, "fat32")
        self.assertEqual(origem, "backup_setor_6")

    def test_exfat_usa_regiao_de_backup_com_checksum(self):
        img = construir_exfat([], corromper_boot=True)
        part = particoes.Particao(0, len(img), "exfat", "teste")
        _s, tipo, _info, origem = particoes.ler_boot_com_backup(LeitorMemoria(img), part)
        self.assertEqual(tipo, "exfat")
        self.assertEqual(origem, "backup_exfat")

    def test_exfat_com_checksum_invalido_nao_e_aceito(self):
        img = bytearray(construir_exfat([], corromper_boot=True))
        for k in range(12 * 512, 13 * 512):     # estraga o backup também
            img[k] ^= 0xFF
        part = particoes.Particao(0, len(img), "exfat", "teste")
        _s, tipo, _info, origem = particoes.ler_boot_com_backup(
            LeitorMemoria(bytes(img)), part)
        self.assertNotEqual(origem, "backup_exfat")
        self.assertIsNone(tipo)

    def test_ntfs_usa_ultimo_setor(self):
        img = construir_ntfs([], corromper_boot=True)
        part = particoes.Particao(0, len(img), "ntfs", "teste")
        _s, tipo, _info, origem = particoes.ler_boot_com_backup(LeitorMemoria(img), part)
        self.assertEqual(tipo, "ntfs")
        self.assertEqual(origem, "backup_ntfs")

    def test_boot_primario_intacto_nao_usa_reserva(self):
        img = construir_fat32([], tam_setores=4096)
        part = particoes.Particao(0, len(img), "fat32", "teste")
        _s, tipo, _info, origem = particoes.ler_boot_com_backup(LeitorMemoria(img), part)
        self.assertEqual(origem, "primario")
        self.assertEqual(tipo, "fat32")

class TesteBootDeReservaNoMotor(BaseImagem):
    def test_motor_avisa_quando_usa_copia_de_reserva(self):
        img = construir_fat32([ArquivoTeste("bk.jpg", jpeg_real(48, 36, semente=1),
                                            apagado=True)],
                              tam_setores=16384, corromper_boot=True)
        res = self.executar(self.imagem(img))
        self.assertIn("bk.jpg", [c.nome for c in res.candidatos])
        self.assertTrue(any("reserva" in a for a in res.avisos))


# ===========================================================================
# 7. Cadeia da FAT que sobrevive à exclusão
# ===========================================================================


class TesteCadeiaFATSobrevivente(unittest.TestCase):
    def test_apagado_fragmentado_usa_a_cadeia_e_nao_a_contiguidade(self):
        """REGRESSÃO: o arquivo apagado e fragmentado era lido como contíguo a
        partir do primeiro cluster — os bytes a partir do 2º fragmento vinham
        errados. A cadeia da FAT sobrevive à exclusão e deve ser seguida."""
        corpo = jpeg_grande_com_rst(semente=3)
        img = construir_fat32([ArquivoTeste("frag.jpg", corpo, apagado=True,
                                            fragmentado=True)])
        fs = fatfs.criar_desde_boot(LeitorMemoria(img), img[:512], 0, len(img))
        cands = fs.recuperar()
        c = next(x for x in cands if x.nome == "frag.jpg")
        self.assertGreater(len(c.fragmentos), 1)
        dados = b"".join(img[f.offset:f.offset + f.tamanho] for f in c.fragmentos)
        self.assertEqual(dados[:len(corpo)], corpo)
        self.assertEqual(c.tamanho, len(corpo))

    def test_apagado_contiguo_sem_ambiguidade(self):
        """Sem cadeia na FAT (o Windows a zera na exclusão de arquivo
        contíguo), usa-se a contiguidade — e os clusters seguintes precisam
        estar livres para o resultado ser declarado confiável."""
        corpo = jpeg_real(80, 60, semente=2)
        img = construir_fat32([ArquivoTeste("cont.jpg", corpo, apagado=True)])
        fs = fatfs.criar_desde_boot(LeitorMemoria(img), img[:512], 0, len(img))
        c = next(x for x in fs.recuperar() if x.nome == "cont.jpg")
        dados = b"".join(img[f.offset:f.offset + f.tamanho] for f in c.fragmentos)
        self.assertEqual(dados[:len(corpo)], corpo)
        self.assertTrue(c.confiavel)

    def test_apagado_contiguo_com_clusters_realocados_nao_e_confiavel(self):
        """Se os clusters seguintes já foram reutilizados por outro arquivo, a
        contiguidade é ambígua: o resultado NÃO pode ser declarado confiável
        (é exatamente assim que uma foto recuperada vem corrompida)."""
        corpo = jpeg_real(160, 120, semente=2)          # ocupa vários clusters
        img = bytearray(construir_fat32(
            [ArquivoTeste("cont.jpg", corpo, apagado=True)]))
        _tipo, info = particoes.identificar_boot(bytes(img[:512]))
        fs = fatfs.criar_desde_boot(LeitorMemoria(bytes(img)), bytes(img[:512]),
                                    0, len(img))
        c = next(x for x in fs.recuperar() if x.nome.endswith("cont.jpg"))
        primeiro = c.extra["cluster"]
        n_clusters = (c.tamanho + info["bytes_setor"] * info["spc"] - 1) // \
            (info["bytes_setor"] * info["spc"])
        self.assertGreater(n_clusters, 1)
        self.assertTrue(c.confiavel)                    # antes de realocar
        # simula a realocação: marca o 2º cluster do arquivo como em uso nas
        # duas cópias da FAT
        fat_ini = info["reservados"] * info["bytes_setor"]
        for k in range(info["nfats"]):
            off = (fat_ini + k * info["fat_size"] * info["bytes_setor"]
                   + (primeiro + 1) * 4)
            img[off:off + 4] = struct.pack("<I", 0x0FFFFFFF)
        fs2 = fatfs.criar_desde_boot(LeitorMemoria(bytes(img)), bytes(img[:512]),
                                     0, len(img))
        c2 = next(x for x in fs2.recuperar() if x.nome.endswith("cont.jpg"))
        self.assertFalse(c2.confiavel)
        self.assertIn("realocados", c2.motivo)

    def test_exfat_apagado_com_cadeia_sobrevivente(self):
        corpo = jpeg_grande_com_rst(semente=4)
        img = construir_exfat([ArquivoTeste("ex.jpg", corpo, apagado=True,
                                            fragmentado=True)])
        _tipo, info = particoes.identificar_boot(img[:512])
        fs = exfat.SistemaExFAT(LeitorMemoria(img), info, 0, len(img))
        c = next(x for x in fs.recuperar() if x.nome == "ex.jpg")
        self.assertEqual(c.tamanho, len(corpo))
        dados = b"".join(img[f.offset:f.offset + f.tamanho] for f in c.fragmentos)
        self.assertEqual(dados[:len(corpo)], corpo)

    def test_lfn_reconstruido_e_checksum_conferido(self):
        nome = "Minha Fotografia Longa.jpg"
        img = construir_fat32([ArquivoTeste(nome, jpeg_real(32, 24, semente=5),
                                            apagado=True)])
        fs = fatfs.criar_desde_boot(LeitorMemoria(img), img[:512], 0, len(img))
        nomes = [c.nome for c in fs.recuperar()]
        self.assertIn(nome, nomes)


# ===========================================================================
# 8. Carving — defeitos corrigidos nos analisadores
# ===========================================================================


class TesteCarvingCorrigido(unittest.TestCase):
    def test_gif_com_trailer_no_fim_da_regiao(self):
        """REGRESSÃO: o trailer 0x3B só era conferido com 2 bytes legíveis;
        no fim exato da região o GIF era dado como truncado."""
        g = A.gif_sintetico()
        self.assertTrue(g.endswith(b"\x3B"))
        r = assinaturas.analisar_gif(LeitorBytes(g), 0)
        self.assertEqual(r.comprimento, len(g))
        self.assertFalse(r.truncado)

    def test_mp4_nao_ultrapassa_o_fim_da_regiao(self):
        """REGRESSÃO: uma box que atravessava o fim da região gerava um
        arquivo de centenas de MB. O tamanho para na posição validada."""
        m = A.mp4_sintetico()
        regiao = m + b"\x00" * 4096
        r = assinaturas.analisar_mp4(LeitorBytes(regiao), 0, fim_regiao=len(m) + 100)
        self.assertIsNotNone(r.comprimento)
        self.assertLessEqual(r.comprimento, len(m) + 100)
        self.assertTrue(r.truncado)

    def test_mp4_inteiro_dentro_da_regiao(self):
        m = A.mp4_sintetico()
        r = assinaturas.analisar_mp4(LeitorBytes(m), 0, fim_regiao=len(m))
        self.assertEqual(r.comprimento, len(m))

    def test_tiff_segue_strips_e_ifds(self):
        """REGRESSÃO: o TIFF era medido sem seguir StripOffsets/TileOffsets —
        TIFF/CR2/ORF/RW2/DNG reais saíam como 'truncados'."""
        casos = [("tiff", A.tiff_sintetico()),
                 ("tiff_be", A.tiff_sintetico(endian=">")),
                 ("dng", A.tiff_sintetico(tag_dng=True)),
                 ("cr2", A.tiff_cr2_sintetico(8, 6)),
                 ("orf", A.orf_sintetico(b"IIRO")),
                 ("rw2", A.rw2_sintetico())]
        for nome, blob in casos:
            r = assinaturas.analisar_tiff(LeitorBytes(blob), 0)
            self.assertIsNotNone(r, nome)
            self.assertEqual(r.comprimento, len(blob), nome)
            self.assertFalse(r.truncado, nome)
            self.assertIn("strips", (r.motivo or "").lower() + "tiles", nome)

    def test_tiff_com_strips_ausentes_nao_vira_ok(self):
        """A estrutura declara strips que não existem mais: o comprimento é o
        declarado, mas o que chega ao usuário é 'parcial'/'corrompido'."""
        blob = A.tiff_sintetico()
        metade = blob[:len(blob) // 2]
        r = assinaturas.analisar_tiff(LeitorBytes(metade), 0)
        self.assertEqual(r.comprimento, len(blob))     # tamanho declarado
        v = verificacao.classificar(metade, "tif", tamanho_esperado=len(blob))
        self.assertEqual(v.estado, ESTADO_PARCIAL)

    def test_jpeg_truncado_e_parcial_nunca_ok(self):
        """REGRESSÃO: JPEG sem EOI era descartado (nada era recuperado)."""
        j = A.jpeg_truncado(jpeg_real(96, 72, semente=6))
        r = assinaturas.analisar_jpeg(LeitorBytes(j), 0, fim_regiao=len(j))
        self.assertIsNotNone(r.comprimento)
        self.assertTrue(r.truncado)
        self.assertTrue(r.diagnostico.get("sem_eoi"))
        v = verificacao.classificar(j, "jpg")
        self.assertEqual(v.estado, ESTADO_PARCIAL)
        self.assertNotEqual(v.estado, ESTADO_OK)

    def test_jpeg_truncado_nao_engole_o_cartao(self):
        """REGRESSÃO: sem EOI, a varredura avançava até o fim da região e o
        'arquivo' recuperado continha megabytes de preenchimento. 64 KB sem
        nenhum byte 0xFF não são dados entropy."""
        j = A.jpeg_truncado(jpeg_real(96, 72, semente=8))
        enchimento = (bytes([0xA5, 0x5A, 0x33]) * 400) * 400     # 480 KB
        regiao = j + enchimento
        r = assinaturas.analisar_jpeg(LeitorBytes(regiao), 0, fim_regiao=len(regiao))
        self.assertLess(r.comprimento, len(j) + 128 * 1024)
        self.assertLessEqual(r.comprimento, len(j))

    def test_jpeg_inteiro_com_rst_e_ok(self):
        j = jpeg_grande_com_rst(semente=11)
        self.assertIn(b"\xFF\xDD", j)                       # DRI presente
        r = assinaturas.analisar_jpeg(LeitorBytes(j), 0, fim_regiao=len(j))
        self.assertEqual(r.comprimento, len(j))
        self.assertFalse(r.truncado)
        self.assertEqual(verificacao.classificar(j, "jpg").estado, ESTADO_OK)

    def test_rst_fora_de_ordem_e_corrompido(self):
        j = A.jpeg_com_rst_fora_de_ordem(jpeg_grande_com_rst(semente=12))
        r = assinaturas.analisar_jpeg(LeitorBytes(j), 0, fim_regiao=len(j))
        self.assertGreater(r.diagnostico["rst_quebras"], 0)
        self.assertEqual(verificacao.classificar(j, "jpg").estado, ESTADO_CORROMPIDO)

    def test_indice_mpf_incoerente_nao_completa_o_arquivo(self):
        """REGRESSÃO: um offset MPF absurdo passava na validação frouxa
        (apenas 'offset >= base'). Agora é checado contra o fim do arquivo."""
        j1 = jpeg_real(64, 48, semente=13)
        j2 = jpeg_real(32, 24, semente=14)
        mau = A.jpeg_mpf(j1, j2, valido=False)
        r = assinaturas.analisar_jpeg(LeitorBytes(mau), 0, fim_regiao=len(mau))
        mpf = r.diagnostico["mpf"]
        self.assertFalse(mpf["valido"])
        self.assertIn("fora do arquivo", mpf["motivo"])
        self.assertEqual(r.comprimento, len(j1) + 90)      # só a primária

    def test_indice_mpf_valido_cobre_as_paginas(self):
        j1 = jpeg_real(64, 48, semente=15)
        j2 = jpeg_real(32, 24, semente=16)
        bom = A.jpeg_mpf(j1, j2, valido=True)
        r = assinaturas.analisar_jpeg(LeitorBytes(bom), 0, fim_regiao=len(bom))
        mpf = r.diagnostico["mpf"]
        self.assertTrue(mpf["valido"])
        self.assertEqual(len(mpf["imagens"]), 2)
        self.assertEqual(max(o + t for o, t in mpf["imagens"]), len(bom))

    def test_heic_e_avif_sao_fotos_mesmo_sem_videos(self):
        """REGRESSÃO: ao desativar vídeos, o analisador 'mp4' era desligado
        inteiro — e HEIC/AVIF/CR3 são FOTOGRAFIAS em contêiner ISO-BMFF."""
        op = motor.Opcoes(origem="x", destino="y", videos=False)
        self.assertFalse(op.videos)
        formatos = {"jpeg", "png", "gif", "bmp", "tiff", "raf", "psd", "riff", "mp4"}
        for blob, ext in ((A.heic_sintetico(), "heic"), (A.avif_sintetico(), "avif")):
            achados = list(assinaturas.varrer_regiao(LeitorBytes(blob), 0, len(blob),
                                                     formatos=formatos, min_bytes=0))
            self.assertTrue(achados, ext)
            self.assertEqual(achados[0][1].extensao, ext)
            self.assertEqual(achados[0][1].comprimento, len(blob))

    def test_m2ts_de_192_bytes_e_encontrado(self):
        """REGRESSÃO: o M2TS (timecode de 4 bytes + pacote de 188) só era
        achado quando a varredura começava 4 bytes antes do sync."""
        ts = A.mpegts_sintetico(n_pacotes=60, pacote=192)
        regiao = bytes(1024) + ts + bytes(1024)
        achados = list(assinaturas.varrer_regiao(LeitorBytes(regiao), 0, len(regiao),
                                                 formatos={"mpegts"}, min_bytes=0))
        offs = [off for off, _r in achados]
        self.assertIn(1024, offs)
        r = next(r for off, r in achados if off == 1024)
        self.assertEqual(r.comprimento, len(ts))
        self.assertEqual(r.extensao, "m2ts")

    def test_mts_de_188_bytes_tambem(self):
        ts = A.mpegts_sintetico(n_pacotes=60, pacote=188)
        achados = list(assinaturas.varrer_regiao(LeitorBytes(ts), 0, len(ts),
                                                 formatos={"mpegts"}, min_bytes=0))
        self.assertEqual(achados[0][0], 0)
        self.assertEqual(achados[0][1].comprimento, len(ts))
        self.assertEqual(achados[0][1].extensao, "mts")

    def test_webp_real_tem_comprimento_exato_e_decodifica(self):
        w = A.webp_real(24, 18)
        r = assinaturas.analisar_riff(LeitorBytes(w), 0)
        self.assertEqual(r.comprimento, len(w))
        self.assertEqual(verificacao.classificar(w, "webp").estado, ESTADO_OK)

    def test_webp_sintetico_nao_afirma_ok(self):
        """WebP montado à mão é estruturalmente válido mas não decodifica:
        o estado honesto é 'nao_verificado' ou 'corrompido', nunca 'ok'."""
        w = A.webp_sintetico()
        v = verificacao.classificar(w, "webp")
        self.assertNotEqual(v.estado, ESTADO_OK)

    def test_png_e_bmp_com_comprimento_exato(self):
        for blob, ext in ((png_real(24, 18), "png"), (A.bmp_sintetico(), "bmp")):
            r = assinaturas.analisar_em(LeitorBytes(blob), 0,
                                        "png" if ext == "png" else "bmp")
            self.assertEqual(r.comprimento, len(blob), ext)


# ===========================================================================
# 9. Leitura honesta, memória e controle de execução
# ===========================================================================


class TesteLeituraHonesta(BaseImagem):
    def test_leitura_alem_do_fim_nao_inventa_zeros(self):
        """REGRESSÃO: o leitor preenche com zeros além do fim físico; se o
        motor pedir o tamanho declarado, ele grava um arquivo 'completo' com
        zeros inventados. O pedido é limitado ao que existe => parcial real."""
        corpo = jpeg_real(160, 120, semente=30)
        img = construir_fat32([ArquivoTeste("cortado.jpg", corpo, apagado=True)])
        corte = img.find(corpo[:64]) + len(corpo) // 3
        res = self.executar(self.imagem(img[:corte]))
        self.assertEqual(res.recuperados, 1)
        c = res.candidatos[0]
        self.assertEqual(c.estado, ESTADO_PARCIAL)
        self.assertEqual(c.tamanho, len(corpo))        # tamanho declarado
        self.assertAlmostEqual(c.fracao_recuperada, 1 / 3, delta=0.08)
        with open(c.arquivo_destino, "rb") as f:
            gravado = f.read()
        self.assertLess(len(gravado), len(corpo))      # só o que existe
        self.assertEqual(gravado, corpo[:len(gravado)])
        self.assertNotIn(b"\x00" * 512, gravado)      # nada de zeros inventados

    def test_nenhuma_leitura_passa_de_8mib(self):
        """REGRESSÃO: o bloco de varredura era 8 MiB + 64 KiB de sobreposição."""
        self.assertLessEqual(assinaturas.LIMITE_LEITURA, 8 * 1024 * 1024)
        self.assertEqual(assinaturas.BLOCO_VARREDURA + assinaturas.SOBREPOSIÇÃO,
                         assinaturas.LIMITE_LEITURA)
        img = construir_fat32([ArquivoTeste("m.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        maximo = [0]
        original = aquisicao.LeitorArquivo.read

        def espiao(leitor, offset, size):
            maximo[0] = max(maximo[0], size)
            return original(leitor, offset, size)

        aquisicao.LeitorArquivo.read = espiao
        try:
            self.executar(caminho, cartao_inteiro=True)
        finally:
            aquisicao.LeitorArquivo.read = original
        self.assertGreater(maximo[0], 0)
        self.assertLessEqual(maximo[0], 8 * 1024 * 1024)

    def test_cancelamento_na_etapa2_aparece_no_resultado(self):
        """REGRESSÃO: cancelar durante a etapa 2 terminava a recuperação com
        cancelado=False — o usuário não sabia que o trabalho foi interrompido."""
        img = construir_fat32([ArquivoTeste("livre.bin",
                                            jpeg_real(200, 150, semente=31) * 3)])
        caminho = self.imagem(img)
        cancelar = threading.Event()
        fases = []

        def progresso(fracao, total, fase):
            fases.append(fase)
            if fase == "carving":
                cancelar.set()

        opcoes = motor.Opcoes(origem=caminho,
                              destino=tempfile.mkdtemp(prefix="rf4_reg_c_",
                                                       dir=self.pasta),
                              permitir_mesmo_disco=True, min_kb=0,
                              cartao_inteiro=True)
        res = motor.Motor(opcoes, progresso=progresso, cancelar=cancelar).executar()
        self.assertIn("carving", fases)
        self.assertTrue(res.cancelado)

    def test_busca_de_particoes_perdidas_avisada(self):
        """REGRESSÃO: a busca automática de partições perdidas rodava sem
        nenhum aviso no relatório."""
        vol = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        disco = montar_disco_mbr([(2048, len(vol) // 512, vol, 0x0C)],
                                 disco_setores=2048 + len(vol) // 512 + 64,
                                 apagar_mbr=True)
        res = self.executar(self.imagem(disco))
        self.assertIn("p.jpg", [c.nome for c in res.candidatos])
        self.assertTrue(any("perdida" in a for a in res.avisos),
                        f"avisos: {res.avisos}")

    def test_candidato_de_tamanho_zero_e_ignorado(self):
        """Entrada de diretório com tamanho 0 não pode virar arquivo vazio."""
        img = construir_fat32([ArquivoTeste("vazio.jpg", b"", apagado=True),
                               ArquivoTeste("bom.jpg", self.jp, apagado=True)])
        res = self.executar(self.imagem(img))
        nomes = [c.nome for c in res.candidatos]
        self.assertIn("bom.jpg", nomes)
        self.assertNotIn("vazio.jpg", nomes)
        for c in res.candidatos:
            self.assertGreater(c.tamanho, 0)


# ===========================================================================
# 10. Segurança: origem intocada, sem sobrescrita, destino validado
# ===========================================================================


class TesteSegurancaRegressoes(BaseImagem):
    @staticmethod
    def _sha(caminho):
        h = hashlib.sha256()
        with open(caminho, "rb") as f:
            for bloco in iter(lambda: f.read(1 << 20), b""):
                h.update(bloco)
        return h.hexdigest()

    def test_origem_nao_e_modificada(self):
        img = construir_fat32([ArquivoTeste("a.jpg", self.jp, apagado=True),
                               ArquivoTeste("b.png", png_real(24, 18), apagado=True)])
        caminho = self.imagem(img)
        antes = self._sha(caminho)
        self.executar(caminho, cartao_inteiro=True, videos=True, profunda=True)
        self.assertEqual(self._sha(caminho), antes)

    def test_leitor_de_arquivo_fecha_o_descritor(self):
        """REGRESSÃO: LeitorArquivo.fechar() só marcava uma flag — o descritor
        continuava aberto. No Windows isso trava o arquivo de imagem (não dá
        para mover/apagar) e vaza descritores a cada recuperação."""
        import gc
        import warnings
        caminho = self.imagem(construir_fat32(
            [ArquivoTeste("d.jpg", self.jp, apagado=True)]))
        with warnings.catch_warnings(record=True) as captados:
            warnings.simplefilter("always")
            self.executar(caminho)
            gc.collect()
        avisos = [str(w.message) for w in captados
                  if issubclass(w.category, ResourceWarning)]
        self.assertEqual(avisos, [], f"descritores vazando: {avisos}")
        leitor = aquisicao.abrir_origem(caminho)
        leitor.fechar()
        self.assertTrue(leitor._fechado)
        with self.assertRaises(aquisicao.ErroLeitura):
            leitor.read(0, 512)
        # fechar duas vezes não pode explodir
        leitor.fechar()

    def test_motivo_da_contiguidade_e_explicito(self):
        """Quando a cadeia da FAT foi zerada, o relatório precisa dizer que a
        recuperação se baseou em contiguidade (nada de motivo vazio)."""
        img = construir_fat32([ArquivoTeste("m.jpg", self.jp, apagado=True)])
        fs = fatfs.criar_desde_boot(LeitorMemoria(img), img[:512], 0, len(img))
        c = next(x for x in fs.recuperar() if x.nome.endswith("m.jpg"))
        self.assertIn("cont", c.motivo.lower())
        self.assertIn("cadeia", c.motivo.lower())

    def test_abertura_e_somente_leitura(self):
        caminho = self.imagem(construir_fat32([]))
        leitor = aquisicao.abrir_origem(caminho)
        try:
            self.assertIn(leitor._fh.mode, ("rb",))
            # aberto sem "+": o próprio objeto de arquivo recusa a escrita
            with self.assertRaises(io.UnsupportedOperation):
                leitor._fh.write(b"\x00")
        finally:
            leitor.fechar()

    def test_destino_no_mesmo_disco_da_origem_e_recusado(self):
        caminho = self.imagem(construir_fat32([]))
        leitor = aquisicao.abrir_origem(caminho)
        try:
            valid = aquisicao.validar_destino(leitor, os.path.dirname(caminho),
                                              permitir_mesmo_disco=False)
            self.assertFalse(valid.ok)
            valido = aquisicao.validar_destino(leitor, os.path.dirname(caminho),
                                               permitir_mesmo_disco=True)
            self.assertTrue(valido.ok)
        finally:
            leitor.fechar()

    def test_nome_invalido_e_saneado(self):
        img = construir_ntfs([ArquivoTeste('fo*to?.jpg', self.jp, apagado=True)])
        res = self.executar(self.imagem(img))
        for c in res.candidatos:
            base = os.path.basename(c.arquivo_destino)
            for proibido in '\\/:*?"<>|':
                self.assertNotIn(proibido, base)
            self.assertTrue(os.path.exists(c.arquivo_destino))

    def test_cli_bloqueia_imagem_no_mesmo_disco(self):
        """REGRESSÃO: --criar-imagem gravava a imagem sem checar o disco."""
        caminho = self.imagem(construir_fat32([]))
        destino_img = os.path.join(self.pasta, "bloq.img")
        codigo = subprocess.call(
            [sys.executable, os.path.join(RAIZ, "recupera_fotos.py"), caminho,
             "--criar-imagem", destino_img],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(codigo, 2)
        self.assertFalse(os.path.exists(destino_img))

    def test_cli_aceita_imagem_no_mesmo_disco_quando_autorizado(self):
        caminho = self.imagem(construir_fat32([]))
        destino_img = os.path.join(self.pasta, "ok.img")
        codigo = subprocess.call(
            [sys.executable, os.path.join(RAIZ, "recupera_fotos.py"), caminho,
             "--criar-imagem", destino_img, "--permitir-mesmo-disco"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(codigo, 0)
        self.assertTrue(os.path.exists(destino_img))
        self.assertTrue(os.path.exists(destino_img + ".map.jsonl"))


class TesteTamanhoExatoDosVolumes(unittest.TestCase):
    """Defeito real encontrado pelo benchmark de 2 GiB: o rótulo do volume
    FAT32 era gravado com 12 bytes (b"TESTE       ") em um campo de 11
    (offset 71..82). Como a atribuição é por fatia, o bytearray do setor de
    boot crescia para 513 bytes, o campo FilSysType ("FAT32   ") era empurrado
    para o offset 83 e TODA imagem de volume saía 2 bytes maior que
    tam_setores * tam_setor (setor de boot + cópia de reserva no setor 6).

    Nenhum teste anterior pegava isso porque o identificador de boot não lê o
    rótulo. Uma imagem 2 bytes maior, porém, desloca tudo o que vem depois do
    volume em um disco montado — exatamente o cenário do benchmark.
    """

    def test_setor_de_boot_fat32_tem_512_bytes_e_campos_alinhados(self):
        vol = construir_fat32([], tam_setores=4096, spc=8, tam_setor=512)
        boot = vol[:512]
        self.assertEqual(len(boot), 512)
        self.assertEqual(boot[510:512], b"\x55\xAA")
        self.assertEqual(boot[82:90], b"FAT32   ",
                         "FilSysType deslocado: o rótulo tem mais de 11 bytes")
        self.assertEqual(len(boot[71:82]), 11)
        self.assertTrue(boot[71:82].startswith(b"TESTE"))
        self.assertEqual(boot[3:11], b"MSDOS5.0")

    def test_copia_de_reserva_e_identica_ao_boot(self):
        vol = construir_fat32([], tam_setores=4096, spc=8, tam_setor=512)
        self.assertEqual(vol[0:512], vol[6 * 512:7 * 512])

    def test_volume_fat32_tem_tamanho_exato(self):
        for setores, spc in ((4096, 8), (32768, 8), (65536, 16)):
            vol = construir_fat32([], tam_setores=setores, spc=spc, tam_setor=512)
            self.assertEqual(len(vol), setores * 512,
                             f"setores={setores} spc={spc}")

    def test_volume_fat16_tem_tamanho_exato(self):
        vol = construir_fat16([], tam_setores=32768, spc=4, tam_setor=512)
        self.assertEqual(len(vol), 32768 * 512)
        self.assertEqual(vol[54:62], b"FAT16   ")
        self.assertEqual(len(vol[43:54]), 11)
        self.assertEqual(vol[510:512], b"\x55\xAA")

    def test_volumes_exfat_e_ntfs_tem_tamanho_exato(self):
        ex = construir_exfat([])
        self.assertEqual(len(ex) % 512, 0)
        self.assertEqual(ex[3:11], b"EXFAT   ")
        nt = construir_ntfs([])
        self.assertEqual(len(nt) % 512, 0)
        self.assertEqual(nt[3:11], b"NTFS    ")

    def test_identificador_de_boot_le_os_campos_certos(self):
        vol = construir_fat32([], tam_setores=4096, spc=8, tam_setor=512)
        tipo, info = particoes.identificar_boot(vol[:512])
        self.assertEqual(tipo, "fat32")
        self.assertEqual(info["spc"], 8)
        self.assertEqual(info["bytes_setor"], 512)

    def test_disco_montado_com_volume_fat32_nao_desloca_as_particoes(self):
        """O volume cabe exatamente no espaço reservado pela tabela de
        partições: nada do que vier depois é empurrado por bytes sobrando."""
        setores_volume = 4096
        vol = construir_fat32([ArquivoTeste("A.JPG", jpeg_real(64, 48),
                                            apagado=True)],
                              tam_setores=setores_volume, spc=8, tam_setor=512)
        self.assertEqual(len(vol), setores_volume * 512)
        disco = montar_disco_mbr([(2048, setores_volume, vol, 0x0C)],
                                 disco_setores=2048 + setores_volume + 64)
        self.assertEqual(len(disco), (2048 + setores_volume + 64) * 512)
        self.assertEqual(disco[2048 * 512:2048 * 512 + len(vol)], vol)
        # e a área depois da partição continua zerada, não deslocada
        self.assertEqual(disco[(2048 + setores_volume) * 512:],
                         b"\x00" * (64 * 512))


class TesteExFatComprimentoDoNome(unittest.TestCase):
    """O byte 3 do Stream extension declara quantos caracteres o nome tem
    (FileNameLength). Antes, esse byte era lido e jogado fora: se faltassem
    entradas de nome no conjunto, o programa montava um nome incompleto e
    ainda tentava validá-lo pelo hash.

    Agora o total de caracteres trazido pelas entradas é comparado com o
    declarado; se não bater, o conjunto é tratado como incompleto (e o hash do
    nome não é usado como validação, porque não pode bater mesmo).
    """

    NOME = "FOTO_MUITO_LONGA_PARA_EXFAT_2024.JPG"      # 36 caracteres -> 3 entradas

    def _imagem(self, declarado: int | None = None) -> bytes:
        corpo = jpeg_real(64, 48, semente=5)
        img = bytearray(construir_exfat(
            [ArquivoTeste(self.NOME, corpo, apagado=True)]))
        bruto = self.NOME.encode("utf-16-le")
        achados = []
        for off in range(0, len(img) - 96, 32):
            if img[off] in (0xC0, 0x40) and img[off + 3] == len(self.NOME):
                if bytes(img[off + 36:off + 36 + 28]) == bruto[:28]:
                    achados.append(off)
        self.assertTrue(achados, "entrada de Stream do arquivo não localizada")
        if declarado is not None:
            for off in achados:
                img[off + 3] = declarado
        return bytes(img)

    def _recuperar(self, img: bytes):
        _tipo, info = particoes.identificar_boot(img[:512])
        self.assertEqual(_tipo, "exfat")
        fs = exfat.SistemaExFAT(LeitorMemoria(img), info, 0, len(img))
        return [c for c in fs.recuperar() if c.nome == self.NOME]

    def test_nome_completo_valida_o_hash(self):
        cands = self._recuperar(self._imagem())
        self.assertEqual(len(cands), 1)
        self.assertIs(cands[0].extra["hash_nome_ok"], True)

    def test_nome_incompleto_nao_e_validado_pelo_hash(self):
        # declara mais caracteres do que as entradas de nome realmente trazem
        cands = self._recuperar(self._imagem(declarado=len(self.NOME) + 2))
        self.assertEqual(len(cands), 1)
        self.assertIsNone(cands[0].extra["hash_nome_ok"],
                          "nome incompleto não pode ser validado pelo hash")

    def test_nome_com_zero_declarado_nao_quebra(self):
        """FileNameLength = 0 (campo zerado por dano) não pode derrubar a
        recuperação: o nome lido das entradas continua sendo usado."""
        cands = self._recuperar(self._imagem(declarado=0))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].nome, self.NOME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
