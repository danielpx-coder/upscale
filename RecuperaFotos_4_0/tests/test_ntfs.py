# -*- coding: utf-8 -*-
"""Testes do parser NTFS: fixup, registros, data runs, LZNT1 e recuperação."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ntfs
from aquisicao import LeitorArquivo
from construtor import (ArquivoTeste, construir_ntfs, jpeg_real, png_real,
                        _registro_mft, _attr_file_name, _attr_data_nao_residente,
                        _attr_data_residente, lznt1_comprimir_unidade,
                        NTFS_REC_SIZE)


def _fs_de_imagem(img: bytes, pasta: str) -> tuple[ntfs.SistemaNTFS, str]:
    caminho = os.path.join(pasta, "vol_ntfs.img")
    with open(caminho, "wb") as f:
        f.write(img)
    leitor = LeitorArquivo(caminho)
    info = ntfs._info_ntfs.__wrapped__ if hasattr(ntfs._info_ntfs, "__wrapped__") else None
    del info
    from particoes import _info_ntfs
    boot = _info_ntfs(img[:512])
    return ntfs.SistemaNTFS(leitor, boot, 0, len(img)), caminho


class TesteFixup(unittest.TestCase):
    def _rec(self):
        return bytearray(_registro_mft(7, True, False,
                                       [_attr_file_name("a.txt", (1 << 48) | 5)]))

    def test_fixup_valido_aplicado(self):
        rec = self._rec()
        usa_ofs, usa_cnt = struct.unpack_from("<HH", rec, 4)
        dados = bytearray(rec)
        orig_finais = [struct.unpack_from("<H", rec, usa_ofs + 2 + k * 2)[0]
                       for k in range(usa_cnt - 1)]
        self.assertTrue(ntfs.aplicar_fixup(dados, usa_ofs, usa_cnt, 512))
        # após aplicar, os finais de setor recebem os valores da matriz USA
        for k, v in enumerate(orig_finais):
            self.assertEqual(struct.unpack_from("<H", dados, (k + 1) * 512 - 2)[0], v)
        # conteúdo inalterado fora dos pontos de fixup
        rec2 = bytearray(rec)
        for k in range(usa_cnt - 1):
            struct.pack_into("<H", rec2, (k + 1) * 512 - 2, orig_finais[k])
        self.assertEqual(bytes(dados), bytes(rec2))

    def test_fixup_corrompido_rejeitado(self):
        rec = self._rec()
        rec[510] ^= 0xFF  # corrompe o valor de fixup do 1º setor
        usa_ofs, usa_cnt = struct.unpack_from("<HH", rec, 4)
        self.assertFalse(ntfs.aplicar_fixup(bytearray(rec), usa_ofs, usa_cnt, 512))

    def test_usa_fora_do_registro(self):
        rec = self._rec()
        self.assertFalse(ntfs.aplicar_fixup(rec, len(rec) + 10, 3, 512))

    def test_parsear_registro_valido(self):
        img = construir_ntfs([ArquivoTeste("x.jpg", b"\xff\xd8\xff", apagado=True)])
        rec = img[2 * 4096: 2 * 4096 + 1024]  # registro 0 ($MFT)
        reg = ntfs.parsear_registro(0, rec, 1024, 512)
        self.assertIsNotNone(reg)
        self.assertTrue(reg.valido)
        self.assertTrue(reg.em_uso)

    def test_parsear_registro_sem_assinatura(self):
        reg = ntfs.parsear_registro(0, b"\x00" * 1024, 1024, 512)
        self.assertIsNone(reg)

    def test_parsear_registro_assinatura_lixo(self):
        lixo = bytearray(os.urandom(1024))
        lixo[0:4] = b"FILE"
        self.assertIsNone(ntfs.parsear_registro(0, bytes(lixo), 1024, 512))

    def test_parsear_registro_usado_maior_que_alocado(self):
        rec = bytearray(_registro_mft(9, True, False,
                                      [_attr_file_name("b.txt", (1 << 48) | 5)]))
        usa_ofs, usa_cnt = struct.unpack_from("<HH", rec, 4)
        # desfaz o fixup para editar e refaz
        struct.pack_into("<I", rec, 24, 2000)   # usado > alocado
        struct.pack_into("<H", rec, 4, usa_ofs)
        self.assertIsNone(ntfs.parsear_registro(9, bytes(rec), 1024, 512))


class TesteRunList(unittest.TestCase):
    def test_run_unico(self):
        dados = bytes([0x11, 10, 5, 0])
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [(5, 10, False)])

    def test_dois_runs_delta_relativo(self):
        # run1: lcn=100 n=4; run2: delta=+50 => lcn=150 n=6
        dados = bytes([0x11, 4]) + (100).to_bytes(1, "little") + \
            bytes([0x11, 6]) + (50).to_bytes(1, "little") + bytes([0])
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [(100, 4, False), (150, 6, False)])

    def test_run_negativo(self):
        dados = (bytes([0x22]) + (4).to_bytes(2, "little")
                 + (200).to_bytes(2, "little")
                 + bytes([0x11]) + (2).to_bytes(1, "little")
                 + (-30).to_bytes(1, "little", signed=True) + bytes([0]))
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs[1][0], 170)

    def test_run_sparse(self):
        # run sparse: nibble baixo = 1 byte de length, nibble alto = 0 (sem offset)
        dados = bytes([0x11, 3]) + (7).to_bytes(1, "little") + bytes([0x01, 5, 0])
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [(7, 3, False), (-1, 5, True)])
        self.assertEqual(rl.total_clusters(), 8)

    def test_fragmentos_mescla_contiguos(self):
        rl = ntfs.RunList([(10, 2, False), (12, 3, False), (40, 1, False)])
        frags = rl.fragmentos(0, 4096)
        self.assertEqual(len(frags), 2)
        self.assertEqual((frags[0].offset, frags[0].tamanho), (10 * 4096, 5 * 4096))
        self.assertEqual(frags[1].offset, 40 * 4096)

    def test_fragmentos_limita_tamanho(self):
        rl = ntfs.RunList([(0, 10, False)])
        frags = rl.fragmentos(0, 512, tamanho=1500)
        self.assertEqual(sum(f.tamanho for f in frags), 1500)

    def test_fragmentos_ignora_sparse(self):
        rl = ntfs.RunList([(5, 2, False), (-1, 3, True), (20, 1, False)])
        frags = rl.fragmentos(0, 512)
        self.assertEqual([f.offset for f in frags], [5 * 512, 20 * 512])

    def test_run_truncado_interrompe(self):
        dados = bytes([0x34])  # declara len=4/off=3 bytes que não existem
        rl = ntfs.RunList.parsear(dados)
        self.assertEqual(rl.runs, [])

    def test_max_runs_limitado(self):
        dados = bytes([0x11, 1, 0]) * 2000 + bytes([0])
        rl = ntfs.RunList.parsear(dados, max_runs=10)
        self.assertLessEqual(len(rl.runs), 10)


class TesteLZNT1(unittest.TestCase):
    def test_descomprime_unidade_comprimida(self):
        original = b"PADRAO16PADRAO16" * 256  # 4096, período 16
        comp = lznt1_comprimir_unidade(original)
        self.assertLess(len(comp), 4096)
        des = ntfs.descomprimir_lznt1(comp, 4096)
        self.assertEqual(des, original)

    def test_descomprime_literais(self):
        original = bytes(range(256)) * 16
        comp = lznt1_comprimir_unidade(original)
        des = ntfs.descomprimir_lznt1(comp, 4096)
        self.assertEqual(des, original)

    def test_chunk_nao_comprimido(self):
        corpo = b"A" * 4096
        cab = struct.pack("<H", (4096 - 1) & 0x0FFF)  # flag 0, campo = len-1
        des = ntfs.descomprimir_lznt1(cab + corpo, 4096)
        self.assertEqual(des, corpo)

    def test_corrompido_offset_invalido(self):
        # par length/offset apontando para trás do início da saída
        corpo = bytes([0x01, 0xFF, 0xFF])
        cab = struct.pack("<H", 0x8000 | ((len(corpo) - 1) & 0x0FFF))
        self.assertIsNone(ntfs.descomprimir_lznt1(cab + corpo, 4096))

    def test_entrada_aleatoria_nao_trava(self):
        import random
        rnd = random.Random(1234)
        for _ in range(200):
            dados = rnd.randbytes(rnd.randint(2, 200))
            ntfs.descomprimir_lznt1(dados, 4096)  # não pode levantar exceção

    def test_saida_acima_do_limite_rejeitada(self):
        import random
        original = random.Random(7).randbytes(4096)  # sem período => literais
        comp = lznt1_comprimir_unidade(original)
        des = ntfs.descomprimir_lznt1(comp, 10)
        self.assertIsNone(des)


class TesteConversaoDatas(unittest.TestCase):
    def test_data_valida(self):
        # 130000000000000000 * 100ns desde 1601 => ~17.01.2023
        dt = ntfs.ntfs_para_datetime(133490000000000000)
        self.assertIsNotNone(dt)
        self.assertIn(dt.year, (2023, 2024))

    def test_zero_invalido(self):
        self.assertIsNone(ntfs.ntfs_para_datetime(0))

    def test_fora_do_intervalo(self):
        self.assertIsNone(ntfs.ntfs_para_datetime((1 << 63) + 5))


class TesteBootNTFS(unittest.TestCase):
    def test_info_boot_valido(self):
        from particoes import _info_ntfs
        img = construir_ntfs([ArquivoTeste("a.jpg", b"x", apagado=True)])
        info = _info_ntfs(img[:512])
        self.assertIsNotNone(info)
        self.assertEqual(info["tipo"], "ntfs")
        self.assertEqual(info["rec_size"], 1024)
        self.assertEqual(info["mft_cluster"], 2)

    def test_rec_size_por_clusters_por_registro(self):
        from particoes import _info_ntfs
        img = bytearray(construir_ntfs([ArquivoTeste("a.jpg", b"x", apagado=True)],
                                       spc=1)[:512])
        # spc=1 => registro = 1 cluster*512? cpmft positivo = clusters/registro
        info = _info_ntfs(bytes(img))
        self.assertIsNotNone(info)

    def test_boot_invalido(self):
        from particoes import _info_ntfs
        self.assertIsNone(_info_ntfs(b"\x00" * 512))
        self.assertIsNone(_info_ntfs(b"NTFS    " + b"\x00" * 504))


class TesteRecuperacaoNTFS(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_ntfs_")
        self.jp = jpeg_real(48, 36)
        self.png = png_real(24, 18)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    def _fs(self, arquivos, **kw):
        img = construir_ntfs(arquivos, **kw)
        fs, caminho = _fs_de_imagem(img, self.pasta)
        return fs, img

    def test_boot_reconhecido_pelo_identificador(self):
        from particoes import identificar_boot
        fs, img = self._fs([])
        t, info = identificar_boot(img[:512])
        self.assertEqual(t, "ntfs")
        fs.leitor.fechar()

    def test_numero_registros_via_mft(self):
        fs, _img = self._fs([])
        self.assertEqual(fs.numero_registros(), 64)
        fs.leitor.fechar()

    def test_arquivo_apagado_recuperado_com_nome(self):
        fs, _img = self._fs([ArquivoTeste("ferias.jpg", self.jp, apagado=True)])
        cands, recusas = fs.recuperar(orfaos=False)
        nomes = [c.nome for c in cands]
        self.assertIn("ferias.jpg", nomes)
        c = next(c for c in cands if c.nome == "ferias.jpg")
        self.assertEqual(c.tamanho, len(self.jp))
        self.assertEqual(c.metodo, "mft")
        self.assertIsNotNone(c.data)
        self.assertEqual(c.data.year, 2024)
        fs.leitor.fechar()

    def test_conteudo_recuperado_byte_a_byte(self):
        fs, _img = self._fs([ArquivoTeste("foto.jpg", self.jp, apagado=True)])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(c for c in cands if c.nome == "foto.jpg")
        dados = fs.leitor.read(c.fragmentos[0].offset, c.tamanho)
        self.assertEqual(dados, self.jp)
        fs.leitor.fechar()

    def test_arquivo_vivo_nao_recuperado(self):
        fs, _img = self._fs([ArquivoTeste("viva.jpg", self.jp, apagado=False)])
        cands, _r = fs.recuperar(orfaos=False)
        self.assertNotIn("viva.jpg", [c.nome for c in cands])
        fs.leitor.fechar()

    def test_fragmentado_dois_extents(self):
        grande = self.png * 300  # ~30 KB => vários clusters
        fs, _img = self._fs([ArquivoTeste("frag.png", grande, apagado=True,
                                          fragmentado=True)])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(c for c in cands if c.nome == "frag.png")
        self.assertEqual(c.extra["n_extents"], 2)
        self.assertGreaterEqual(len(c.fragmentos), 2)
        buf = bytearray()
        for f in c.fragmentos:
            buf += fs.leitor.read(f.offset, f.tamanho)
        self.assertEqual(bytes(buf[:c.tamanho]), grande)
        fs.leitor.fechar()

    def test_residente_pequeno(self):
        fs, _img = self._fs([])  # placeholder; criamos manualmente abaixo
        fs.leitor.fechar()
        pequeno = b"conteudo residente"
        a = ArquivoTeste("nota.txt", pequeno, apagado=True)
        a.__dict__["extra_ntfs"] = {"residente": True}
        fs, _img = self._fs([a])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(c for c in cands if c.nome == "nota.txt")
        self.assertTrue(c.extra["residente"])
        self.assertEqual(c.extra["conteudo"], pequeno)
        fs.leitor.fechar()

    def test_comprimido_lznt1(self):
        original = b"CICLO123" * 512  # 4096 periódico
        a = ArquivoTeste("comp.jpg", original, apagado=True)
        a.__dict__["extra_ntfs"] = {"comprimido": True}
        fs, _img = self._fs([a])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(c for c in cands if c.nome == "comp.jpg")
        self.assertTrue(c.extra["comprimido"])
        rl = ntfs.RunList([])
        for _vcn, runs in c.extra["runs"]:
            rl.runs.extend(runs.runs)
        dados = fs.ler_dados_runlist(rl, c.tamanho, comprimido=True,
                                     unidade=c.extra["unidade_compressao"])
        self.assertEqual(dados, original)
        fs.leitor.fechar()

    def test_caminho_reconstruido_via_subpasta(self):
        fs, _img = self._fs([ArquivoTeste("praia.jpg", self.jp, apagado=True,
                                          subpasta="Viagens")])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(c for c in cands if c.nome == "praia.jpg")
        self.assertEqual(c.caminho_original, "Viagens\\praia.jpg")
        fs.leitor.fechar()

    def test_bitmap_cluster_alocado(self):
        fs, _img = self._fs([ArquivoTeste("b.jpg", self.jp, apagado=False)])
        self.assertTrue(fs.cluster_alocado(2))     # MFT
        self.assertIs(fs.cluster_alocado(2) or False, True)
        # cluster muito além do bitmap => None ou False
        fs.leitor.fechar()

    def test_recusa_registro_sem_data(self):
        # registro com FILE_NAME mas sem DATA => recusa registrada
        reg_bytes = _registro_mft(30, False, False,
                                  [_attr_file_name("semdata.jpg", (1 << 48) | 5)])
        img = bytearray(construir_ntfs([]))
        off = 2 * 4096 + 30 * 1024
        img[off:off + 1024] = reg_bytes
        fs, _c = _fs_de_imagem(bytes(img), self.pasta)
        cands, recusas = fs.recuperar(orfaos=False)
        self.assertIn("semdata.jpg", [r["nome"] for r in recusas])
        self.assertTrue(any("sem atributo DATA" in r["motivo"] for r in recusas))
        fs.leitor.fechar()

    def test_leitura_alem_do_fim_do_volume(self):
        fs, _img = self._fs([])
        n = fs.numero_registros()
        self.assertIsNone(fs.ler_registro_bruto(n + 100000))
        fs.leitor.fechar()


if __name__ == "__main__":
    unittest.main(verbosity=2)
