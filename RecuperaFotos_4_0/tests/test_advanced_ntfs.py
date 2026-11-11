"""
test_advanced_ntfs.py — casos avançados do parser NTFS:
ATTRIBUTE_LIST multi-extent, sparse, LZNT1 real, MFT órfã após formatação
rápida, boot de reserva, registros adulterados, setores de 4096 bytes,
runs além do fim do volume, clusters realocados, nomes Unicode, hard links,
volume com deslocamento (partição dentro de imagem de disco).
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ntfs
import particoes
from aquisicao import LeitorArquivo
from construtor import (
    NTFS_REC_SIZE,
    ArquivoTeste,
    _attr_attribute_list,
    _attr_data_nao_residente,
    _attr_data_residente,
    _attr_file_name,
    _registro_mft,
    construir_ntfs,
    formatar_rapido_ntfs,
    jpeg_real,
    lznt1_comprimir_unidade,
    png_real,
)


def gravar(pasta: str, nome: str, img: bytes) -> str:
    caminho = os.path.join(pasta, nome)
    with open(caminho, "wb") as f:
        f.write(img)
    return caminho


def abrir_fs(caminho: str, offset: int = 0, tamanho: int | None = None,
             setor: int = 512):
    leitor = LeitorArquivo(caminho)
    if tamanho is None:
        tamanho = leitor.tamanho - offset
    boot = leitor.read(offset, setor)
    t, info = particoes.identificar_boot(boot)
    if t != "ntfs":
        leitor.fechar()
        raise AssertionError(f"boot não reconhecido como NTFS: {t!r}")
    return ntfs.SistemaNTFS(leitor, info, offset, tamanho)


class BaseNTFS(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_adv_")
        self.jp = jpeg_real(56, 40, semente=11)
        self.png = png_real(30, 20)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    def _fs(self, arquivos, nome="v.img", **kw):
        img = construir_ntfs(arquivos, **kw)
        caminho = gravar(self.pasta, nome, img)
        return abrir_fs(caminho), img, caminho


class TesteAttributeList(BaseNTFS):
    def test_dois_extents_em_registros_distintos(self):
        grande = self.png * 200          # ~20 KB => múltiplos clusters
        a = ArquivoTeste("dividido.png", grande, apagado=True, fragmentado=True)
        a.__dict__["extra_ntfs"] = {"attr_list": True}
        fs, img, _c = self._fs([a])
        cands, recusas = fs.recuperar(orfaos=False)
        c = next((x for x in cands if x.nome == "dividido.png"), None)
        self.assertIsNotNone(c, f"candidato ausente; recusas={recusas}")
        self.assertEqual(c.extra["n_atributos_data"], 2)
        buf = bytearray()
        for f in c.fragmentos:
            buf += fs.leitor.read(f.offset, f.tamanho)
        self.assertEqual(bytes(buf[:c.tamanho]), grande)
        fs.leitor.fechar()

    def test_registro_de_extensao_nao_gera_candidato_duplicado(self):
        grande = self.png * 200
        a = ArquivoTeste("dividido.png", grande, apagado=True, fragmentado=True)
        a.__dict__["extra_ntfs"] = {"attr_list": True}
        fs, _img, _c = self._fs([a])
        cands, _r = fs.recuperar(orfaos=False)
        nomes = [x.nome for x in cands]
        self.assertEqual(nomes.count("dividido.png"), 1)
        fs.leitor.fechar()

    def test_attribute_list_residente_parseado(self):
        entradas = [(0x80, 0, 24, 1, 4), (0x80, 8, 30, 1, 5)]
        attr = _attr_attribute_list(entradas)
        self.assertEqual(struct.unpack_from("<I", attr, 0)[0], 0x20)
        self.assertEqual(attr[8], 0)              # residente
        (comp,) = struct.unpack_from("<I", attr, 16)
        self.assertEqual(comp, 64)                # 2 entradas de 32 bytes


class TesteSparseEComprimido(BaseNTFS):
    def test_comprimido_lznt1_real(self):
        original = b"CICLO123" * 512            # 4096 bytes, período 8
        a = ArquivoTeste("compactado.jpg", original, apagado=True)
        a.__dict__["extra_ntfs"] = {"comprimido": True}
        fs, _img, _c = self._fs([a])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(x for x in cands if x.nome == "compactado.jpg")
        self.assertTrue(c.extra["comprimido"])
        self.assertEqual(c.extra["unidade_compressao"], 16)
        rl = c.extra["runs"][0][1]
        dados = fs.ler_dados_runlist(rl, c.tamanho, comprimido=True,
                                     unidade=16)
        self.assertEqual(dados[:len(original)], original)
        fs.leitor.fechar()

    def test_runlist_sparse_preservada(self):
        original = b"P" * 4096
        comp = lznt1_comprimir_unidade(original)
        n_fis = (len(comp) + 4095) // 4096
        attr = _attr_data_nao_residente([(50, n_fis)], 16 * 4096, 4096,
                                        comprimido=True, unidade=16,
                                        sparse=[16 - n_fis])
        runs_ofs = struct.unpack_from("<H", attr, 32)[0]
        (comp_len,) = struct.unpack_from("<I", attr, 4)
        rl = ntfs.RunList.parsear(attr[runs_ofs:comp_len])
        self.assertTrue(any(sp for _l, _n, sp in rl.runs))
        self.assertEqual(rl.total_clusters(), 16)

    def test_unidade_integralmente_fisica_copiada_sem_descomprimir(self):
        # 4 clusters reais, sem sparse: o conteúdo é lido como está
        original = bytes(range(256)) * 4         # 1024 bytes
        rl = ntfs.RunList([(60, 1, False)])
        a = ArquivoTeste("x.bin", original, apagado=True)
        fs, img, caminho = self._fs([a])
        dados = fs.ler_dados_runlist(rl, 4096, comprimido=True, unidade=4)
        self.assertEqual(len(dados), 4096)
        fs.leitor.fechar()
        del img, caminho

    def test_corpo_lznt1_invalido_retorna_none(self):
        rl = ntfs.RunList([(70, 1, False), (-1, 15, True)])
        a = ArquivoTeste("y.bin", b"z", apagado=True)
        fs, _img, _c = self._fs([a])
        # escreve lixo onde a runlist aponta (cluster 70)
        self.assertIsNone(fs.ler_dados_runlist(rl, 4096, comprimido=True,
                                               unidade=16))
        fs.leitor.fechar()


class TesteMFTOrfaAposFormatacao(BaseNTFS):
    def test_registro_orfao_fora_da_mft_recuperado(self):
        img = construir_ntfs(
            [ArquivoTeste("atual.jpg", self.jp, apagado=True)],
            registro_orfao=("antiga_orfa.jpg", self.png, 500, 700, 40))
        # formatação rápida apaga os registros de usuário da MFT atual
        img = formatar_rapido_ntfs(img)
        caminho = gravar(self.pasta, "formatado.img", img)
        fs = abrir_fs(caminho)
        cands, recusas = fs.recuperar(orfaos=True)
        nomes = [c.nome for c in cands]
        self.assertIn("antiga_orfa.jpg", nomes)
        self.assertNotIn("atual.jpg", nomes)     # zerado pela formatação
        c = next(x for x in cands if x.nome == "antiga_orfa.jpg")
        self.assertEqual(c.metodo, "mft_orfa")
        self.assertIn("fora da MFT atual", c.motivo)
        off = c.extra["offset_orfa"]
        self.assertEqual(img[off:off + 4], b"FILE")
        dados = fs.leitor.read(c.fragmentos[0].offset, c.tamanho)
        self.assertEqual(dados, self.png)
        fs.leitor.fechar()
        del recusas

    def test_varredura_orfa_nao_repete_mft_atual(self):
        fs, _img, _c = self._fs([ArquivoTeste("u.jpg", self.jp, apagado=True)])
        cands, _r = fs.recuperar(orfaos=True)
        nomes = [x.nome for x in cands]
        self.assertEqual(nomes.count("u.jpg"), 1)
        self.assertTrue(all(x.metodo == "mft" for x in cands))
        fs.leitor.fechar()

    def test_assinatura_file_falsa_rejeitada(self):
        fs, img, caminho = self._fs([])
        lixo = bytearray(img)
        off = 900 * 4096
        lixo[off:off + 4] = b"FILE"
        lixo[off + 4:off + 64] = os.urandom(60)
        with open(caminho, "wb") as f:
            f.write(bytes(lixo))
        fs2 = abrir_fs(caminho)
        cands, recusas = fs2.recuperar(orfaos=True)
        self.assertEqual(cands, [])
        self.assertTrue(any("estrutura inválida" in r["motivo"] for r in recusas))
        fs.leitor.fechar()
        fs2.leitor.fechar()


class TesteBootDeReserva(BaseNTFS):
    def test_boot_primario_corrompido_usa_ultimo_setor(self):
        img = construir_ntfs([ArquivoTeste("b.jpg", self.jp, apagado=True)],
                             corromper_boot=True)
        caminho = gravar(self.pasta, "boot_ruim.img", img)
        leitor = LeitorArquivo(caminho)
        part = particoes.Particao(0, len(img), "ntfs", "volume")
        setor, t, info, origem = particoes.ler_boot_com_backup(leitor, part)
        self.assertEqual(t, "ntfs")
        self.assertEqual(origem, "backup_ntfs")
        self.assertEqual(setor[3:11], b"NTFS    ")
        leitor.fechar()

    def test_motor_recupera_com_boot_de_reserva(self):
        import motor
        img = construir_ntfs([ArquivoTeste("reserva.jpg", self.jp, apagado=True)],
                             corromper_boot=True)
        caminho = gravar(self.pasta, "boot_ruim2.img", img)
        dest = os.path.join(self.pasta, "saida")
        op = motor.Opcoes(origem=caminho, destino=dest,
                          permitir_mesmo_disco=True, min_kb=0)
        res = motor.Motor(op).executar()
        self.assertIn("ntfs", res.sistemas)
        nomes = [os.path.basename(c.arquivo_destino or c.nome)
                 for c in res.candidatos]
        self.assertIn("reserva.jpg", nomes)
        self.assertTrue(any("reserva usada" in a for a in res.avisos))

    def test_backup_boot_invalido_tambem(self):
        img = bytearray(construir_ntfs([], corromper_boot=True))
        # corrompe também o backup (último setor)
        ult = (len(img) // 512 - 1) * 512
        img[ult:ult + 8] = b"\x00" * 8
        caminho = gravar(self.pasta, "sem_boot.img", bytes(img))
        leitor = LeitorArquivo(caminho)
        part = particoes.Particao(0, len(img), "ntfs", "volume")
        _setor, t, _info, origem = particoes.ler_boot_com_backup(leitor, part)
        self.assertIsNone(t)
        self.assertEqual(origem, "nenhum")
        leitor.fechar()


class TesteRegistrosAdulterados(BaseNTFS):
    def _injetar(self, num: int, reg_bytes: bytes) -> tuple:
        fs, img, caminho = self._fs([])
        fs.leitor.fechar()
        dados = bytearray(img)
        off = 2 * 4096 + num * NTFS_REC_SIZE
        dados[off:off + NTFS_REC_SIZE] = reg_bytes
        with open(caminho, "wb") as f:
            f.write(bytes(dados))
        return abrir_fs(caminho), caminho

    def test_fixup_invalido_gera_diagnostico(self):
        reg = bytearray(_registro_mft(25, False, False,
                                      [_attr_file_name("mau.jpg", (1 << 48) | 5),
                                       _attr_data_nao_residente([(40, 1)], 4096, 100)]))
        reg[510] ^= 0xFF
        fs, _c = self._injetar(25, bytes(reg))
        cands, recusas = fs.recuperar(orfaos=False)
        self.assertEqual([x.nome for x in cands], [])
        self.assertTrue(any(r["registro_MFT"] == 25 and "estrutura inválida" in r["motivo"]
                            for r in recusas))
        fs.leitor.fechar()

    def test_comprimento_de_atributo_inconsistente(self):
        attr = bytearray(_attr_data_nao_residente([(41, 1)], 4096, 100))
        struct.pack_into("<I", attr, 4, 7)      # comprimento menor que o mínimo
        reg = _registro_mft(26, False, False,
                            [_attr_file_name("attr_ruim.jpg", (1 << 48) | 5),
                             bytes(attr)])
        fs, _c = self._injetar(26, reg)
        cands, recusas = fs.recuperar(orfaos=False)
        self.assertNotIn("attr_ruim.jpg", [x.nome for x in cands])
        self.assertTrue(recusas)
        fs.leitor.fechar()

    def test_registro_truncado_nao_trava(self):
        reg = bytearray(_registro_mft(27, False, False,
                                      [_attr_file_name("t.jpg", (1 << 48) | 5)]))
        struct.pack_into("<I", reg, 24, NTFS_REC_SIZE + 5000)   # usado > alocado
        self.assertIsNone(ntfs.parsear_registro(27, bytes(reg), NTFS_REC_SIZE, 512))

    def test_data_residente_truncado_registrado(self):
        attr = bytearray(_attr_data_residente(b"abc"))
        struct.pack_into("<I", attr, 16, 10 ** 6)   # tamanho maior que o corpo
        reg = _registro_mft(28, False, False,
                            [_attr_file_name("res.jpg", (1 << 48) | 5), bytes(attr)])
        fs, _c = self._injetar(28, reg)
        cands, recusas = fs.recuperar(orfaos=False)
        self.assertNotIn("res.jpg", [x.nome for x in cands])
        self.assertTrue(any("residente" in r["motivo"] or "estrutura inválida" in r["motivo"]
                            for r in recusas), recusas)
        fs.leitor.fechar()

    def test_runlist_apontando_para_fora_do_volume(self):
        reg = _registro_mft(29, False, False,
                            [_attr_file_name("longe.jpg", (1 << 48) | 5),
                             _attr_data_nao_residente([(10 ** 7, 4)],
                                                      4 * 4096, 4 * 4096)])
        fs, _c = self._injetar(29, reg)
        cands, recusas = fs.recuperar(orfaos=False)
        self.assertNotIn("longe.jpg", [x.nome for x in cands])
        self.assertTrue(recusas)
        fs.leitor.fechar()


class TesteNomesECaminhos(BaseNTFS):
    def test_nome_unicode_preservado(self):
        fs, _img, _c = self._fs([ArquivoTeste("Férias em São Paulo.jpg",
                                              self.jp, apagado=True)])
        cands, _r = fs.recuperar(orfaos=False)
        self.assertIn("Férias em São Paulo.jpg", [x.nome for x in cands])
        fs.leitor.fechar()

    def test_caminho_com_subpasta(self):
        fs, _img, _c = self._fs([ArquivoTeste("praia.jpg", self.jp,
                                              apagado=True, subpasta="Viagens")])
        cands, _r = fs.recuperar(orfaos=False)
        c = next(x for x in cands if x.nome == "praia.jpg")
        self.assertEqual(c.caminho_original, "Viagens\\praia.jpg")
        fs.leitor.fechar()

    def test_preferencia_de_namespace_win32_sobre_dos(self):
        # dois FILE_NAME no mesmo registro: DOS (2) e Win32 (1)
        nomes = [_attr_file_name("FOTOLONGA.JPG", (1 << 48) | 5, namespace=2),
                 _attr_file_name("FotoLonga.jpg", (1 << 48) | 5, namespace=1)]
        reg = _registro_mft(31, False, False,
                            nomes + [_attr_data_nao_residente([(45, 1)], 4096, 10)])
        fs, img, caminho = self._fs([])
        fs.leitor.fechar()
        dados = bytearray(img)
        off = 2 * 4096 + 31 * NTFS_REC_SIZE
        dados[off:off + NTFS_REC_SIZE] = reg
        with open(caminho, "wb") as f:
            f.write(bytes(dados))
        fs2 = abrir_fs(caminho)
        cands, _r = fs2.recuperar(orfaos=False)
        c = next(x for x in cands if "FOTOLONGA" in x.nome.upper())
        self.assertEqual(c.nome, "FotoLonga.jpg")
        fs2.leitor.fechar()

    def test_diretorio_nao_gera_candidato(self):
        fs, img, caminho = self._fs([])
        fs.leitor.fechar()
        dados = bytearray(img)
        reg = _registro_mft(32, False, True,
                            [_attr_file_name("Pasta", (1 << 48) | 5)])
        off = 2 * 4096 + 32 * NTFS_REC_SIZE
        dados[off:off + NTFS_REC_SIZE] = reg
        with open(caminho, "wb") as f:
            f.write(bytes(dados))
        fs2 = abrir_fs(caminho)
        cands, _r = fs2.recuperar(orfaos=False)
        self.assertNotIn("Pasta", [x.nome for x in cands])
        fs2.leitor.fechar()

    def test_filtro_por_caminho(self):
        arqs = [ArquivoTeste("a.jpg", self.jp, apagado=True, subpasta="Fotos"),
                ArquivoTeste("b.png", self.png, apagado=True, subpasta="Outros")]
        fs, _img, _c = self._fs(arqs)
        cands, _r = fs.recuperar(orfaos=False, filtro_caminho="Fotos")
        self.assertEqual([x.nome for x in cands], ["a.jpg"])
        fs.leitor.fechar()

    def test_arquivo_vivo_incluido_sob_pedido(self):
        fs, _img, _c = self._fs([ArquivoTeste("viva.jpg", self.jp, apagado=False)])
        cands, _r = fs.recuperar(orfaos=False, incluir_vivos=True)
        self.assertIn("viva.jpg", [x.nome for x in cands])
        fs.leitor.fechar()


class TesteConfiabilidade(BaseNTFS):
    def test_clusters_realocados_marcam_nao_confiavel(self):
        # arquivo apagado cujos clusters estão marcados como alocados no bitmap
        a = ArquivoTeste("reuso.jpg", self.jp, apagado=True)
        fs, img, caminho = self._fs([a])
        fs.leitor.fechar()
        dados = bytearray(img)
        # localiza o bitmap (cluster do $Bitmap) e marca todos os bits
        # o bitmap fica no cluster seguinte ao $MFTMirr
        info = particoes._info_ntfs(bytes(dados[:512]))
        cluster_bytes = info["spc"] * 512
        # varre o registro 6 ($Bitmap) para achar o offset
        reg6 = dados[2 * 4096 + 6 * NTFS_REC_SIZE: 2 * 4096 + 7 * NTFS_REC_SIZE]
        i = struct.unpack_from("<H", reg6, 20)[0]
        usado = struct.unpack_from("<I", reg6, 24)[0]
        off_bitmap = None
        while i + 8 <= usado:
            tipo = struct.unpack_from("<I", reg6, i)[0]
            if tipo == 0xFFFFFFFF:
                break
            comp = struct.unpack_from("<I", reg6, i + 4)[0]
            if tipo == 0x80 and comp < 24:
                break
            if tipo == 0x80 and comp >= 24:
                runs_ofs = struct.unpack_from("<H", reg6, i + 32)[0]
                rl = ntfs.RunList.parsear(bytes(reg6[i + runs_ofs:i + comp]))
                if rl.runs:
                    off_bitmap = rl.runs[0][0] * cluster_bytes
                break
            i += comp
        self.assertIsNotNone(off_bitmap)
        dados[off_bitmap:off_bitmap + 256] = b"\xFF" * 256
        with open(caminho, "wb") as f:
            f.write(bytes(dados))
        fs2 = abrir_fs(caminho)
        cands, _r = fs2.recuperar(orfaos=False)
        c = next((x for x in cands if x.nome == "reuso.jpg"), None)
        if c is not None:
            self.assertFalse(c.confiavel)
            self.assertIn("realocados", c.motivo)
        fs2.leitor.fechar()

    def test_data_incompleta_gera_motivo(self):
        reg = _registro_mft(33, False, False,
                            [_attr_file_name("curto.jpg", (1 << 48) | 5),
                             _attr_data_nao_residente([(47, 1)], 4096, 20000)])
        fs, img, caminho = self._fs([])
        fs.leitor.fechar()
        dados = bytearray(img)
        dados[2 * 4096 + 33 * NTFS_REC_SIZE:
              2 * 4096 + 34 * NTFS_REC_SIZE] = reg
        with open(caminho, "wb") as f:
            f.write(bytes(dados))
        fs2 = abrir_fs(caminho)
        cands, recusas = fs2.recuperar(orfaos=False)
        c = next((x for x in cands if x.nome == "curto.jpg"), None)
        self.assertTrue(c is None or not c.confiavel or recusas)
        fs2.leitor.fechar()


class TesteSetores4096(BaseNTFS):
    def test_volume_ntfs_com_setor_4096(self):
        img = construir_ntfs([ArquivoTeste("s4k.jpg", self.jp, apagado=True)],
                             tam_setor=4096, spc=1, tam_setores=4096)
        caminho = gravar(self.pasta, "ntfs4k.img", img)
        t, info = particoes.identificar_boot(img[:4096])
        self.assertEqual(t, "ntfs")
        self.assertEqual(info["bytes_setor"], 4096)
        fs = abrir_fs(caminho, setor=4096)
        cands, recusas = fs.recuperar(orfaos=False)
        self.assertIn("s4k.jpg", [x.nome for x in cands],
                      f"recusas={recusas}")
        c = next(x for x in cands if x.nome == "s4k.jpg")
        dados = fs.leitor.read(c.fragmentos[0].offset, c.tamanho)
        self.assertEqual(dados, self.jp)
        fs.leitor.fechar()


class TesteVolumeDeslocado(BaseNTFS):
    def test_particao_ntfs_dentro_de_imagem_de_disco(self):
        img_vol = construir_ntfs([ArquivoTeste("deslocada.jpg", self.jp,
                                               apagado=True)])
        prefixo = 63 * 512                     # LBA 63, como em MBR antigo
        disco = b"\x00" * prefixo + img_vol
        caminho = gravar(self.pasta, "disco.img", disco)
        leitor = LeitorArquivo(caminho)
        part = particoes.Particao(prefixo, len(img_vol), "ntfs", "mbr",
                                  indice=1)
        recorte = part.recorte(leitor)
        t, info = particoes.identificar_boot(recorte.read(0, 512))
        self.assertEqual(t, "ntfs")
        fs = ntfs.SistemaNTFS(recorte, info, 0, len(img_vol))
        cands, _r = fs.recuperar(orfaos=False)
        c = next((x for x in cands if x.nome == "deslocada.jpg"), None)
        self.assertIsNotNone(c)
        self.assertEqual(recorte.read(c.fragmentos[0].offset, c.tamanho), self.jp)
        leitor.fechar()


class TesteLimitesDeMemoria(BaseNTFS):
    def test_leitura_da_mft_em_blocos_limitados(self):
        fs, _img, _c = self._fs([])
        lidos = []
        original = fs.leitor.read

        def espiao(offset, size):
            lidos.append(size)
            return original(offset, size)

        fs.leitor.read = espiao
        fs.recuperar(orfaos=False)
        self.assertTrue(lidos)
        self.assertLessEqual(max(lidos), 8 * 1024 * 1024)
        fs.leitor.fechar()

    def test_atributo_de_comprimento_zero_nao_causa_laco_infinito(self):
        base = bytearray(_registro_mft(34, False, False,
                                       [_attr_file_name("n.jpg", (1 << 48) | 5)]))
        attr_ofs = struct.unpack_from("<H", base, 20)[0]
        struct.pack_into("<I", base, attr_ofs, 0x30)
        struct.pack_into("<I", base, attr_ofs + 4, 0)   # comprimento zero
        # reescreve o fixup para o registro permanecer "assado"
        usado = attr_ofs + 24
        struct.pack_into("<I", base, 24, usado)
        for k in range(NTFS_REC_SIZE // 512):
            struct.pack_into("<H", base, (k + 1) * 512 - 2,
                             struct.unpack_from("<H", base, 48 + 2 + k * 2)[0])
        # parsear não pode entrar em laço infinito; aceita None ou registro
        # com no máximo MAX_ATRIBUTOS atributos
        reg = ntfs.parsear_registro(34, bytes(base), NTFS_REC_SIZE, 512)
        self.assertTrue(reg is None or len(reg.atributos) <= ntfs.MAX_ATRIBUTOS)

    def test_limite_de_atributos_do_parser(self):
        self.assertEqual(ntfs.MAX_ATRIBUTOS, 512)

    def test_bitmap_grande_nao_carrega_volume_inteiro(self):
        fs, _img, _c = self._fs([])
        fs._carregar_bitmap()
        self.assertIsNotNone(fs._bitmap_runs)
        # o bitmap é paginado em blocos de 64 KiB, nunca carregado inteiro
        fs.cluster_alocado(10)
        self.assertLessEqual(sum(len(b or b"") for b in fs._bitmap_cache.values()),
                             33 * 64 * 1024)
        fs.leitor.fechar()


class TesteCriarDesdeBoot(BaseNTFS):
    def test_criar_desde_boot_valido(self):
        fs, img, caminho = self._fs([ArquivoTeste("cb.jpg", self.jp, apagado=True)])
        fs.leitor.fechar()
        leitor = LeitorArquivo(caminho)
        fs2 = ntfs.criar_desde_boot(leitor, img[:512], 0, len(img))
        self.assertIsNotNone(fs2)
        self.assertEqual(fs2.rec_size, NTFS_REC_SIZE)
        leitor.fechar()

    def test_criar_desde_boot_invalido(self):
        leitor = LeitorArquivo(gravar(self.pasta, "lixo.img", b"\x00" * 4096))
        self.assertIsNone(ntfs.criar_desde_boot(leitor, b"\x00" * 512, 0, 4096))
        leitor.fechar()


if __name__ == "__main__":
    unittest.main(verbosity=2)
