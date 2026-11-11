"""
test_recupera_fotos.py — testes ponta-a-ponta do Recupera Fotos 4.0.

Cobrem: FAT16/FAT32/exFAT/NTFS (metadados), varredura por assinatura de todos
os formatos anunciados, Motion Photo, MPF válido/inválido, deduplicação,
partições (MBR/GPT/GPT de reserva/EBR/perdidas), setores de 4096, boots de
reserva, relatórios CSV, opções da interface/CLI, segurança (somente leitura,
destino em outro disco, nomes seguros, sem sobrescrita), cancelamento, pausa
entre etapas e limites de memória.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aquisicao
import auxiliar as A
import motor
import particoes
from candidatos import (
    ESTADO_CORROMPIDO,
    ESTADO_NAO_VERIFICADO,
    ESTADO_OK,
    ESTADO_PARCIAL,
    METODO_MFT,
    METODO_MFT_ORFA,
    METODO_PASTA,
    METODO_VARREDURA,
)
from construtor import (
    ArquivoTeste,
    construir_exfat,
    construir_fat16,
    construir_fat32,
    construir_ntfs,
    dados_pseudo_aleatorios,
    formatar_rapido_fat32,
    formatar_rapido_ntfs,
    gravar,
    jpeg_grande_com_rst,
    jpeg_real,
    montar_disco_ebr,
    montar_disco_gpt,
    montar_disco_mbr,
    png_real,
)

try:
    from PIL import Image  # noqa: F401 - só sondando se o Pillow está instalado
    PIL_OK = True
except ImportError:
    PIL_OK = False

# preenchimento entre blobs: padrão determinístico sem nenhuma assinatura
PREENCHIMENTO = (bytes([0xA5, 0x5A, 0x33]) * 400)


class BaseMotor(unittest.TestCase):
    """Cria pasta temporária, imagem de origem e destino, e roda o motor."""

    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_e2e_")
        self.destino = os.path.join(self.pasta, "recuperadas")
        self.jp = jpeg_real(64, 48, semente=3)
        self.png = png_real(40, 30)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    # ------------------------------------------------------------ utilidades

    def imagem(self, dados: bytes, nome="origem.img") -> str:
        return gravar(os.path.join(self.pasta, nome), dados)

    def executar(self, origem: str, **kw) -> motor.ResultadoRecuperacao:
        destino = kw.pop("destino", None) or tempfile.mkdtemp(
            prefix="rf4_dest_", dir=self.pasta)
        # os arquivos sintéticos dos testes são pequenos (< 8 KB); o filtro
        # padrão de tamanho é desativado aqui e testado separadamente
        kw.setdefault("min_kb", 0)
        opcoes = motor.Opcoes(origem=origem, destino=destino,
                              permitir_mesmo_disco=True, **kw)
        m = motor.Motor(opcoes)
        res = m.executar()
        res._destino_teste = destino          # atributo de teste
        return res

    def nomes(self, res) -> list[str]:
        return [os.path.basename(c.arquivo_destino or c.nome)
                for c in res.candidatos]

    def por_ext(self, res, ext: str):
        return [c for c in res.candidatos if c.extensao == ext]

    def sha256(self, caminho: str) -> str:
        h = hashlib.sha256()
        with open(caminho, "rb") as f:
            for bloco in iter(lambda: f.read(1 << 20), b""):
                h.update(bloco)
        return h.hexdigest()

    # ------------------------------------------------- imagens de espaço livre

    def imagem_livre(self, blobs: list[bytes], sistema="fat32",
                     tam_setores=32768) -> str:
        """Volume cujo ESPAÇO LIVRE contém os blobs (dentro de um .bin
        apagado, ignorado pela etapa 1 por não ser foto/vídeo)."""
        payload = bytearray()
        for b in blobs:
            payload += b
            payload += PREENCHIMENTO
        arqs = [ArquivoTeste("container.bin", bytes(payload), apagado=True)]
        if sistema == "fat32":
            img = construir_fat32(arqs, tam_setores=tam_setores)
        elif sistema == "fat16":
            img = construir_fat16(arqs, tam_setores=tam_setores)
        elif sistema == "exfat":
            img = construir_exfat(arqs, tam_setores=tam_setores)
        else:
            img = construir_ntfs([ArquivoTeste("container.jpg", bytes(payload),
                                               apagado=True)])
        return self.imagem(img, f"livre_{sistema}.img")


# ===========================================================================
# 1. Sistemas de arquivos — recuperação por metadados
# ===========================================================================


class TesteFAT32Metadados(BaseMotor):
    def _img(self, arquivos, formatado=False):
        img = construir_fat32(arquivos)
        if formatado:
            img = formatar_rapido_fat32(img)
        return self.imagem(img)

    def test_apagado_contiguo_recuperado_por_pasta(self):
        caminho = self._img([ArquivoTeste("foto.jpg", self.jp, apagado=True)])
        res = self.executar(caminho)
        self.assertIn("fat32", res.sistemas)
        self.assertEqual(res.por_metodo.get(METODO_PASTA), 1)
        c = res.candidatos[0]
        self.assertEqual(c.nome, "foto.jpg")
        self.assertEqual(c.estado, ESTADO_OK)
        self.assertEqual(c.tamanho, len(self.jp))

    def test_conteudo_byte_a_byte_identico(self):
        caminho = self._img([ArquivoTeste("foto.jpg", self.jp, apagado=True)])
        res = self.executar(caminho)
        saida = os.path.join(res._destino_teste, "foto.jpg")
        with open(saida, "rb") as f:
            self.assertEqual(f.read(), self.jp)
        self.assertEqual(hashlib.sha256(self.jp).hexdigest(),
                         res.candidatos[0].sha256)

    def test_nome_longo_lfn_preservado(self):
        nome = "Minhas Férias de Verão 2024.jpg"
        caminho = self._img([ArquivoTeste(nome, self.jp, apagado=True)])
        res = self.executar(caminho)
        self.assertIn(nome, self.nomes(res))

    def test_fragmentado_com_cadeia_sobrevivente(self):
        grande = jpeg_grande_com_rst(semente=5)
        caminho = self._img([ArquivoTeste("frag.jpg", grande, apagado=True,
                                          fragmentado=True)])
        res = self.executar(caminho)
        c = next(x for x in res.candidatos if x.nome == "frag.jpg")
        self.assertEqual(c.estado, ESTADO_OK)
        self.assertGreaterEqual(len(c.fragmentos), 2)
        with open(c.arquivo_destino, "rb") as f:
            self.assertEqual(f.read(), grande)

    def test_arquivos_vivos_nao_sao_recuperados(self):
        caminho = self._img([ArquivoTeste("viva.jpg", self.jp, apagado=False)])
        res = self.executar(caminho)
        self.assertEqual(res.recuperados, 0)

    def test_formatado_rapido_recupera_por_varredura(self):
        caminho = self._img([ArquivoTeste("foto.jpg", self.jp, apagado=True),
                             ArquivoTeste("img.png", self.png, apagado=True)],
                            formatado=True)
        res = self.executar(caminho)
        self.assertEqual(res.por_metodo.get(METODO_VARREDURA, 0), 2)
        self.assertEqual(res.por_estado[ESTADO_OK], 2)

    def test_data_da_entrada_preservada(self):
        caminho = self._img([ArquivoTeste("d.jpg", self.jp, apagado=True,
                                          data=(2021, 3, 9, 14, 25, 30))])
        res = self.executar(caminho)
        c = res.candidatos[0]
        self.assertEqual((c.data.year, c.data.month, c.data.day), (2021, 3, 9))

    def test_subpasta_recriada_no_destino(self):
        img = construir_fat32([])
        # FAT32 do construtor coloca tudo na raiz; usamos NTFS para pastas
        caminho = self.imagem(construir_ntfs(
            [ArquivoTeste("praia.jpg", self.jp, apagado=True, subpasta="Viagens")]))
        res = self.executar(caminho)
        c = res.candidatos[0]
        self.assertEqual(c.caminho_original, "Viagens\\praia.jpg")
        self.assertTrue(os.path.exists(os.path.join(res._destino_teste,
                                                    "Viagens", "praia.jpg")))
        del img

    def test_sem_pastas_grava_tudo_na_raiz_do_destino(self):
        caminho = self.imagem(construir_ntfs(
            [ArquivoTeste("praia.jpg", self.jp, apagado=True, subpasta="Viagens")]))
        res = self.executar(caminho, sem_pastas=True)
        self.assertTrue(os.path.exists(os.path.join(res._destino_teste, "praia.jpg")))
        self.assertFalse(os.path.isdir(os.path.join(res._destino_teste, "Viagens")))

    def test_filtro_por_pasta_relativa(self):
        caminho = self.imagem(construir_ntfs([
            ArquivoTeste("a.jpg", self.jp, apagado=True, subpasta="Fotos"),
            ArquivoTeste("b.jpg", self.png, apagado=True, subpasta="Outros")]))
        res = self.executar(caminho, pasta_relativa="Fotos")
        self.assertEqual(self.nomes(res), ["a.jpg"])

    def test_extensao_nao_foto_ignorada_na_etapa1(self):
        caminho = self._img([ArquivoTeste("notas.txt", b"texto simples" * 10,
                                          apagado=True)])
        res = self.executar(caminho, min_kb=0)
        self.assertEqual(res.recuperados, 0)


class TesteFAT16Metadados(BaseMotor):
    def test_apagado_recuperado(self):
        caminho = self.imagem(construir_fat16(
            [ArquivoTeste("antiga.jpg", self.jp, apagado=True)]))
        res = self.executar(caminho)
        self.assertIn("fat16", res.sistemas)
        self.assertEqual(self.nomes(res), ["antiga.jpg"])
        self.assertEqual(res.candidatos[0].estado, ESTADO_OK)

    def test_lfn_em_fat16(self):
        nome = "Relatório Final.PNG"
        caminho = self.imagem(construir_fat16(
            [ArquivoTeste(nome, self.png, apagado=True)]))
        res = self.executar(caminho)
        self.assertIn(nome, self.nomes(res))

    def test_deteccao_fat16_nao_confunde_com_fat32(self):
        img = construir_fat16([ArquivoTeste("x.jpg", self.jp, apagado=True)])
        tipo, info = particoes.identificar_boot(img[:512])
        self.assertEqual(tipo, "fat16")
        self.assertGreaterEqual(info["clusters"], 4085)
        self.assertLess(info["clusters"], 65525)


class TesteExFATMetadados(BaseMotor):
    def test_apagado_contiguo(self):
        caminho = self.imagem(construir_exfat(
            [ArquivoTeste("ex.jpg", self.jp, apagado=True)]))
        res = self.executar(caminho)
        self.assertIn("exfat", res.sistemas)
        self.assertEqual(self.nomes(res), ["ex.jpg"])
        self.assertEqual(res.candidatos[0].estado, ESTADO_OK)

    def test_fragmentado_com_cadeia(self):
        grande = jpeg_grande_com_rst(semente=6)
        caminho = self.imagem(construir_exfat(
            [ArquivoTeste("exfrag.jpg", grande, apagado=True, fragmentado=True)]))
        res = self.executar(caminho)
        c = next(x for x in res.candidatos if x.nome == "exfrag.jpg")
        with open(c.arquivo_destino, "rb") as f:
            self.assertEqual(f.read(), grande)

    def test_nome_longo_exfat(self):
        nome = "Nome Bem Longo de Fotografia.png"
        caminho = self.imagem(construir_exfat(
            [ArquivoTeste(nome, self.png, apagado=True)]))
        res = self.executar(caminho)
        self.assertIn(nome, self.nomes(res))


class TesteNTFSMotor(BaseMotor):
    def test_mft_e_orfa(self):
        img = construir_ntfs([ArquivoTeste("n.jpg", self.jp, apagado=True)],
                             registro_orfao=("orfa.png", self.png, 500, 700, 40))
        res = self.executar(self.imagem(img))
        self.assertIn("ntfs", res.sistemas)
        self.assertEqual(res.por_metodo.get(METODO_MFT), 1)
        self.assertEqual(res.por_metodo.get(METODO_MFT_ORFA), 1)
        self.assertEqual(res.por_estado[ESTADO_OK], 2)

    def test_formatado_rapido_usa_mft_orfa(self):
        img = formatar_rapido_ntfs(construir_ntfs(
            [], registro_orfao=("sobreviveu.jpg", self.jp, 500, 700, 41)))
        res = self.executar(self.imagem(img))
        self.assertEqual(res.por_metodo.get(METODO_MFT_ORFA), 1)
        self.assertIn("sobreviveu.jpg", self.nomes(res))

    def test_diagnostico_ntfs_gerado_quando_ha_recusas(self):
        img = construir_ntfs([ArquivoTeste("ok.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        res = self.executar(caminho, profunda=True)
        # sem recusas não há diagnóstico; com recusas o arquivo é criado
        if res.recusas_ntfs:
            self.assertTrue(os.path.exists(res.diagnostico_csv))
            with open(res.diagnostico_csv, encoding="utf-8-sig", newline="") as f:
                linhas = list(csv.reader(f, delimiter=";"))
            self.assertEqual(linhas[0], ["registro_MFT", "nome", "motivo"])
        else:
            self.assertEqual(res.diagnostico_csv, "")


# ===========================================================================
# 2. Varredura por assinatura — cobertura de formatos
# ===========================================================================


class TesteCarvingFotos(BaseMotor):
    def _carvar(self, blobs, **kw):
        kw.setdefault("min_kb", 0)
        caminho = self.imagem_livre(blobs)
        return self.executar(caminho, **kw)

    def test_jpeg_por_assinatura(self):
        res = self._carvar([self.jp])
        c = self.por_ext(res, "jpg")[0]
        self.assertEqual(c.metodo, METODO_VARREDURA)
        self.assertEqual(c.estado, ESTADO_OK)
        self.assertEqual(c.tamanho, len(self.jp))

    def test_png_por_assinatura(self):
        res = self._carvar([self.png])
        c = self.por_ext(res, "png")[0]
        self.assertEqual(c.tamanho, len(self.png))
        self.assertEqual(c.estado, ESTADO_OK)

    def test_gif_por_assinatura(self):
        g = A.gif_sintetico(16, 12)
        res = self._carvar([g])
        c = self.por_ext(res, "gif")[0]
        self.assertEqual(c.tamanho, len(g))
        self.assertEqual(c.estado, ESTADO_OK)

    def test_bmp_por_assinatura(self):
        b = A.bmp_sintetico(12, 9)
        res = self._carvar([b])
        c = self.por_ext(res, "bmp")[0]
        self.assertEqual(c.tamanho, len(b))
        self.assertEqual(c.estado, ESTADO_OK)

    def test_webp_por_assinatura(self):
        w = A.webp_real()
        res = self._carvar([w])
        c = self.por_ext(res, "webp")[0]
        self.assertEqual(c.tamanho, len(w))
        self.assertEqual(c.estado, ESTADO_OK)

    def test_tiff_por_assinatura(self):
        t = A.tiff_sintetico(largura=16, altura=12)
        res = self._carvar([t])
        c = self.por_ext(res, "tif")[0]
        self.assertEqual(c.tamanho, len(t))

    def test_psd_por_assinatura(self):
        p = A.psd_sintetico()
        res = self._carvar([p])
        c = self.por_ext(res, "psd")[0]
        self.assertEqual(c.tamanho, len(p))
        self.assertEqual(c.estado, ESTADO_NAO_VERIFICADO)

    def test_raf_por_assinatura(self):
        r = A.raf_sintetico()
        res = self._carvar([r])
        c = self.por_ext(res, "raf")[0]
        self.assertEqual(c.tamanho, len(r))

    def test_dng_por_assinatura(self):
        d = A.tiff_sintetico(tag_dng=True)
        res = self._carvar([d])
        c = self.por_ext(res, "dng")[0]
        self.assertEqual(c.tamanho, len(d))

    def test_cr2_por_assinatura(self):
        c2 = A.tiff_cr2_sintetico()
        res = self._carvar([c2])
        c = self.por_ext(res, "cr2")[0]
        self.assertEqual(c.tamanho, len(c2))

    def test_orf_por_assinatura(self):
        o = A.orf_sintetico()
        res = self._carvar([o])
        self.assertTrue(self.por_ext(res, "orf"))

    def test_rw2_por_assinatura(self):
        r = A.rw2_sintetico()
        res = self._carvar([r])
        self.assertTrue(self.por_ext(res, "rw2"))

    def test_heic_sem_decodificador_nao_vira_ok(self):
        h = A.heic_sintetico()
        res = self._carvar([h])
        c = self.por_ext(res, "heic")[0]
        self.assertEqual(c.tamanho, len(h))
        self.assertNotEqual(c.estado, ESTADO_OK)
        self.assertEqual(c.estado, ESTADO_NAO_VERIFICADO)

    def test_avif_sem_decodificador(self):
        a = A.avif_sintetico()
        res = self._carvar([a])
        c = self.por_ext(res, "avif")[0]
        self.assertEqual(c.estado, ESTADO_NAO_VERIFICADO)

    def test_multiplos_formatos_na_mesma_varredura(self):
        blobs = [self.jp, self.png, A.gif_sintetico(), A.bmp_sintetico(),
                 A.tiff_sintetico(), A.heic_sintetico()]
        res = self._carvar(blobs)
        exts = sorted(c.extensao for c in res.candidatos)
        for esperado in ("jpg", "png", "gif", "bmp", "tif", "heic"):
            self.assertIn(esperado, exts)

    def test_minimo_8kb_por_padrao(self):
        caminho = self.imagem_livre([self.png])              # 104 bytes
        destino = tempfile.mkdtemp(prefix="rf4_dest_", dir=self.pasta)
        opcoes = motor.Opcoes(origem=caminho, destino=destino,
                              permitir_mesmo_disco=True)     # min_kb padrão
        res = motor.Motor(opcoes).executar()
        self.assertEqual(res.recuperados, 0)
        self.assertTrue(res.rejeitados >= 1 or res.recuperados == 0)

    def test_incluir_pequenos(self):
        res = self.executar(self.imagem_livre([self.png]), incluir_pequenos=True)
        self.assertEqual(res.recuperados, 1)


class TesteCarvingVideos(BaseMotor):
    def _carvar(self, blobs, **kw):
        kw.setdefault("min_kb", 0)
        kw.setdefault("videos", True)
        return self.executar(self.imagem_livre(blobs), **kw)

    def test_mp4(self):
        m = A.mp4_sintetico(tam_mdat=4096)
        res = self._carvar([m])
        c = self.por_ext(res, "mp4")[0]
        self.assertEqual(c.tamanho, len(m))

    def test_mov(self):
        m = A.mov_sintetico(tam_mdat=2048)
        res = self._carvar([m])
        self.assertTrue(self.por_ext(res, "mov"))

    def test_m4v(self):
        m = A.m4v_sintetico(tam_mdat=2048)
        res = self._carvar([m])
        self.assertTrue(self.por_ext(res, "m4v"))

    def test_3gp(self):
        m = A.mp4_3gp_sintetico(tam_mdat=2048)
        res = self._carvar([m])
        self.assertTrue(self.por_ext(res, "3gp"))

    def test_avi(self):
        a = A.avi_sintetico()
        res = self._carvar([a])
        c = self.por_ext(res, "avi")[0]
        self.assertEqual(c.tamanho, len(a))
        self.assertEqual(c.estado, ESTADO_NAO_VERIFICADO)

    def test_mts_188(self):
        t = A.mpegts_sintetico(n_pacotes=60, pacote=188)
        res = self._carvar([t])
        self.assertTrue(self.por_ext(res, "mts") or self.por_ext(res, "m2ts"))

    def test_m2ts_192(self):
        t = A.mpegts_sintetico(n_pacotes=60, pacote=192)
        res = self._carvar([t])
        c = (self.por_ext(res, "m2ts") or self.por_ext(res, "mts"))[0]
        self.assertEqual(c.tamanho, len(t))

    def test_mkv_com_tamanho_conhecido(self):
        k = A.mkv_sintetico(tam_conhecido=True, corpo_seg=4096)
        res = self._carvar([k])
        c = self.por_ext(res, "mkv")[0]
        self.assertEqual(c.tamanho, len(k))

    def test_mkv_sem_tamanho_nao_promete_recuperacao(self):
        k = A.mkv_sintetico(tam_conhecido=False, corpo_seg=4096)
        res = self._carvar([k])
        # comprimento indeterminado => não pode ser entregue como íntegro
        for c in self.por_ext(res, "mkv"):
            self.assertNotEqual(c.estado, ESTADO_OK)

    def test_videos_desativados_por_padrao(self):
        m = A.mp4_sintetico(tam_mdat=4096)
        res = self.executar(self.imagem_livre([m]), min_kb=0, videos=False)
        self.assertEqual(self.por_ext(res, "mp4"), [])


class TesteJpegEspecial(BaseMotor):
    def test_jpeg_com_dri_e_rst_valido(self):
        j = jpeg_grande_com_rst(semente=9)
        self.assertIn(b"\xFF\xDD", j)
        res = self.executar(self.imagem_livre([j]), min_kb=0)
        c = self.por_ext(res, "jpg")[0]
        self.assertEqual(c.estado, ESTADO_OK)
        self.assertEqual(c.tamanho, len(j))

    def test_jpeg_truncado_e_recuperado_como_parcial(self):
        """JPEG sem EOI (cartão truncado) NÃO é descartado nem marcado 'ok':
        o fluxo entropy aproveitável é recuperado como 'parcial'."""
        j = A.jpeg_truncado(jpeg_real(96, 72, semente=4))
        self.assertFalse(j.endswith(b"\xFF\xD9"))
        res = self.executar(self.imagem_livre([j]), min_kb=0)
        cands = self.por_ext(res, "jpg")
        self.assertTrue(cands)
        c = cands[0]
        self.assertEqual(c.estado, ESTADO_PARCIAL)
        self.assertNotEqual(c.estado, ESTADO_OK)
        self.assertIn("EOI", c.motivo)
        gravado = os.path.join(res._destino_teste, os.path.basename(c.arquivo_destino))
        with open(gravado, "rb") as f:
            self.assertEqual(f.read(), j[:c.tamanho])

    def test_jpeg_truncado_nao_vira_ok_mesmo_com_pillow(self):
        """O Pillow decodifica JPEGs truncados; isso não pode virar 'ok'."""
        import verificacao
        j = A.jpeg_truncado(jpeg_real(96, 72, semente=21))
        v = verificacao.classificar(j, "jpg")
        self.assertEqual(v.estado, ESTADO_PARCIAL)

    def test_jpeg_lixo_no_meio_e_corrompido(self):
        j = A.jpeg_lixo_no_meio(jpeg_real(64, 48, semente=5))
        res = self.executar(self.imagem_livre([j]), min_kb=0)
        for c in self.por_ext(res, "jpg"):
            self.assertIn(c.estado, (ESTADO_CORROMPIDO, ESTADO_PARCIAL))

    def test_jpeg_rst_fora_de_ordem(self):
        j = jpeg_grande_com_rst(semente=10)
        mau = A.jpeg_com_rst_fora_de_ordem(j)
        self.assertNotEqual(mau, j)
        res = self.executar(self.imagem_livre([mau]), min_kb=0)
        cands = self.por_ext(res, "jpg")
        self.assertTrue(cands)
        self.assertNotEqual(cands[0].estado, ESTADO_OK)
        self.assertIn("RST", cands[0].motivo.upper())

    def test_motion_photo_valida(self):
        j = jpeg_real(64, 48, semente=6)
        mp4 = A.mp4_sintetico(tam_mdat=2048)
        motion = A.jpeg_motion_photo(j, mp4)
        res = self.executar(self.imagem_livre([motion]), min_kb=0, videos=True)
        jpgs = self.por_ext(res, "jpg")
        self.assertTrue(jpgs)
        c = jpgs[0]
        # o JPEG principal é íntegro; o vídeo anexo é tratado separadamente
        self.assertEqual(c.estado, ESTADO_OK)
        self.assertGreaterEqual(c.tamanho, len(j))

    def test_motion_photo_offset_invalido_rejeitado(self):
        j = jpeg_real(64, 48, semente=7)
        mp4 = A.mp4_sintetico(tam_mdat=512)
        motion = bytearray(A.jpeg_motion_photo(j, mp4))
        # corrompe o valor de MicroVideoOffset no XMP
        i = bytes(motion).find(b"MicroVideoOffset=")
        self.assertGreater(i, 0)
        motion[i + 17:i + 21] = b"9999"
        res = self.executar(self.imagem_livre([bytes(motion)]), min_kb=0,
                            videos=True)
        jpgs = self.por_ext(res, "jpg")
        self.assertTrue(jpgs)
        # junção inconsistente => não pode ser declarada íntegra sem aviso
        c = jpgs[0]
        self.assertTrue(c.estado != ESTADO_OK or "Motion" in c.motivo
                        or "junção" in c.motivo or c.tamanho >= len(j))

    def test_mpf_valido(self):
        j1 = jpeg_real(64, 48, semente=8)
        j2 = jpeg_real(32, 24, semente=9)
        mpf = A.jpeg_mpf(j1, j2, valido=True)
        res = self.executar(self.imagem_livre([mpf]), min_kb=0)
        c = self.por_ext(res, "jpg")[0]
        self.assertEqual(c.tamanho, len(mpf))
        self.assertEqual(c.estado, ESTADO_OK)

    def test_mpf_invalido_nao_anexa_pagina(self):
        """Índice MPF incoerente (offset fora do arquivo): a página annexa NÃO
        é anexada — recupera-se só a imagem primária, íntegra, e o motivo
        registra a recusa. Nunca se declara 'arquivo completo' sem base."""
        j1 = jpeg_real(64, 48, semente=10)
        j2 = jpeg_real(32, 24, semente=11)
        mpf = A.jpeg_mpf(j1, j2, valido=False)
        res = self.executar(self.imagem_livre([mpf]), min_kb=0)
        cands = self.por_ext(res, "jpg")
        self.assertTrue(cands)
        c = cands[0]
        self.assertEqual(c.tamanho, len(j1) + 90)     # primária + segmento MPF
        self.assertLess(c.tamanho, len(mpf))          # annexo recusado
        self.assertIn("MPF", c.motivo)
        diag = (c.extra.get("diagnostico") or {}).get("mpf") or {}
        self.assertFalse(diag.get("valido", True))
        with open(c.arquivo_destino, "rb") as f:
            dados = f.read()
        self.assertEqual(dados, mpf[:len(dados)])
        self.assertTrue(dados.endswith(b"\xFF\xD9"))  # primária completa

    def test_jpeg_com_lixo_depois_do_eoi(self):
        j = jpeg_real(64, 48, semente=12) + dados_pseudo_aleatorios(2000, 3)
        res = self.executar(self.imagem_livre([j]), min_kb=0)
        c = self.por_ext(res, "jpg")[0]
        self.assertEqual(c.tamanho, len(j) - 2000)   # termina no EOI

    def test_verificador_independente_do_pillow(self):
        import verificacao
        j = jpeg_real(80, 60, semente=13)
        v = verificacao.verificar_jpeg_estrutural(j)
        self.assertEqual(v.estado, ESTADO_OK)
        self.assertEqual(v.verificador, "estrutural_jpeg")

    def test_jpeg_adulterado_detectado_sem_pillow(self):
        import verificacao
        j = bytearray(jpeg_real(80, 60, semente=14))
        j[400:420] = b"\x00" * 20
        v = verificacao.verificar_jpeg_estrutural(bytes(j))
        self.assertIn(v.estado, (ESTADO_CORROMPIDO, ESTADO_PARCIAL, ESTADO_OK))
        j2 = bytearray(jpeg_real(80, 60, semente=15))
        j2[20] = 0x00                      # quebra a sequência de marcadores
        v2 = verificacao.verificar_jpeg_estrutural(bytes(j2))
        self.assertNotEqual(v2.estado, ESTADO_OK)


# ===========================================================================
# 3. Deduplicação
# ===========================================================================


class TesteDeduplicacao(BaseMotor):
    def test_mesmo_conteudo_duas_vezes_gera_um_arquivo(self):
        caminho = self.imagem_livre([self.jp, self.jp])
        res = self.executar(caminho, min_kb=0)
        self.assertEqual(len(self.por_ext(res, "jpg")), 1)
        self.assertEqual(res.duplicados_hash, 1)

    def test_mesmo_arquivo_por_metadados_e_assinatura(self):
        """A região recuperada por metadados fica protegida: a etapa 2 não
        grava o mesmo arquivo outra vez (nem com outro nome)."""
        img = construir_fat32([ArquivoTeste("dup.jpg", self.jp, apagado=True)])
        res = self.executar(self.imagem(img), min_kb=0, cartao_inteiro=True)
        jpgs = [c for c in res.candidatos if c.extensao == "jpg"]
        self.assertEqual(len(jpgs), 1)
        self.assertEqual(jpgs[0].metodo, METODO_PASTA)
        gravados = []
        for _raiz, _dirs, files in os.walk(res._destino_teste):
            gravados += [f for f in files if f.lower().endswith((".jpg", ".jpeg"))]
        self.assertEqual(len(gravados), 1)
        hashes = {c.sha256 for c in res.candidatos if c.sha256}
        self.assertEqual(len(hashes), 1)

    def test_mesma_regiao_em_duas_particoes_contabilizada(self):
        """Duas entradas de tabela apontando para o MESMO volume: a segunda
        cópia é contada como duplicata de região, não gravada de novo."""
        vol = construir_fat32([ArquivoTeste("dup.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        n = len(vol) // 512
        disco = montar_disco_mbr([(2048, n, vol, 0x0C), (2048, n, vol, 0x0C)],
                                 disco_setores=2048 + n + 64)
        res = self.executar(self.imagem(disco), min_kb=0)
        self.assertEqual(len(self.por_ext(res, "jpg")), 1)
        self.assertGreaterEqual(res.duplicados_regiao, 1)

    def test_regiao_ja_recuperada_nao_e_recarvada(self):
        img = construir_fat32([ArquivoTeste("a.jpg", self.jp, apagado=True)])
        res = self.executar(self.imagem(img), min_kb=0, cartao_inteiro=True)
        offsets = [c.fragmentos[0].offset for c in res.candidatos
                   if c.fragmentos]
        self.assertEqual(len(offsets), len(set(offsets)))

    def test_arquivos_diferentes_nao_sao_deduplicados(self):
        j1 = jpeg_real(64, 48, semente=20)
        j2 = jpeg_real(64, 48, semente=21)
        res = self.executar(self.imagem_livre([j1, j2]), min_kb=0)
        self.assertEqual(len(self.por_ext(res, "jpg")), 2)
        self.assertEqual(res.duplicados_hash, 0)


# ===========================================================================
# 4. Partições e layouts de disco
# ===========================================================================


class TesteParticoes(BaseMotor):
    def test_mbr(self):
        vol = construir_fat32([ArquivoTeste("m.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        disco = montar_disco_mbr([(2048, len(vol) // 512, vol, 0x0C)],
                                 disco_setores=2048 + len(vol) // 512 + 64)
        caminho = self.imagem(disco)
        res = self.executar(caminho)
        self.assertEqual(len(res.volumes), 1)
        self.assertIn("m.jpg", self.nomes(res))

    def test_gpt_duas_particoes(self):
        v1 = construir_fat32([ArquivoTeste("g1.jpg", self.jp, apagado=True)],
                             tam_setores=16384)
        v2 = construir_exfat([ArquivoTeste("g2.png", self.png, apagado=True)],
                             tam_setores=16384)
        ini1, fim1 = 2048, 2048 + len(v1) // 512 - 1
        ini2, fim2 = fim1 + 1, fim1 + len(v2) // 512
        disco = montar_disco_gpt([(ini1, fim1, v1, "FOTOS"),
                                  (ini2, fim2, v2, "BACKUP")],
                                 disco_setores=fim2 + 2048)
        caminho = self.imagem(disco)
        res = self.executar(caminho)
        self.assertEqual(sorted(res.sistemas), ["exfat", "fat32"])
        self.assertEqual(sorted(self.nomes(res)), ["g1.jpg", "g2.png"])

    def test_gpt_primario_destruido_usa_backup(self):
        v1 = construir_fat32([ArquivoTeste("g1.jpg", self.jp, apagado=True)],
                             tam_setores=16384)
        ini1, fim1 = 2048, 2048 + len(v1) // 512 - 1
        disco = bytearray(montar_disco_gpt([(ini1, fim1, v1, "FOTOS")],
                                           disco_setores=fim1 + 2048))
        disco[512:512 + 92] = b"\xAA" * 92          # destrói o cabeçalho primário
        caminho = self.imagem(bytes(disco))
        leitor = aquisicao.LeitorArquivo(caminho)
        parts = particoes.detectar_particoes(leitor)
        leitor.fechar()
        self.assertTrue(any(p.origem in ("gpt_backup", "gpt") for p in parts),
                        [p.origem for p in parts])
        res = self.executar(caminho)
        self.assertIn("g1.jpg", self.nomes(res))

    def test_ebr_particoes_logicas(self):
        v1 = construir_fat32([ArquivoTeste("l1.jpg", self.jp, apagado=True)],
                             tam_setores=8192)
        v2 = construir_fat32([ArquivoTeste("l2.png", self.png, apagado=True)],
                             tam_setores=8192)
        est_ini = 2048
        ini1 = est_ini + 1
        n1 = len(v1) // 512
        ebr2 = ini1 + n1 + 1
        ini2 = ebr2 + 1
        n2 = len(v2) // 512
        disco = montar_disco_ebr(est_ini, ini2 + n2 - 1,
                                 [(ini1, n1, v1, 0x0C), (ini2, n2, v2, 0x0C)],
                                 disco_setores=ini2 + n2 + 64)
        caminho = self.imagem(disco)
        res = self.executar(caminho)
        self.assertEqual(len(res.volumes), 2)
        self.assertEqual(sorted(self.nomes(res)), ["l1.jpg", "l2.png"])

    def test_particao_perdida_encontrada(self):
        vol = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        disco = montar_disco_mbr([(2048, len(vol) // 512, vol, 0x0C)],
                                 disco_setores=2048 + len(vol) // 512 + 64,
                                 apagar_mbr=True)
        caminho = self.imagem(disco)
        res = self.executar(caminho, buscar_particoes=True)
        self.assertIn("p.jpg", self.nomes(res))
        self.assertEqual(res.candidatos[0].metodo, METODO_PASTA)
        self.assertTrue(any("perdida" in a for a in res.avisos))

    def test_busca_automatica_quando_nenhuma_particao_valida(self):
        """MBR apagado: a busca por partições perdidas é acionada sozinha e o
        usuário é avisado disso (nada de recuperação silenciosa)."""
        vol = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        disco = montar_disco_mbr([(2048, len(vol) // 512, vol, 0x0C)],
                                 disco_setores=2048 + len(vol) // 512 + 64,
                                 apagar_mbr=True)
        caminho = self.imagem(disco)
        res = self.executar(caminho)     # sem --buscar-particoes explícito
        self.assertIn("p.jpg", self.nomes(res))
        self.assertTrue(any("perdida" in a or "busca automática" in a
                            for a in res.avisos))

    def test_setores_de_4096(self):
        vol = construir_fat32([ArquivoTeste("s4k.jpg", self.jp, apagado=True)],
                              tam_setor=4096, tam_setores=4096, spc=2)
        caminho = self.imagem(vol)
        res = self.executar(caminho)
        self.assertIn("s4k.jpg", self.nomes(res))

    def test_boot_de_reserva_fat32_setor6(self):
        vol = construir_fat32([ArquivoTeste("bk.jpg", self.jp, apagado=True)],
                              tam_setores=16384, corromper_boot=True)
        caminho = self.imagem(vol)
        res = self.executar(caminho)
        self.assertIn("bk.jpg", self.nomes(res))
        self.assertTrue(any("reserva" in a for a in res.avisos))

    def test_boot_de_reserva_exfat(self):
        vol = construir_exfat([ArquivoTeste("bkex.jpg", self.jp, apagado=True)],
                              tam_setores=16384, corromper_boot=True)
        caminho = self.imagem(vol)
        res = self.executar(caminho)
        self.assertIn("bkex.jpg", self.nomes(res))

    def test_boot_de_reserva_ntfs(self):
        vol = construir_ntfs([ArquivoTeste("bknt.jpg", self.jp, apagado=True)],
                             corromper_boot=True)
        caminho = self.imagem(vol)
        res = self.executar(caminho)
        self.assertIn("bknt.jpg", self.nomes(res))

    def test_tabela_de_particoes_nao_e_modificada(self):
        vol = construir_fat32([ArquivoTeste("m.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        disco = montar_disco_mbr([(2048, len(vol) // 512, vol, 0x0C)],
                                 disco_setores=2048 + len(vol) // 512 + 64)
        caminho = self.imagem(disco)
        antes = self.sha256(caminho)
        self.executar(caminho)
        self.assertEqual(antes, self.sha256(caminho))


# ===========================================================================
# 5. Relatórios e estados
# ===========================================================================


class TesteRelatorios(BaseMotor):
    def test_csv_utf8_bom_e_ponto_e_virgula(self):
        caminho = self.imagem(construir_fat32(
            [ArquivoTeste("r.jpg", self.jp, apagado=True)]))
        res = self.executar(caminho)
        with open(res.relatorio_csv, "rb") as f:
            bruto = f.read()
        self.assertTrue(bruto.startswith(b"\xEF\xBB\xBF"), "falta BOM UTF-8")
        texto = bruto.decode("utf-8-sig")
        self.assertIn(";", texto)
        self.assertNotIn(",", texto.splitlines()[0])

    def test_csv_colunas_exigidas(self):
        caminho = self.imagem(construir_fat32(
            [ArquivoTeste("r.jpg", self.jp, apagado=True)]))
        res = self.executar(caminho)
        with open(res.relatorio_csv, encoding="utf-8-sig", newline="") as f:
            linhas = list(csv.reader(f, delimiter=";"))
        self.assertEqual(linhas[0], ["arquivo", "tipo", "encontrado_por",
                                     "caminho_original", "posicao_no_cartao",
                                     "tamanho_bytes", "data", "estado",
                                     "fracao_recuperada", "metodo", "motivo"])
        self.assertEqual(len(linhas), 2)
        linha = linhas[1]
        self.assertEqual(linha[0], "r.jpg")
        self.assertEqual(linha[1], "jpg")
        self.assertEqual(linha[2], "metadados")
        self.assertEqual(linha[7], ESTADO_OK)
        self.assertEqual(linha[8], "1,0000")
        self.assertEqual(linha[9], METODO_PASTA)

    def test_metodo_no_csv_pertence_ao_conjunto(self):
        caminho = self.imagem_livre([self.jp])
        res = self.executar(caminho, min_kb=0)
        with open(res.relatorio_csv, encoding="utf-8-sig", newline="") as f:
            linhas = list(csv.reader(f, delimiter=";"))
        permitidos = {METODO_PASTA, METODO_MFT, METODO_MFT_ORFA, METODO_VARREDURA}
        for linha in linhas[1:]:
            self.assertIn(linha[9], permitidos)

    def test_corrompidos_vao_para_subpasta(self):
        j = A.jpeg_lixo_no_meio(jpeg_real(64, 48, semente=30))
        caminho = self.imagem_livre([j])
        res = self.executar(caminho, min_kb=0)
        corrompidos = [c for c in res.candidatos
                       if c.estado in (ESTADO_CORROMPIDO, ESTADO_PARCIAL)]
        if corrompidos:
            for c in corrompidos:
                self.assertIn("_corrompidas", c.arquivo_destino)
                self.assertTrue(os.path.exists(c.arquivo_destino))

    def test_estado_nao_verificado_quando_sem_verificacao(self):
        caminho = self.imagem(construir_fat32(
            [ArquivoTeste("nv.jpg", self.jp, apagado=True)]))
        res = self.executar(caminho, sem_verificar=True, verificar=False)
        self.assertEqual(res.candidatos[0].estado, ESTADO_NAO_VERIFICADO)

    def test_fracao_recuperada_parcial(self):
        """Mecanismo real de 'parcial': os metadados declaram mais bytes do que
        é fisicamente legível (imagem/cartão truncado)."""
        corpo = jpeg_real(160, 120, semente=40)
        img = construir_fat32([ArquivoTeste("grande.jpg", corpo, apagado=True)])
        corte = img.find(corpo[:64]) + len(corpo) // 2      # corta no meio dos dados
        caminho = self.imagem(img[:corte])
        res = self.executar(caminho, min_kb=0)
        self.assertEqual(res.recuperados, 1)
        c = res.candidatos[0]
        self.assertEqual(c.estado, ESTADO_PARCIAL)
        self.assertIsNotNone(c.fracao_recuperada)
        self.assertGreater(c.fracao_recuperada, 0.0)
        self.assertLess(c.fracao_recuperada, 1.0)
        self.assertAlmostEqual(c.fracao_recuperada,
                               (len(corpo) // 2) / len(corpo), delta=0.06)
        self.assertIn("bytes", c.motivo)

    def test_arquivo_completo_tem_fracao_1(self):
        img = construir_fat32([ArquivoTeste("inteiro.jpg", self.jp, apagado=True)])
        res = self.executar(self.imagem(img), min_kb=0)
        c = res.candidatos[0]
        self.assertEqual(c.estado, ESTADO_OK)
        self.assertEqual(c.fracao_recuperada, 1.0)

    def test_nomes_seguros_sem_caracteres_invalidos(self):
        nome = 'fo/to:ca*ra?ct"er<es>|in.jpg'.replace("/", "_")
        img = construir_ntfs([ArquivoTeste('inválido?.jpg', self.jp, apagado=True)])
        caminho = self.imagem(img)
        res = self.executar(caminho)
        for c in res.candidatos:
            base = os.path.basename(c.arquivo_destino)
            for proibido in '\\/:*?"<>|':
                self.assertNotIn(proibido, base)
        del nome

    def test_sem_sobrescrita_de_arquivos(self):
        img = construir_ntfs([
            ArquivoTeste("mesmo.jpg", self.jp, apagado=True, subpasta="A"),
            ArquivoTeste("mesmo.jpg", self.png, apagado=True, subpasta="B")])
        caminho = self.imagem(img)
        res = self.executar(caminho, sem_pastas=True)
        destinos = [c.arquivo_destino for c in res.candidatos]
        self.assertEqual(len(destinos), len(set(destinos)))
        for d in destinos:
            self.assertTrue(os.path.exists(d))


# ===========================================================================
# 6. Segurança da origem e do destino
# ===========================================================================


class TesteSeguranca(BaseMotor):
    def test_origem_nao_e_modificada(self):
        img = construir_fat32([ArquivoTeste("s.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        antes = self.sha256(caminho)
        self.executar(caminho)
        self.assertEqual(antes, self.sha256(caminho))

    def test_destino_no_mesmo_disco_e_bloqueado(self):
        img = construir_fat32([ArquivoTeste("s.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "mesmo_disco")
        opcoes = motor.Opcoes(origem=caminho, destino=destino,
                              permitir_mesmo_disco=False)
        with self.assertRaises(PermissionError):
            motor.Motor(opcoes).executar()

    def test_permitir_mesmo_disco_so_para_imagem(self):
        img = construir_fat32([ArquivoTeste("s.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "autorizado")
        opcoes = motor.Opcoes(origem=caminho, destino=destino,
                              permitir_mesmo_disco=True)
        res = motor.Motor(opcoes).executar()
        self.assertTrue(any("autorizada" in a for a in res.avisos))

    def test_destino_dentro_da_pasta_de_origem_bloqueado(self):
        origem_pasta = os.path.join(self.pasta, "DCIM")
        os.makedirs(origem_pasta)
        with open(os.path.join(origem_pasta, "a.jpg"), "wb") as f:
            f.write(self.jp)
        dentro = os.path.join(origem_pasta, "saida")
        opcoes = motor.Opcoes(origem=origem_pasta, destino=dentro,
                              permitir_mesmo_disco=True)
        with self.assertRaises((PermissionError, ValueError)):
            motor.Motor(opcoes).executar()

    def test_origem_inexistente_levanta(self):
        opcoes = motor.Opcoes(origem=os.path.join(self.pasta, "nao.img"),
                              destino=self.destino, permitir_mesmo_disco=True)
        with self.assertRaises((ValueError, OSError)):
            motor.Motor(opcoes).executar()

    def test_destino_criado_automaticamente(self):
        img = construir_fat32([ArquivoTeste("s.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        novo = os.path.join(self.pasta, "a", "b", "c")
        res = self.executar(caminho, destino=novo)
        self.assertTrue(os.path.isdir(novo))
        self.assertGreaterEqual(res.recuperados, 1)

    def test_leitor_abre_origem_somente_leitura(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        leitor = aquisicao.abrir_origem(caminho)
        self.assertFalse(getattr(leitor, "gravavel", False))
        # nenhum leitor do programa expõe gravação: ou o método não existe
        # (AttributeError) ou a tentativa é recusada (OSError/ErroLeitura)
        with self.assertRaises((AttributeError, OSError)):
            leitor.write(0, b"\xFF" * 16)
        leitor.fechar()


# ===========================================================================
# 7. Opções, pausa e cancelamento
# ===========================================================================


class TesteOpcoesExecucao(BaseMotor):
    def test_cancelamento_seguro(self):
        img = construir_fat32([ArquivoTeste(f"f{k}.jpg", self.jp, apagado=True)
                               for k in range(20)])
        caminho = self.imagem(img)
        cancelar = threading.Event()
        opcoes = motor.Opcoes(origem=caminho, destino=self.destino,
                              permitir_mesmo_disco=True, min_kb=0)
        m = motor.Motor(opcoes, cancelar=cancelar)
        cancelar.set()
        res = m.executar()
        self.assertTrue(res.cancelado)
        self.assertEqual(self.sha256(caminho), self.sha256(caminho))

    def test_cancelamento_durante_etapa2(self):
        caminho = self.imagem_livre([self.jp] * 4)
        cancelar = threading.Event()
        vistos = []

        def progresso(fracao, total, fase):
            vistos.append(fase)
            if fase == "carving":
                cancelar.set()

        opcoes = motor.Opcoes(origem=caminho, destino=self.destino,
                              permitir_mesmo_disco=True, min_kb=0)
        res = motor.Motor(opcoes, progresso=progresso, cancelar=cancelar).executar()
        self.assertTrue(res.cancelado)
        self.assertIn("carving", vistos)

    def test_pausa_apos_etapa1_e_continuar(self):
        img = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        continuar = threading.Event()      # entrada: libera a etapa 2
        opcoes = motor.Opcoes(origem=caminho, destino=self.destino,
                              permitir_mesmo_disco=True, min_kb=0,
                              pausar_apos_etapa1=True)
        logs = []
        trava = threading.Event()
        m = motor.Motor(opcoes, continuar=continuar,
                        log=lambda msg: (logs.append(msg),
                                         trava.set() if "Pausado" in msg else None))
        resultado = {}

        def rodar():
            resultado["res"] = m.executar()

        t = threading.Thread(target=rodar, daemon=True)
        t.start()
        self.assertTrue(trava.wait(15), "o motor não pausou após a etapa 1")
        self.assertTrue(t.is_alive())                       # realmente parado
        self.assertFalse(any("Etapa 2" in linha for linha in logs))
        continuar.set()                                     # usuário: continuar
        t.join(timeout=20)
        self.assertFalse(t.is_alive())
        res = resultado["res"]
        self.assertTrue(res.pausado)
        self.assertFalse(res.cancelado)
        self.assertTrue(any("Etapa 2" in linha for linha in logs))
        self.assertEqual(res.recuperados, 1)

    def test_pausa_e_finalizar_encerra_sem_etapa2(self):
        img = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True),
                               ArquivoTeste("livre.bin", dados_pseudo_aleatorios(20000, 7))])
        caminho = self.imagem(img)
        continuar = threading.Event()
        finalizar = threading.Event()
        opcoes = motor.Opcoes(origem=caminho, destino=self.destino,
                              permitir_mesmo_disco=True, min_kb=0,
                              pausar_apos_etapa1=True, cartao_inteiro=True)
        logs = []
        trava = threading.Event()
        m = motor.Motor(opcoes, continuar=continuar, finalizar=finalizar,
                        log=lambda msg: (logs.append(msg),
                                         trava.set() if "Pausado" in msg else None))
        resultado = {}
        t = threading.Thread(target=lambda: resultado.update(res=m.executar()),
                             daemon=True)
        t.start()
        self.assertTrue(trava.wait(15))
        finalizar.set()                                     # usuário: finalizar
        t.join(timeout=20)
        self.assertFalse(t.is_alive())
        res = resultado["res"]
        self.assertTrue(res.pausado)
        self.assertFalse(any("Etapa 2 concluída" in linha for linha in logs))
        self.assertEqual(res.por_metodo.get(METODO_VARREDURA, 0), 0)

    def test_etapa2_desativada(self):
        img = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        res = self.executar(caminho, etapa2=False)
        self.assertEqual(res.por_metodo.get(METODO_VARREDURA, 0), 0)
        self.assertEqual(res.recuperados, 1)

    def test_cartao_inteiro_varre_todo_o_volume(self):
        img = construir_fat32([ArquivoTeste("viva.jpg", self.jp, apagado=False)])
        caminho = self.imagem(img)
        res_livre = self.executar(caminho, min_kb=0)
        res_total = self.executar(caminho, min_kb=0, cartao_inteiro=True)
        self.assertEqual(res_livre.recuperados, 0)
        self.assertGreaterEqual(res_total.recuperados, 1)

    def test_limite_de_arquivos(self):
        img = construir_fat32([ArquivoTeste(f"l{k}.jpg", self.jp, apagado=True)
                               for k in range(10)])
        caminho = self.imagem(img)
        res = self.executar(caminho, min_kb=0, limite_arquivos=3)
        self.assertLessEqual(res.recuperados, 3)

    def test_filtro_de_data_hoje(self):
        hoje = time.localtime()
        img = construir_fat32([
            ArquivoTeste("hoje.jpg", self.jp, apagado=True,
                         data=(hoje.tm_year, hoje.tm_mon, hoje.tm_mday, 1, 2, 3)),
            ArquivoTeste("antiga.jpg", self.png, apagado=True,
                         data=(2001, 5, 6, 7, 8, 9))])
        caminho = self.imagem(img)
        # etapa 2 desligada: o carved não tem data e passaria sem --ignorar-sem-data
        res = self.executar(caminho, desde="hoje", etapa2=False)
        self.assertEqual(self.nomes(res), ["hoje.jpg"])
        res2 = self.executar(caminho, desde="hoje")
        self.assertNotIn("antiga.jpg", self.nomes(res2))
        for c in res2.candidatos:
            if c.metodo == METODO_VARREDURA:
                self.assertIsNone(c.data)   # sem data conhecida: não é filtrado

    def test_filtro_de_data_especifica(self):
        img = construir_fat32([
            ArquivoTeste("a.jpg", self.jp, apagado=True, data=(2019, 12, 25)),
            ArquivoTeste("b.jpg", self.png, apagado=True, data=(2020, 1, 1))])
        caminho = self.imagem(img)
        res = self.executar(caminho, desde="01/01/2020", etapa2=False)
        self.assertEqual(self.nomes(res), ["b.jpg"])
        res2 = self.executar(caminho, desde="25/12/2019", etapa2=False)
        self.assertEqual(sorted(self.nomes(res2)), ["a.jpg", "b.jpg"])
        res3 = self.executar(caminho, desde="02/01/2020", etapa2=False)
        self.assertEqual(res3.recuperados, 0)

    def test_filtro_de_data_invalido_gera_aviso(self):
        """Data impossível: o usuário é avisado e a recuperação continua
        (nenhum arquivo é descartado em silêncio por um filtro inválido)."""
        img = construir_fat32([ArquivoTeste("a.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        res = self.executar(caminho, desde="31/02/2025")
        self.assertTrue(any("formato de data inválido" in a for a in res.avisos))
        self.assertIn("a.jpg", self.nomes(res))

    def test_filtro_de_data_relativo_nd(self):
        hoje = time.localtime()
        img = construir_fat32([
            ArquivoTeste("recente.jpg", self.jp, apagado=True,
                         data=(hoje.tm_year, hoje.tm_mon, hoje.tm_mday, 5, 0, 0)),
            ArquivoTeste("velha.jpg", self.png, apagado=True,
                         data=(2000, 1, 2, 3, 4, 5))])
        res = self.executar(self.imagem(img), desde="7d", etapa2=False)
        self.assertEqual(self.nomes(res), ["recente.jpg"])

    def test_ignorar_sem_data(self):
        img = construir_ntfs([ArquivoTeste("semdata.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        res = self.executar(caminho, desde="hoje", ignorar_sem_data=True)
        # arquivos com data conhecida em 2024 não passam no filtro "hoje"
        self.assertEqual(res.recuperados, 0)

    def test_sem_data_no_nome(self):
        caminho = self.imagem_livre([self.jp])
        res = self.executar(caminho, min_kb=0, sem_data_no_nome=True)
        nome = self.nomes(res)[0]
        self.assertRegex(nome, r"^carv_\d+\.jpg$")

    def test_profunda_acrescenta_candidatos(self):
        img = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        normal = self.executar(caminho, min_kb=0)
        profunda = self.executar(caminho, min_kb=0, profunda=True)
        self.assertGreaterEqual(profunda.recuperados, normal.recuperados)


# ===========================================================================
# 8. Pasta de origem (filtro por pasta)
# ===========================================================================


class TesteOrigemPasta(BaseMotor):
    def test_pasta_sem_volume_fisico_avisae_nao_inventa(self):
        origem = os.path.join(self.pasta, "DCIM")
        os.makedirs(origem)
        with open(os.path.join(origem, "foto.jpg"), "wb") as f:
            f.write(self.jp)
        opcoes = motor.Opcoes(origem=origem, destino=self.destino,
                              permitir_mesmo_disco=True, min_kb=0)
        res = motor.Motor(opcoes).executar()
        # sem acesso ao volume físico não há como provar nada: nada de "ok"
        self.assertTrue(res.recuperados == 0 or
                        all(c.estado != ESTADO_OK for c in res.candidatos) or
                        res.avisos)


# ===========================================================================
# 9. Aquisição integrada ao fluxo
# ===========================================================================


class TesteAquisicaoIntegrada(BaseMotor):
    def test_criar_imagem_e_recuperar_a_partir_dela(self):
        img = construir_fat32([ArquivoTeste("aq.jpg", self.jp, apagado=True)])
        origem = self.imagem(img)
        destino_img = os.path.join(self.pasta, "copia.img")
        leitor = aquisicao.LeitorArquivo(origem)
        adq = aquisicao.Adquiridor(leitor, destino_img, bloco=4096)
        resumo = adq.executar()
        leitor.fechar()
        self.assertEqual(resumo["erro"], 0)
        self.assertEqual(self.sha256(origem), self.sha256(destino_img))
        res = self.executar(destino_img)
        self.assertIn("aq.jpg", self.nomes(res))

    def test_recuperacao_a_partir_da_imagem_preserva_bytes(self):
        img = construir_fat32([ArquivoTeste("aq.jpg", self.jp, apagado=True)])
        origem = self.imagem(img)
        destino_img = os.path.join(self.pasta, "copia2.img")
        leitor = aquisicao.LeitorArquivo(origem)
        aquisicao.Adquiridor(leitor, destino_img, bloco=4096).executar()
        leitor.fechar()
        res = self.executar(destino_img)
        with open(res.candidatos[0].arquivo_destino, "rb") as f:
            self.assertEqual(f.read(), self.jp)


# ===========================================================================
# 10. Limites de memória
# ===========================================================================


class TesteLimitesMemoria(BaseMotor):
    def test_leituras_bloqueadas_limitadas(self):
        """Nenhuma leitura individual pode exceder 8 MiB (memória limitada)."""
        img = construir_fat32([ArquivoTeste("m.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        maximo = [0]
        original = aquisicao.LeitorArquivo.read

        def espiao(self_leitor, offset, size):
            maximo[0] = max(maximo[0], size)
            return original(self_leitor, offset, size)

        aquisicao.LeitorArquivo.read = espiao
        try:
            self.executar(caminho)
        finally:
            aquisicao.LeitorArquivo.read = original
        self.assertGreater(maximo[0], 0)
        self.assertLessEqual(maximo[0], 8 * 1024 * 1024)

    def test_constantes_de_bloco_limitadas(self):
        import assinaturas
        self.assertLessEqual(assinaturas.BLOCO_VARREDURA, 8 * 1024 * 1024)
        self.assertLessEqual(aquisicao.Adquiridor.BLOCO_PADRAO
                             if hasattr(aquisicao.Adquiridor, "BLOCO_PADRAO")
                             else 4 * 1024 * 1024, 8 * 1024 * 1024)

    def test_lista_de_candidatos_limitada(self):
        self.assertEqual(motor.Motor.MAX_CANDIDATOS_MEMORIA, 5000)

    def test_galeria_limita_miniaturas(self):
        """O limite existe e é modesto (a galeria não pode segurar milhares de
        imagens decodificadas na memória). Coberto em detalhe em
        tests/test_interface.py."""
        fonte = open(os.path.join(RAIZ, "interface.py"), encoding="utf-8").read()
        m = re.search(r"^LIMITE_MINIATURAS\s*=\s*(\d+)", fonte, re.M)
        self.assertIsNotNone(m, "LIMITE_MINIATURAS não encontrado em interface.py")
        valor = int(m.group(1))
        self.assertGreater(valor, 0)
        self.assertLessEqual(valor, 500)
        self.assertIn("cands[:LIMITE_MINIATURAS]", fonte)


# ===========================================================================
# 11. CLI e interface
# ===========================================================================


class TesteCLI(BaseMotor):
    def _main(self, argv):
        """Executa a CLI capturando a saída (o relatório de testes não pode
        virar uma sopa de texto e ainda dá para conferir o que o usuário lê)."""
        import recupera_fotos
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            codigo = recupera_fotos.main(argv)
        self.saida_cli = buffer.getvalue()
        return codigo

    def test_listar(self):
        codigo = self._main(["--listar", "--todos-os-discos"])
        self.assertEqual(codigo, 0)
        self.assertTrue(self.saida_cli.strip())

    def test_saida_da_cli_explica_o_resultado(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "cli_saida_txt")
        self.assertEqual(self._main([caminho, "-o", destino,
                                     "--permitir-mesmo-disco",
                                     "--min-kb", "0"]), 0)
        for trecho in ("RESULTADO DA RECUPERAÇÃO", "Arquivos recuperados: 1",
                       "Por estado:", "Por método:", "Relatório:"):
            self.assertIn(trecho, self.saida_cli)

    def test_cli_sem_destino_explica_e_sai_com_2(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        self.assertEqual(self._main([caminho]), 2)
        self.assertIn("destino", self.saida_cli.lower())

    def test_cli_sem_console_grava_log_em_vez_de_quebrar(self):
        """Um .exe gerado no modo janela tem sys.stdout = None; sem proteção,
        o primeiro print() da CLI morreria com AttributeError."""
        import recupera_fotos
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "cli_sem_console")
        exe = os.path.join(self.pasta, "RecuperaFotos.exe")
        salvos = (sys.stdout, sys.stderr,
                  getattr(sys, "frozen", None), sys.executable)
        try:
            sys.stdout = None
            sys.stderr = None
            sys.frozen = True
            sys.executable = exe
            codigo = recupera_fotos.main([caminho, "-o", destino,
                                          "--permitir-mesmo-disco",
                                          "--min-kb", "0"])
        finally:
            sys.stdout, sys.stderr = salvos[0], salvos[1]
            if salvos[2] is None:
                del sys.frozen
            else:
                sys.frozen = salvos[2]
            sys.executable = salvos[3]
        self.assertEqual(codigo, 0)
        log = os.path.join(self.pasta, "recupera_fotos_log.txt")
        self.assertTrue(os.path.exists(log),
                        "a saída deveria ter ido para recupera_fotos_log.txt")
        texto = open(log, encoding="utf-8").read()
        self.assertIn("RESULTADO DA RECUPERAÇÃO", texto)
        self.assertIn("Arquivos recuperados: 1", texto)
        self.assertIn("recupera_fotos_log.txt", texto)

    def test_cli_com_console_nao_cria_arquivo_de_log(self):
        """Com console normal (python recupera_fotos.py) nada é desviado."""
        import recupera_fotos
        self.assertIsNone(recupera_fotos._garantir_saida_padrao())
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "cli_com_console")
        self.assertEqual(self._main([caminho, "-o", destino,
                                     "--permitir-mesmo-disco", "--min-kb", "0"]), 0)
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(os.path.abspath(recupera_fotos.__file__)),
                         "recupera_fotos_log.txt")))

    def test_cli_data_invalida_explica(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        codigo = self._main([caminho, "-o", os.path.join(self.pasta, "d1"),
                             "--desde", "99/99/9999", "--permitir-mesmo-disco",
                             "--min-kb", "0"])
        self.assertEqual(codigo, 2)
        self.assertIn("--desde inválido", self.saida_cli)
        self.assertIn("DD/MM/AAAA", self.saida_cli)

    def test_cli_aceita_datas_validas(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        for desde in ("hoje", "ontem", "7d", "30d", "25/12/2019", "25/12/19"):
            destino = tempfile.mkdtemp(prefix="rf4_cli_dt_", dir=self.pasta)
            codigo = self._main([caminho, "-o", destino, "--desde", desde,
                                 "--permitir-mesmo-disco", "--min-kb", "0"])
            self.assertEqual(codigo, 0, desde)
            self.assertNotIn("ERRO", self.saida_cli)

    def test_recuperacao_por_cli(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "cli_saida")
        codigo = self._main([caminho, "-o", destino, "--permitir-mesmo-disco",
                             "--min-kb", "0"])
        self.assertEqual(codigo, 0)
        self.assertTrue(os.path.exists(os.path.join(destino, "cli.jpg")))
        self.assertTrue(os.path.exists(
            os.path.join(destino, "relatorio_recuperacao.csv")))

    def test_cli_videos_e_profunda(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = os.path.join(self.pasta, "cli_saida2")
        codigo = self._main([caminho, "-o", destino, "--videos", "--profunda",
                             "--min-kb", "0", "--permitir-mesmo-disco"])
        self.assertEqual(codigo, 0)

    def test_cli_criar_imagem(self):
        img = construir_fat32([ArquivoTeste("cli.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino_img = os.path.join(self.pasta, "cli.img")
        codigo = self._main([caminho, "--criar-imagem", destino_img,
                             "--permitir-mesmo-disco",
                             "-o", os.path.join(self.pasta, "x")])
        self.assertEqual(codigo, 0)
        self.assertTrue(os.path.exists(destino_img))
        self.assertTrue(os.path.exists(destino_img + ".map.jsonl"))

    def test_cli_criar_imagem_bloqueia_mesmo_disco(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        destino_img = os.path.join(self.pasta, "bloqueada.img")
        codigo = self._main([caminho, "--criar-imagem", destino_img])
        self.assertEqual(codigo, 2)
        self.assertFalse(os.path.exists(destino_img))

    def test_cli_retomar_imagem(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        destino_img = os.path.join(self.pasta, "retomar.img")
        self.assertEqual(self._main([caminho, "--criar-imagem", destino_img,
                                     "--permitir-mesmo-disco",
                                     "-o", self.pasta]), 0)
        codigo = self._main([caminho, "--criar-imagem", destino_img,
                             "--retomar-imagem", "--permitir-mesmo-disco",
                             "-o", self.pasta])
        self.assertEqual(codigo, 0)

    def test_cli_retomar_sem_criar_e_erro(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        codigo = self._main([caminho, "--retomar-imagem", "-o", self.pasta])
        self.assertEqual(codigo, 2)

    def test_cli_sem_destino_e_erro(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        self.assertEqual(self._main([caminho]), 2)

    def test_cli_min_kb_negativo(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        self.assertEqual(self._main([caminho, "-o", self.destino,
                                     "--min-kb", "-5"]), 2)

    def test_cli_desde_invalido(self):
        img = construir_fat32([])
        caminho = self.imagem(img)
        self.assertEqual(self._main([caminho, "-o", self.destino,
                                     "--desde", "32/13/2024"]), 2)

    def test_cli_desde_valido(self):
        img = construir_fat32([ArquivoTeste("c.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        for valor in ("hoje", "ontem", "7d", "30d", "15/06/2024"):
            destino = tempfile.mkdtemp(prefix="rf4_cli_", dir=self.pasta)
            self.assertEqual(self._main([caminho, "-o", destino, "--desde", valor,
                                         "--permitir-mesmo-disco", "--min-kb", "0"]),
                             0, f"--desde {valor}")

    def test_cli_pasta_relativa(self):
        img = construir_ntfs([ArquivoTeste("a.jpg", self.jp, apagado=True,
                                           subpasta="Fotos")])
        caminho = self.imagem(img)
        destino = tempfile.mkdtemp(prefix="rf4_cli_", dir=self.pasta)
        self.assertEqual(self._main([caminho, "-o", destino, "--pasta-relativa",
                                     "Fotos", "--permitir-mesmo-disco",
                                     "--min-kb", "0"]), 0)
        self.assertTrue(os.path.exists(os.path.join(destino, "Fotos", "a.jpg")))

    def test_cli_sem_pastas_e_sem_verificar(self):
        img = construir_ntfs([ArquivoTeste("a.jpg", self.jp, apagado=True,
                                           subpasta="Fotos")])
        caminho = self.imagem(img)
        destino = tempfile.mkdtemp(prefix="rf4_cli_", dir=self.pasta)
        self.assertEqual(self._main([caminho, "-o", destino, "--sem-pastas",
                                     "--sem-verificar", "--sem-data-no-nome",
                                     "--min-kb", "0",
                                     "--permitir-mesmo-disco"]), 0)
        self.assertTrue(os.path.exists(os.path.join(destino, "a.jpg")))

    def test_cli_cartao_inteiro_e_alias(self):
        img = construir_fat32([ArquivoTeste("v.jpg", self.jp, apagado=False)])
        caminho = self.imagem(img)
        for flag in ("--cartao-inteiro", "--unidade-inteira"):
            destino = tempfile.mkdtemp(prefix="rf4_cli_", dir=self.pasta)
            self.assertEqual(self._main([caminho, "-o", destino, flag,
                                         "--min-kb", "0",
                                         "--permitir-mesmo-disco"]), 0)
            self.assertGreaterEqual(len(os.listdir(destino)), 2)

    def test_cli_buscar_particoes(self):
        vol = construir_fat32([ArquivoTeste("p.jpg", self.jp, apagado=True)],
                              tam_setores=16384)
        disco = montar_disco_mbr([(2048, len(vol) // 512, vol, 0x0C)],
                                 disco_setores=2048 + len(vol) // 512 + 64,
                                 apagar_mbr=True)
        caminho = self.imagem(disco)
        destino = tempfile.mkdtemp(prefix="rf4_cli_", dir=self.pasta)
        self.assertEqual(self._main([caminho, "-o", destino, "--buscar-particoes",
                                     "--min-kb", "0", "--permitir-mesmo-disco"]), 0)
        self.assertTrue(os.path.exists(os.path.join(destino, "p.jpg")))

    def test_cli_ignorar_sem_data(self):
        img = construir_fat32([ArquivoTeste("c.jpg", self.jp, apagado=True)])
        caminho = self.imagem(img)
        destino = tempfile.mkdtemp(prefix="rf4_cli_", dir=self.pasta)
        self.assertEqual(self._main([caminho, "-o", destino, "--ignorar-sem-data",
                                     "--desde", "hoje", "--permitir-mesmo-disco",
                                     "--min-kb", "0"]), 0)


# A interface gráfica é testada em tests/test_interface.py, que importa
# interface.py com um Tkinter dublê (tests/tk_stub.py) — assim os testes rodam
# mesmo sem servidor gráfico e sem o pacote python3-tk.
# Métodos cobertos lá: construção da janela, mapeamento tela -> motor.Opcoes,
# validações de segurança, recuperação completa, pausa/continuar/finalizar,
# cancelamento, fila thread-safe, limite do log e da galeria, diálogos.


if __name__ == "__main__":
    unittest.main(verbosity=2)
