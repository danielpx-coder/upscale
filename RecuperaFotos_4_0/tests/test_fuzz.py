"""
test_fuzz.py — robustez do Recupera Fotos 4.0 com dados hostis/aleatórios.

Objetivo: um cartão corrompido é a regra, não a exceção. Estes testes
alimentam o programa com bytes aleatórios, estruturas truncadas e imagens com
bits flipados e exigem SEMPRE as mesmas cinco garantias:

  1. nenhuma exceção não tratada escapa (o programa não pode quebrar no meio
     da recuperação);
  2. nada trava: cada operação termina dentro de um limite de tempo;
  3. nenhum arquivo é declarado "ok" sem verificação real (zero falso-ok);
  4. todo byte gravado veio da origem — nada é inventado (cada fragmento
     confere byte a byte com a imagem);
  5. a origem não é modificada e nada é gravado fora do destino escolhido
     (inclusive quando nomes de arquivo corrompidos tentam "../").

Tudo é determinístico (sementes fixas), então uma falha é reproduzível.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import struct
import sys
import tempfile
import time
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
from candidatos import ESTADO_CORROMPIDO, ESTADO_NAO_VERIFICADO, ESTADO_OK, ESTADO_PARCIAL
from construtor import (
    ArquivoTeste,
    construir_exfat,
    construir_fat16,
    construir_fat32,
    construir_ntfs,
    gravar,
    jpeg_real,
    montar_disco_gpt,
    montar_disco_mbr,
    png_real,
)
from verificacao import LeitorBytes

try:
    from PIL import Image  # noqa: F401 - só sondando se o Pillow está instalado
    PIL_OK = True
except ImportError:
    PIL_OK = False

LIMITE_TEMPO_ANALISE = 5.0        # segundos por operação de análise
LIMITE_TEMPO_MOTOR = 60.0         # segundos por recuperação completa
# todas as extensões que o programa anuncia recuperar
EXTENSOES_ANUNCIADAS = sorted(
    set(list(A.__dict__.get("EXTENSOES_FUZZ", []))
        + ["jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "psd",
           "raf", "cr2", "cr3", "nef", "nrw", "arw", "dng", "orf", "rw2",
           "pef", "srw", "heic", "heif", "avif", "mp4", "mov", "m4v", "3gp",
           "avi", "mts", "m2ts", "mkv"]))


def flipar(dados: bytes, semente: int, n_flips: int = 8) -> bytearray:
    """Altera n bytes aleatórios (determinístico pela semente)."""
    rnd = random.Random(semente)
    b = bytearray(dados)
    for _ in range(min(n_flips, len(b))):
        i = rnd.randrange(len(b))
        b[i] ^= 1 << rnd.randrange(8)
    return b


def truncar(dados: bytes, semente: int) -> bytes:
    rnd = random.Random(semente)
    if len(dados) < 2:
        return dados
    return dados[:rnd.randrange(1, len(dados))]


class LeitorMemoria(aquisicao.LeitorBase):
    def __init__(self, dados: bytes):
        super().__init__()
        self._dados = dados

    @property
    def tamanho(self) -> int:
        return len(self._dados)

    def read(self, offset: int, size: int) -> bytes:
        if offset >= len(self._dados) or size <= 0:
            return b""
        p = self._dados[offset:offset + size]
        return p + b"\x00" * (size - len(p)) if len(p) < size else p


# ===========================================================================
# 1. Analisadores de assinatura com bytes aleatórios
# ===========================================================================


class TesteFuzzAnalisadores(unittest.TestCase):
    ANALISADORES = [
        ("jpeg", assinaturas.analisar_jpeg),
        ("png", assinaturas.analisar_png),
        ("gif", assinaturas.analisar_gif),
        ("bmp", assinaturas.analisar_bmp),
        ("tiff", assinaturas.analisar_tiff),
        ("raf", assinaturas.analisar_raf),
        ("psd", assinaturas.analisar_psd),
        ("riff", assinaturas.analisar_riff),
        ("mp4", assinaturas.analisar_mp4),
        ("mkv", assinaturas.analisar_mkv),
        ("mpegts", assinaturas.analisar_mpegts),
    ]

    def _rodar(self, fn, dados, fim_regiao=None):
        ini = time.time()
        leitor = LeitorBytes(dados)
        try:
            if fn in (assinaturas.analisar_jpeg, assinaturas.analisar_mp4,
                      assinaturas.analisar_mkv, assinaturas.analisar_mpegts):
                r = fn(leitor, 0, fim_regiao=fim_regiao)
            else:
                r = fn(leitor, 0)
        finally:
            self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE,
                            f"{getattr(fn, '__name__', fn)} demorou demais")
        return r

    def test_bytes_aleatorios_nao_quebram_os_analisadores(self):
        rnd = random.Random(1234)
        for _ in range(40):
            n = rnd.choice([0, 1, 7, 64, 511, 4096, 70000])
            dados = bytes(rnd.randrange(256) for _ in range(n))
            for nome, fn in self.ANALISADORES:
                r = self._rodar(fn, dados, fim_regiao=len(dados))
                if r is not None and r.comprimento:
                    self.assertGreater(r.comprimento, 0, nome)
                    self.assertLessEqual(r.comprimento,
                                         max(len(dados), 1) + (1 << 20), nome)

    def test_assinatura_valida_com_lixo_depois(self):
        """Cabeçalho verdadeiro + lixo: o comprimento não pode explodir."""
        cabecalos = [b"\xFF\xD8\xFF\xE0", b"\x89PNG\r\n\x1a\n", b"GIF89a",
                     b"BM", b"II*\x00", b"MM\x00*", b"FUJIFILMCCD-RAW",
                     b"8BPS", b"RIFF", b"\x1a\x45\xdf\xa3", b"\x47"]
        rnd = random.Random(99)
        lixo = bytes(rnd.randrange(256) for _ in range(200000))
        for cab in cabecalos:
            dados = cab + lixo
            for nome, fn in self.ANALISADORES:
                r = self._rodar(fn, dados, fim_regiao=len(dados))
                if r is not None and r.comprimento:
                    self.assertLessEqual(r.comprimento, len(dados) + (1 << 20),
                                         f"{nome} com cabeçalho {cab!r}")

    def test_arquivos_reais_truncados_em_todos_os_pontos(self):
        """Truncamento em posições variadas nunca gera comprimento além do
        que existe, nem estado 'ok' na verificação."""
        amostras = [("jpg", jpeg_real(64, 48, semente=1)),
                    ("png", png_real(32, 24)),
                    ("gif", A.gif_sintetico()),
                    ("bmp", A.bmp_sintetico()),
                    ("tif", A.tiff_sintetico()),
                    ("mp4", A.mp4_sintetico()),
                    ("webp", A.webp_real(24, 18))]
        for ext, blob in amostras:
            for corte in (1, len(blob) // 3, len(blob) // 2, len(blob) - 1):
                if corte <= 0 or corte >= len(blob):
                    continue
                pedaço = blob[:corte]
                v = verificacao.classificar(pedaço, ext,
                                            tamanho_esperado=len(blob))
                self.assertIn(v.estado, (ESTADO_PARCIAL, ESTADO_CORROMPIDO,
                                         ESTADO_NAO_VERIFICADO),
                              f"{ext} truncado em {corte} deu {v.estado}")
                self.assertNotEqual(v.estado, ESTADO_OK)

    def test_varrer_regiao_sobre_dados_aleatorios(self):
        rnd = random.Random(7)
        dados = bytes(rnd.randrange(256) for _ in range(300000))
        ini = time.time()
        achados = list(assinaturas.varrer_regiao(LeitorBytes(dados), 0,
                                                 len(dados), min_bytes=0))
        self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE * 4)
        for off, r in achados:
            self.assertGreaterEqual(off, 0)
            self.assertTrue(r.comprimento)
            # nada de "arquivo" que ultrapasse absurdamente a região
            self.assertLessEqual(off + r.comprimento, len(dados) + (1 << 20))
        # nenhum lixo aleatório pode ser declarado íntegro
        for off, r in achados:
            v = verificacao.classificar(dados[off:off + r.comprimento],
                                        r.extensao)
            self.assertNotEqual(v.estado, ESTADO_OK,
                                f"falso ok em {off} ({r.extensao})")


# ===========================================================================
# 2. Verificação: nenhum falso "ok"
# ===========================================================================


class TesteFuzzVerificacao(unittest.TestCase):
    def test_lixo_nunca_e_ok(self):
        rnd = random.Random(2024)
        for ext in EXTENSOES_ANUNCIADAS:
            for k in range(12):
                n = rnd.choice([1, 16, 512, 8192, 100000])
                dados = bytes(rnd.randrange(256) for _ in range(n))
                v = verificacao.classificar(dados, ext)
                self.assertNotEqual(v.estado, ESTADO_OK,
                                    f"falso ok para {ext} ({n} bytes, caso {k})")
                self.assertIn(v.estado, (ESTADO_OK, ESTADO_NAO_VERIFICADO,
                                         ESTADO_CORROMPIDO, ESTADO_PARCIAL))

    def test_zeros_nunca_sao_ok(self):
        for ext in EXTENSOES_ANUNCIADAS:
            for n in (1, 512, 65536):
                v = verificacao.classificar(b"\x00" * n, ext)
                self.assertNotEqual(v.estado, ESTADO_OK, f"zeros {ext} {n}")

    def test_arquivo_vazio_e_corrompido(self):
        for ext in EXTENSOES_ANUNCIADAS:
            self.assertEqual(verificacao.classificar(b"", ext).estado,
                             ESTADO_CORROMPIDO)

    def test_extensao_desconhecida_nao_quebra(self):
        for ext in ("", "zzz", ".JPG", "JPG", "jpeg ", None):
            v = verificacao.classificar(jpeg_real(32, 24, semente=2), ext or "")
            self.assertIn(v.estado, (ESTADO_OK, ESTADO_NAO_VERIFICADO))

    def test_data_exif_com_tiff_corrompido(self):
        rnd = random.Random(5)
        for _ in range(30):
            dados = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 4096)))
            self.assertIsInstance(verificacao.data_exif(dados), (tuple, type(None)))

    def test_verificadores_estruturais_com_bytes_mutados(self):
        blob = jpeg_real(80, 60, semente=3)
        rnd = random.Random(11)
        for k in range(40):          # k é a semente de cada mutação
            mutado = flipar(blob, semente=k, n_flips=rnd.randrange(1, 40))
            v = verificacao.verificar_jpeg_estrutural(bytes(mutado))
            self.assertIn(v.estado, (ESTADO_OK, ESTADO_PARCIAL,
                                     ESTADO_CORROMPIDO))
            final = verificacao.classificar(bytes(mutado), "jpg")
            if final.estado == ESTADO_OK:
                # só é 'ok' se a estrutura também não a condenou
                self.assertNotEqual(v.estado, ESTADO_CORROMPIDO)
            if v.estado == ESTADO_CORROMPIDO:
                # estrutura condenada nunca pode ser resgatada para 'ok'
                self.assertNotEqual(final.estado, ESTADO_OK)


# ===========================================================================
# 3. Estruturas NTFS hostis
# ===========================================================================


class TesteFuzzNTFS(unittest.TestCase):
    def test_runlist_aleatoria(self):
        rnd = random.Random(31)
        for _ in range(200):
            n = rnd.randrange(0, 200)
            dados = bytes(rnd.randrange(256) for _ in range(n))
            rl = ntfs.RunList.parsear(dados)
            self.assertIsNotNone(rl)
            self.assertLessEqual(len(rl.runs), ntfs.MAX_RUNS)
            for lcn, nc, sp in rl.runs:
                self.assertGreater(nc, 0)          # run degenerado nunca entra
                if sp:
                    self.assertEqual(lcn, -1)      # sparse não tem LCN
            self.assertEqual(rl.total_clusters(),
                             sum(n for _l, n, _s in rl.runs))
            frags = rl.fragmentos(0, 4096, tamanho=rnd.randrange(0, 1 << 20))
            for f in frags:
                # nenhum fragmento pode apontar para antes do volume
                self.assertGreaterEqual(f.offset, 0)
                self.assertGreater(f.tamanho, 0)

    def test_registro_mft_aleatorio(self):
        rnd = random.Random(32)
        for _ in range(120):
            rec = bytearray(bytes(rnd.randrange(256) for _ in range(1024)))
            rec[0:4] = rnd.choice([b"FILE", b"BAAD", b"\x00\x00\x00\x00", b"XYZW"])
            reg = ntfs.parsear_registro(rnd.randrange(0, 100), bytes(rec), 1024, 512)
            if reg is not None:
                reg.nomes()
                reg.dados_principais()
                _ = reg.em_uso, reg.diretorio

    def test_lznt1_aleatorio(self):
        rnd = random.Random(33)
        for _ in range(120):
            n = rnd.randrange(0, 20000)
            dados = bytes(rnd.randrange(256) for _ in range(n))
            saida = ntfs.descomprimir_lznt1(dados, 1 << 20)
            if saida is not None:
                self.assertLessEqual(len(saida), 1 << 20)

    def test_imagem_ntfs_com_bits_flipados(self):
        base = construir_ntfs([ArquivoTeste("n.jpg", jpeg_real(64, 48, semente=4),
                                            apagado=True)])
        for semente in range(12):
            img = bytes(flipar(base, semente=semente, n_flips=40))
            leitor = LeitorMemoria(img)
            _t, info = particoes.identificar_boot(img[:512])
            if not info:
                continue
            fs = ntfs.SistemaNTFS(leitor, info, 0, len(img))
            ini = time.time()
            cands, recusas = fs.recuperar()
            self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE * 2)
            for rec in recusas:
                self.assertIn("motivo", rec)
            for c in cands:
                self.assertGreaterEqual(c.tamanho, 0)
                for f in c.fragmentos:
                    self.assertGreaterEqual(f.offset, 0)
                    self.assertLessEqual(f.offset + f.tamanho,
                                         len(img) + fs.setor * fs.spc)
            fs.leitor.fechar()


# ===========================================================================
# 4. Estruturas FAT/exFAT hostis
# ===========================================================================


class TesteFuzzSistemasDeArquivos(unittest.TestCase):
    def _fs_fat(self, img):
        _t, info = particoes.identificar_boot(img[:512])
        if not info:
            return None
        return fatfs.criar_desde_boot(LeitorMemoria(img), img[:512], 0, len(img))

    def test_fat32_com_bits_flipados(self):
        base = construir_fat32([ArquivoTeste("f.jpg", jpeg_real(64, 48, semente=5),
                                             apagado=True),
                                ArquivoTeste("g.png", png_real(32, 24),
                                             apagado=True)])
        for semente in range(12):
            img = bytes(flipar(base, semente=semente, n_flips=30))
            fs = self._fs_fat(img)
            if fs is None:
                continue
            ini = time.time()
            cands = fs.recuperar(profunda=(semente % 2 == 0))
            self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE * 2)
            for c in cands:
                for f in c.fragmentos:
                    self.assertLessEqual(f.offset + f.tamanho,
                                         len(img) + fs.cluster_bytes)

    def test_fat16_com_bits_flipados(self):
        base = construir_fat16([ArquivoTeste("f16.jpg",
                                             jpeg_real(48, 36, semente=6),
                                             apagado=True)])
        for semente in range(6):
            img = bytes(flipar(base, semente=semente, n_flips=25))
            fs = self._fs_fat(img)
            if fs is None:
                continue
            fs.recuperar()

    def test_exfat_com_bits_flipados(self):
        base = construir_exfat([ArquivoTeste("e.jpg", jpeg_real(64, 48, semente=7),
                                             apagado=True)])
        for semente in range(10):
            img = bytes(flipar(base, semente=semente, n_flips=30))
            _t, info = particoes.identificar_boot(img[:512])
            if not info:
                continue
            fs = exfat.SistemaExFAT(LeitorMemoria(img), info, 0, len(img))
            ini = time.time()
            fs.recuperar()
            self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE * 2)

    def test_boot_aleatorio_nao_vira_sistema_de_arquivos(self):
        rnd = random.Random(41)
        for _ in range(300):
            setor = bytearray(bytes(rnd.randrange(256) for _ in range(512)))
            setor[510:512] = b"\x55\xAA"
            tipo, info = particoes.identificar_boot(bytes(setor))
            if tipo:
                self.assertIsInstance(info, dict)
                self.assertEqual(info.get("tipo"), tipo)
                if "bytes_setor" in info:
                    self.assertIn(info["bytes_setor"], (512, 1024, 2048, 4096))
                if "spc" in info:
                    self.assertGreater(info["spc"], 0)

    def test_cadeia_de_fat_ciclica_nao_trava(self):
        """FAT com ciclo (cluster aponta para si mesmo / para trás)."""
        img = bytearray(construir_fat32(
            [ArquivoTeste("c.jpg", jpeg_real(64, 48, semente=8), apagado=True)]))
        _t, info = particoes.identificar_boot(bytes(img[:512]))
        fat_ini = info["reservados"] * info["bytes_setor"]
        for k in range(info["nfats"]):
            off = fat_ini + k * info["fat_size"] * info["bytes_setor"]
            struct.pack_into("<I", img, off + 3 * 4, 4)     # 3 -> 4
            struct.pack_into("<I", img, off + 4 * 4, 3)     # 4 -> 3 (ciclo)
        fs = fatfs.criar_desde_boot(LeitorMemoria(bytes(img)), bytes(img[:512]),
                                    0, len(img))
        ini = time.time()
        cands = fs.recuperar()
        self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE)
        self.assertTrue(cands)
        for c in cands:
            self.assertLessEqual(sum(f.tamanho for f in c.fragmentos), len(img))

    def test_entrada_de_diretorio_com_nome_hostil(self):
        """Nomes corrompidos/maliciosos com '../' não podem escapar do destino
        (path traversal) — nem pelo NTFS, nem pelo FAT."""
        corpo = jpeg_real(64, 48, semente=9)
        for nome_hostil in ("..\\..\\evil.jpg", "../../evil.jpg",
                            "..\\\\..\\\\x.JPG"):
            for sistema, construtor in (("ntfs", construir_ntfs),
                                        ("fat32", construir_fat32)):
                img = construtor([ArquivoTeste(nome_hostil, corpo, apagado=True)])
                pasta = tempfile.mkdtemp(prefix="rf4_fuzz_h_")
                try:
                    caminho = gravar(os.path.join(pasta, "hostil.img"), img)
                    destino = os.path.join(pasta, "saida")
                    res = motor.Motor(motor.Opcoes(
                        origem=caminho, destino=destino,
                        permitir_mesmo_disco=True, min_kb=0)).executar()
                    self.assertTrue(res.candidatos, f"{sistema}: {nome_hostil}")
                    for c in res.candidatos:
                        self.assertTrue(c.arquivo_destino)
                        real = os.path.realpath(c.arquivo_destino)
                        self.assertTrue(
                            real.startswith(os.path.realpath(destino) + os.sep),
                            f"{sistema} {nome_hostil}: gravou em {real}")
                        base = os.path.basename(real)
                        # ".." como TEXTO é inofensivo; o que não pode existir
                        # é separador de caminho nem nome feito só de pontos
                        self.assertNotIn(os.sep, base)
                        self.assertNotIn("/", base)
                        self.assertNotIn("\\", base)
                        self.assertNotEqual(base.strip("."), "")
                        for proibido in '\\/:*?"<>|':
                            self.assertNotIn(proibido, os.path.basename(real))
                finally:
                    shutil.rmtree(pasta, ignore_errors=True)


# ===========================================================================
# 5. Tabelas de partição hostis
# ===========================================================================


class TesteFuzzParticoes(unittest.TestCase):
    def test_mbr_aleatorio(self):
        rnd = random.Random(51)
        disco = bytearray(bytes(rnd.randrange(256) for _ in range(1 << 20)))
        disco[510:512] = b"\x55\xAA"
        leitor = LeitorMemoria(bytes(disco))
        ini = time.time()
        parts = particoes.detectar_particoes(leitor, setor=512, buscar_perdidas=True)
        self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE * 4)
        for p in parts:
            self.assertGreaterEqual(p.inicio, 0)
            self.assertGreater(p.tamanho, 0)
            self.assertLessEqual(p.inicio + p.tamanho, len(disco) + 4096)

    def test_gpt_aleatorio(self):
        rnd = random.Random(52)
        disco = bytearray(bytes(rnd.randrange(256) for _ in range(2 << 20)))
        leitor = LeitorMemoria(bytes(disco))
        gpt = particoes.ler_gpt(leitor, 1, 512)
        if gpt:
            for p in gpt["particoes"]:
                self.assertLessEqual(p["lba_ini"], p["lba_fim"])

    def test_ebr_ciclico_nao_trava(self):
        """Cadeia de EBRs que aponta para si mesma: limite de 128 saltos."""
        vol = construir_fat32([ArquivoTeste("e.jpg", jpeg_real(48, 36, semente=10),
                                            apagado=True)], tam_setores=8192)
        n = len(vol) // 512
        disco = bytearray(montar_disco_mbr(
            [(2048, 8192, bytes(8192 * 512), 0x0F)],
            disco_setores=2048 + 8192 + n + 64))
        # EBR no setor 2048 apontando para si mesmo
        ebr = bytearray(512)
        ebr[446 + 4] = 0x07
        struct.pack_into("<I", ebr, 446 + 8, 4096)      # lógica em 2048+4096
        ebr[450 + 4] = 0x0F
        struct.pack_into("<I", ebr, 450 + 8, 0)         # próxima = ela mesma
        ebr[510:512] = b"\x55\xAA"
        disco[2048 * 512:2049 * 512] = ebr
        leitor = LeitorMemoria(bytes(disco))
        ini = time.time()
        parts = particoes.detectar_particoes(leitor, setor=512)
        self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE)
        self.assertLessEqual(len(parts), 130)

    def test_busca_de_particoes_perdidas_em_disco_aleatorio(self):
        rnd = random.Random(53)
        disco = bytes(rnd.randrange(256) for _ in range(4 << 20))
        leitor = LeitorMemoria(disco)
        ini = time.time()
        achadas = particoes.buscar_particoes_perdidas(leitor, setor=512)
        self.assertLess(time.time() - ini, LIMITE_TEMPO_ANALISE * 4)
        for p in achadas:
            self.assertLessEqual(p.inicio + p.tamanho, len(disco) + 4096)

    def test_gpt_com_particoes_sobrepostas(self):
        vol1 = construir_fat32([ArquivoTeste("o1.jpg", jpeg_real(48, 36, semente=12),
                                             apagado=True)], tam_setores=8192)
        n1 = len(vol1) // 512
        disco = montar_disco_gpt([(2048, n1, vol1, "FOTOS"),
                                  (2048, n1, vol1, "FOTOS2")],
                                 disco_setores=2048 + n1 + 4096)
        pasta = tempfile.mkdtemp(prefix="rf4_fuzz_gpt_")
        try:
            caminho = gravar(os.path.join(pasta, "sobre.img"), disco)
            destino = os.path.join(pasta, "saida")
            res = motor.Motor(motor.Opcoes(origem=caminho, destino=destino,
                                           permitir_mesmo_disco=True,
                                           min_kb=0)).executar()
            nomes = [c.nome for c in res.candidatos]
            self.assertEqual(len([n for n in nomes if n == "o1.jpg"]), 1)
            self.assertGreaterEqual(res.duplicados_regiao + res.duplicados_hash, 0)
        finally:
            shutil.rmtree(pasta, ignore_errors=True)


# ===========================================================================
# 6. Motor ponta-a-ponta com imagens corrompidas
# ===========================================================================


class TesteFuzzMotor(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_fuzzm_")
        self.jp = jpeg_real(96, 72, semente=20)
        self.png = png_real(48, 36)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    @staticmethod
    def _sha(caminho):
        h = hashlib.sha256()
        with open(caminho, "rb") as f:
            for bloco in iter(lambda: f.read(1 << 20), b""):
                h.update(bloco)
        return h.hexdigest()

    def _rodar(self, img: bytes, nome_img: str, **kw):
        caminho = gravar(os.path.join(self.pasta, nome_img), img)
        antes = self._sha(caminho)
        destino = tempfile.mkdtemp(prefix="rf4_fuzz_d_", dir=self.pasta)
        kw.setdefault("min_kb", 0)
        opcoes = motor.Opcoes(origem=caminho, destino=destino,
                              permitir_mesmo_disco=True, **kw)
        ini = time.time()
        res = motor.Motor(opcoes).executar()
        self.assertLess(time.time() - ini, LIMITE_TEMPO_MOTOR,
                        f"motor demorou demais em {nome_img}")
        self.assertEqual(self._sha(caminho), antes, "a origem foi modificada!")
        return res, img, destino

    def _checar_invariantes(self, res, img, destino):
        """As três garantias que valem para QUALQUER entrada."""
        for c in res.candidatos:
            # (a) estado pertence ao conjunto declarado
            self.assertIn(c.estado, (ESTADO_OK, ESTADO_NAO_VERIFICADO,
                                     ESTADO_CORROMPIDO, ESTADO_PARCIAL))
            self.assertGreater(c.tamanho, 0)
            if not c.arquivo_destino:
                continue
            # (b) nada gravado fora do destino
            real = os.path.realpath(c.arquivo_destino)
            self.assertTrue(real.startswith(os.path.realpath(destino)),
                            f"{c.nome}: gravado fora do destino ({real})")
            self.assertTrue(os.path.exists(real))
            with open(real, "rb") as f:
                gravado = f.read()
            self.assertGreater(len(gravado), 0, f"{c.nome}: arquivo vazio")
            # (c) zero falso-ok: quem é 'ok' precisa verificar de novo
            if c.estado == ESTADO_OK:
                v = verificacao.classificar(gravado, c.extensao)
                self.assertEqual(v.estado, ESTADO_OK,
                                 f"{c.nome}: 'ok' no relatório mas "
                                 f"{v.estado} na reverificação ({v.motivo})")
            # (d) nada inventado: cada fragmento confere com a origem
            if (not c.extra.get("comprimido") and not c.extra.get("residente")
                    and c.fragmentos):
                esperado = b"".join(img[f.offset:f.offset + f.tamanho]
                                    for f in c.fragmentos)
                self.assertEqual(gravado, esperado[:len(gravado)],
                                 f"{c.nome}: bytes gravados não vieram da origem")
                if c.estado == ESTADO_OK:
                    self.assertEqual(len(gravado), c.tamanho)
        # (e) contadores coerentes
        self.assertEqual(res.recuperados, len(res.candidatos))
        self.assertGreaterEqual(res.duplicados_hash, 0)
        self.assertGreaterEqual(res.duplicados_regiao, 0)
        self.assertEqual(res.por_estado.get(ESTADO_OK, 0),
                         len([c for c in res.candidatos if c.estado == ESTADO_OK]))

    def test_fat32_flipado(self):
        base = construir_fat32([ArquivoTeste("a.jpg", self.jp, apagado=True),
                                ArquivoTeste("b.png", self.png, apagado=True),
                                ArquivoTeste("viva.jpg", self.jp, apagado=False)])
        for semente in range(8):
            img = bytes(flipar(base, semente=semente, n_flips=50))
            res, i, d = self._rodar(img, f"f32_{semente}.img",
                                    cartao_inteiro=(semente % 2 == 0),
                                    profunda=(semente % 3 == 0))
            self._checar_invariantes(res, i, d)

    def test_fat32_truncado(self):
        base = construir_fat32([ArquivoTeste("a.jpg", self.jp, apagado=True)])
        for semente in range(6):
            img = truncar(base, semente=semente)
            if len(img) < 4096:
                continue
            res, i, d = self._rodar(img, f"f32t_{semente}.img")
            self._checar_invariantes(res, i, d)
            for c in res.candidatos:
                self.assertNotEqual(c.estado, ESTADO_OK if c.fracao_recuperada
                                    and c.fracao_recuperada < 1.0 else "",
                                    "parcial declarado como ok")

    def test_ntfs_flipado(self):
        base = construir_ntfs([ArquivoTeste("n.jpg", self.jp, apagado=True),
                               ArquivoTeste("n.png", self.png, apagado=True)])
        for semente in range(8):
            img = bytes(flipar(base, semente=semente, n_flips=50))
            res, i, d = self._rodar(img, f"ntfs_{semente}.img")
            self._checar_invariantes(res, i, d)

    def test_exfat_flipado(self):
        base = construir_exfat([ArquivoTeste("e.jpg", self.jp, apagado=True)])
        for semente in range(6):
            img = bytes(flipar(base, semente=semente, n_flips=40))
            res, i, d = self._rodar(img, f"exf_{semente}.img")
            self._checar_invariantes(res, i, d)

    def test_disco_aleatorio_inteiro(self):
        rnd = random.Random(61)
        img = bytes(rnd.randrange(256) for _ in range(1 << 20))
        res, i, d = self._rodar(img, "aleatorio.img", cartao_inteiro=True,
                                videos=True)
        self._checar_invariantes(res, i, d)

    def test_imagem_toda_zerada(self):
        res, i, d = self._rodar(b"\x00" * (1 << 20), "zerada.img",
                                cartao_inteiro=True, videos=True, profunda=True)
        self._checar_invariantes(res, i, d)
        self.assertEqual(res.recuperados, 0)
        self.assertTrue(res.avisos)

    def test_imagem_toda_ff(self):
        res, i, d = self._rodar(b"\xFF" * (1 << 20), "ff.img",
                                cartao_inteiro=True, videos=True)
        self._checar_invariantes(res, i, d)

    def test_jpeg_fragmentado_e_sobrescrito(self):
        """Metade de uma foto sobrescrita por outra: o resultado não pode ser
        'ok' — ou é corrompido (RST fora de ordem) ou parcial."""
        corpo = bytearray(A.jpeg_lixo_no_meio(self.jp))
        img = construir_fat32([ArquivoTeste("q.jpg", bytes(corpo), apagado=True)])
        res, i, d = self._rodar(img, "quebrado.img")
        self._checar_invariantes(res, i, d)
        for c in res.candidatos:
            if c.extensao == "jpg" and c.metodo == "varredura":
                self.assertNotEqual(c.estado, ESTADO_OK)

    def test_muitos_arquivos_apagados(self):
        arquivos = [ArquivoTeste(f"m{k:03d}.jpg",
                                 jpeg_real(32, 24, semente=k), apagado=True)
                    for k in range(40)]
        img = construir_fat32(arquivos, tam_setores=65536)
        res, i, d = self._rodar(img, "muitos.img", profunda=True)
        self._checar_invariantes(res, i, d)
        self.assertGreaterEqual(res.recuperados, 40)
        nomes = {c.nome for c in res.candidatos}
        self.assertEqual(len(nomes), len(res.candidatos), "nomes duplicados")

    def test_nome_gigante_e_caracteres_de_controle(self):
        nome = "ç" * 60 + "\x01\x02" + "A" * 60 + ".jpg"
        img = construir_ntfs([ArquivoTeste(nome, self.jp, apagado=True)])
        res, i, d = self._rodar(img, "nomeg.img")
        self._checar_invariantes(res, i, d)
        for c in res.candidatos:
            base = os.path.basename(c.arquivo_destino)
            self.assertLessEqual(len(base), 255)
            for ch in base:
                self.assertGreaterEqual(ord(ch), 32,
                                        f"caractere de controle no nome: {base!r}")

    def test_relatorios_csv_validos_com_entradas_hostis(self):
        import csv as modulo_csv
        base = construir_ntfs([ArquivoTeste("r.jpg", self.jp, apagado=True)])
        img = bytes(flipar(base, semente=5, n_flips=20))
        res, _i, _d = self._rodar(img, "csv.img")
        if res.relatorio_csv:
            with open(res.relatorio_csv, encoding="utf-8-sig", newline="") as f:
                linhas = list(modulo_csv.reader(f, delimiter=";"))
            self.assertTrue(linhas)
            self.assertEqual(len(linhas[0]), len(motor.COLUNAS_CSV))
            for linha in linhas[1:]:
                self.assertEqual(len(linha), len(motor.COLUNAS_CSV))
                self.assertIn(linha[motor.COLUNAS_CSV.index("estado")],
                              (ESTADO_OK, ESTADO_NAO_VERIFICADO,
                               ESTADO_CORROMPIDO, ESTADO_PARCIAL, "recusado"))

    def test_cancelamento_aleatorio_nao_corrompe_o_destino(self):
        import threading
        base = construir_fat32([ArquivoTeste(f"c{k}.jpg",
                                             jpeg_real(64, 48, semente=k),
                                             apagado=True) for k in range(12)],
                               tam_setores=65536)
        caminho = gravar(os.path.join(self.pasta, "canc.img"), base)
        destino = tempfile.mkdtemp(prefix="rf4_fuzz_c_", dir=self.pasta)
        cancelar = threading.Event()
        rnd = random.Random(71)

        def interromper():
            time.sleep(rnd.uniform(0.0, 0.05))
            cancelar.set()

        t = threading.Thread(target=interromper, daemon=True)
        t.start()
        res = motor.Motor(motor.Opcoes(origem=caminho, destino=destino,
                                       permitir_mesmo_disco=True, min_kb=0,
                                       profunda=True),
                          cancelar=cancelar).executar()
        t.join(timeout=5)
        self._checar_invariantes(res, base, destino)
        # nenhum arquivo pela metade no destino
        for c in res.candidatos:
            if c.arquivo_destino and os.path.exists(c.arquivo_destino) \
                    and c.estado == ESTADO_OK:
                self.assertEqual(os.path.getsize(c.arquivo_destino), c.tamanho)


if __name__ == "__main__":
    unittest.main(verbosity=2)
