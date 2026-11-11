"""Testes de aquisição forense, leitores e validação de origem/destino."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aquisicao
from aquisicao import (
    Adquiridor,
    ErroLeitura,
    Intervalos,
    LeitorArquivo,
    LeitorRecorte,
    OrigemPasta,
    abrir_origem,
    destino_mesmo_disco_fisico,
    validar_destino,
)


class TesteLeitorArquivo(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_aq_")
        self.caminho = os.path.join(self.pasta, "imagem.img")
        self.dados = bytes(range(256)) * 64  # 16 KiB
        with open(self.caminho, "wb") as f:
            f.write(self.dados)
        self.leitor = LeitorArquivo(self.caminho)

    def tearDown(self):
        self.leitor.fechar()
        shutil.rmtree(self.pasta, ignore_errors=True)

    def test_leitura_simples(self):
        self.assertEqual(self.leitor.read(0, 16), self.dados[:16])
        self.assertEqual(self.leitor.read(255, 2), self.dados[255:257])

    def test_tamanho(self):
        self.assertEqual(self.leitor.tamanho, len(self.dados))

    def test_leitura_alem_do_fim_retorna_zeros(self):
        d = self.leitor.read(len(self.dados) - 4, 16)
        self.assertEqual(len(d), 16)
        self.assertEqual(d[:4], self.dados[-4:])
        self.assertEqual(d[4:], b"\x00" * 12)

    def test_offset_fora_retorna_vazio(self):
        self.assertEqual(self.leitor.read(len(self.dados) + 10, 4), b"")

    def test_offset_negativo_levanta(self):
        with self.assertRaises(ErroLeitura):
            self.leitor.read(-1, 4)

    @unittest.skipUnless(sys.platform != "win32", "verificação via /proc (Linux)")
    def test_aberto_somente_leitura(self):
        # o descritor interno não tem modo de gravação
        modo = None
        for fd in range(3, 64):
            try:
                st = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if st == self.caminho:
                # verifica ausência de O_WRONLY/O_RDWR nos flags
                import fcntl
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                modo = "rdonly"
        self.assertEqual(modo, "rdonly")

    def test_context_manager(self):
        with LeitorArquivo(self.caminho) as leitor:
            self.assertEqual(leitor.read(0, 4), self.dados[:4])


class TesteLeitorRecorte(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_rec_")
        self.caminho = os.path.join(self.pasta, "d.img")
        self.dados = bytes((i * 7) % 256 for i in range(8192))
        with open(self.caminho, "wb") as f:
            f.write(self.dados)
        self.base = LeitorArquivo(self.caminho)

    def tearDown(self):
        self.base.fechar()
        shutil.rmtree(self.pasta, ignore_errors=True)

    def test_recorte_deslocado(self):
        r = LeitorRecorte(self.base, 1024, 2048, "recorte")
        self.assertEqual(r.tamanho, 2048)
        self.assertEqual(r.read(0, 32), self.dados[1024:1056])
        self.assertEqual(r.read(2048, 8), b"")

    def test_recorte_limitado_pelo_tamanho_base(self):
        r = LeitorRecorte(self.base, 8000, 999999)
        self.assertEqual(r.tamanho, 192)

    def test_recorte_nao_fecha_base(self):
        r = LeitorRecorte(self.base, 0, 100)
        r.fechar()
        self.assertEqual(self.base.read(0, 4), self.dados[:4])


class TesteAbrirOrigem(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_orig_")
        self.img = os.path.join(self.pasta, "o.img")
        with open(self.img, "wb") as f:
            f.write(b"\x00" * 4096)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    def test_arquivo_de_imagem(self):
        leitor = abrir_origem(self.img)
        self.assertIsInstance(leitor, LeitorArquivo)
        leitor.fechar()

    def test_pasta_retorna_origem_pasta(self):
        o = abrir_origem(self.pasta)
        self.assertIsInstance(o, OrigemPasta)
        self.assertEqual(o.pasta, os.path.abspath(self.pasta))

    def test_origem_inexistente(self):
        with self.assertRaises(ValueError):
            abrir_origem(os.path.join(self.pasta, "nao_existe.img"))

    def test_origem_vazia(self):
        with self.assertRaises(ValueError):
            abrir_origem("   ")

    @unittest.skipUnless(sys.platform == "win32", "letras de unidade: só Windows")
    def test_letra_unidade_windows(self):
        leitor = abrir_origem("C:")
        self.assertTrue(leitor.e_dispositivo)

    @unittest.skipUnless(sys.platform != "win32", "discoN no Linux usa /sys/block")
    def test_disco_inexistente_linux(self):
        with self.assertRaises((ValueError, OSError)):
            abrir_origem("disco9999")


class TesteIntervalos(unittest.TestCase):
    def test_vazio(self):
        iv = Intervalos()
        self.assertEqual(len(iv), 0)
        self.assertFalse(iv.contem(0))
        self.assertEqual(iv.total_bytes(), 0)

    def test_adicionar_e_conter(self):
        iv = Intervalos()
        iv.adicionar(100, 200)
        self.assertTrue(iv.contem(100, 200))
        self.assertTrue(iv.contem(150))
        self.assertFalse(iv.contem(99, 101))
        self.assertFalse(iv.contem(200, 201))

    def test_mesclagem(self):
        iv = Intervalos()
        iv.adicionar(0, 100)
        iv.adicionar(50, 150)
        self.assertEqual(len(iv), 1)
        self.assertTrue(iv.contem(0, 150))
        self.assertEqual(iv.total_bytes(), 150)

    def test_mesclagem_adjacentes(self):
        iv = Intervalos()
        iv.adicionar(0, 100)
        iv.adicionar(100, 200)
        self.assertEqual(len(iv), 1)

    def test_intervalos_desjuntos_ordenados(self):
        iv = Intervalos()
        iv.adicionar(300, 400)
        iv.adicionar(0, 100)
        iv.adicionar(600, 700)
        lista = list(iv)
        self.assertEqual(lista, [(0, 100), (300, 400), (600, 700)])

    def test_sobrepoe(self):
        iv = Intervalos()
        iv.adicionar(100, 200)
        self.assertTrue(iv.sobrepoe(150, 250))
        self.assertTrue(iv.sobrepoe(50, 150))
        self.assertFalse(iv.sobrepoe(200, 300))
        self.assertFalse(iv.sobrepoe(0, 100))

    def test_contido_por_partes_nao_contem(self):
        iv = Intervalos()
        iv.adicionar(0, 100)
        iv.adicionar(200, 300)
        self.assertFalse(iv.contem(50, 250))

    def test_intervalo_invalido_ignorado(self):
        iv = Intervalos()
        iv.adicionar(100, 100)
        iv.adicionar(200, 150)
        self.assertEqual(len(iv), 0)


class TesteAdquiridor(unittest.TestCase):
    BLOCO = 4096

    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_adq_")
        self.origem = os.path.join(self.pasta, "origem.img")
        self.dados = os.urandom(3 * 4096 + 100)
        with open(self.origem, "wb") as f:
            f.write(self.dados)
        self.img_saida = os.path.join(self.pasta, "copia.img")

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    def _adquirir(self, retomar=False, cancelar=None):
        leitor = LeitorArquivo(self.origem)
        adq = Adquiridor(leitor, self.img_saida, bloco=self.BLOCO,
                         cancelar=cancelar)
        resumo = adq.executar(retomar=retomar)
        leitor.fechar()
        return adq, resumo

    def test_imagem_identica_byte_a_byte(self):
        self._adquirir()
        with open(self.img_saida, "rb") as f:
            self.assertEqual(f.read(), self.dados)

    def test_mapa_versao_1_criado(self):
        self._adquirir()
        caminho_mapa = self.img_saida + ".map.jsonl"
        self.assertTrue(os.path.exists(caminho_mapa))
        with open(caminho_mapa, encoding="utf-8") as f:
            linhas = [json.loads(ln) for ln in f if ln.strip()]
        cab = linhas[0]
        self.assertEqual(cab["versao"], 1)
        self.assertEqual(cab["tamanho"], len(self.dados))
        self.assertEqual(cab["bloco"], self.BLOCO)
        regs = linhas[1:]
        self.assertEqual(len(regs), (len(self.dados) + self.BLOCO - 1) // self.BLOCO)

    def test_sha256_dos_blocos_correto(self):
        self._adquirir()
        with open(self.img_saida + ".map.jsonl", encoding="utf-8") as f:
            linhas = [json.loads(ln) for ln in f if ln.strip()][1:]
        for k, reg in enumerate(linhas):
            bloco = self.dados[k * self.BLOCO:(k + 1) * self.BLOCO]
            self.assertEqual(reg["sha256"], hashlib.sha256(bloco).hexdigest())
            self.assertEqual(reg["estado"], "ok")

    def test_blocos_zerados_registrados_como_zero(self):
        with open(self.origem, "wb") as f:
            f.write(b"\x00" * 2 * self.BLOCO)
        _adq, resumo = self._adquirir()
        self.assertEqual(resumo["zero"], 2)
        self.assertEqual(resumo["erro"], 0)

    def test_setores_ilegivel_registrado_como_erro(self):
        class LeitorComFalha(LeitorArquivo):
            def read(self, offset, size):
                if offset == 4096:
                    self.errores_leitura += 1
                    raise ErroLeitura("falha simulada")
                return super().read(offset, size)
        leitor = LeitorComFalha(self.origem)
        adq = Adquiridor(leitor, self.img_saida, bloco=self.BLOCO)
        resumo = adq.executar()
        leitor.fechar()
        self.assertEqual(resumo["erro"], 1)
        with open(self.img_saida + ".map.jsonl", encoding="utf-8") as f:
            regs = [json.loads(ln) for ln in f if ln.strip()][1:]
        self.assertEqual(regs[1]["estado"], "erro")
        # bloco de erro preenchido com 0xF8 (não confundir com zeros)
        with open(self.img_saida, "rb") as f:
            f.seek(self.BLOCO)
            self.assertEqual(f.read(self.BLOCO), b"\xf8" * self.BLOCO)

    def test_imagem_existente_sem_retomar_levanta(self):
        with open(self.img_saida, "wb") as f:
            f.write(b"x")
        with self.assertRaises(FileExistsError):
            self._adquirir(retomar=False)

    def test_retomada_revalida_blocos(self):
        self._adquirir()
        # simula interrupção: tudo copiado; retomada deve revalidar
        _adq, resumo = self._adquirir(retomar=True)
        self.assertTrue(resumo["retomada"])
        n_blocos = (len(self.dados) + self.BLOCO - 1) // self.BLOCO
        self.assertEqual(resumo["revalidados"], n_blocos)
        with open(self.img_saida, "rb") as f:
            self.assertEqual(f.read(), self.dados)

    def test_retomada_recopia_bloco_corrompido(self):
        self._adquirir()
        with open(self.img_saida, "r+b") as f:
            f.seek(self.BLOCO)
            f.write(b"\xAA" * 32)  # corrompe o bloco 1
        _adq, resumo = self._adquirir(retomar=True)
        self.assertEqual(resumo["revalidados"],
                         (len(self.dados) + self.BLOCO - 1) // self.BLOCO - 1)
        with open(self.img_saida, "rb") as f:
            self.assertEqual(f.read(), self.dados)

    def test_cancelamento_seguro(self):
        cancelar = threading.Event()
        cancelar.set()
        _adq, resumo = self._adquirir(cancelar=cancelar)
        self.assertTrue(resumo["cancelado"])

    def test_sha256_arquivo(self):
        self._adquirir()
        esperado = hashlib.sha256(self.dados).hexdigest()
        self.assertEqual(Adquiridor.sha256_arquivo(self.img_saida), esperado)

    def test_mapa_incompativel_ignorado_na_retomada(self):
        self._adquirir()
        with open(self.img_saida + ".map.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"versao": 99}) + "\n")
        _adq, resumo = self._adquirir(retomar=True)
        self.assertEqual(resumo.get("revalidados", 0), 0)


class TesteValidacaoDestino(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.mkdtemp(prefix="rf4_val_")
        self.origem_pasta = os.path.join(self.pasta, "origem")
        self.destino = os.path.join(self.pasta, "destino")
        os.makedirs(self.origem_pasta)
        self.img = os.path.join(self.pasta, "o.img")
        with open(self.img, "wb") as f:
            f.write(b"\x00" * 512)

    def tearDown(self):
        shutil.rmtree(self.pasta, ignore_errors=True)

    def test_destino_dentro_da_pasta_de_origem_bloqueado(self):
        o = OrigemPasta(self.origem_pasta, None, 0, None)
        dentro = os.path.join(self.origem_pasta, "saida")
        os.makedirs(dentro)
        mesmo, motivo = destino_mesmo_disco_fisico(o, dentro)
        self.assertTrue(mesmo)
        self.assertIn("dentro da pasta pesquisada", motivo)

    def test_validar_destino_cria_pasta(self):
        leitor = LeitorArquivo(self.img)
        novo = os.path.join(self.pasta, "novo_destino")
        res = validar_destino(leitor, novo, permitir_mesmo_disco=True)
        self.assertTrue(res.ok)
        self.assertTrue(os.path.isdir(novo))
        leitor.fechar()

    def test_validar_destino_inexistente_sem_criar_explica(self):
        """Com criar_se_nao_existe=False (é assim que a interface valida antes
        de iniciar), a pasta que não existe precisa ser dita como inexistente —
        e não como "sem permissão de gravação", que era o que o os.access()
        devolvia para um caminho ausente."""
        leitor = LeitorArquivo(self.img)
        faltando = os.path.join(self.pasta, "nao_existe", "saida")
        res = validar_destino(leitor, faltando, permitir_mesmo_disco=True,
                              criar_se_nao_existe=False)
        self.assertFalse(res.ok)
        self.assertIn("não existe", res.motivo)
        self.assertFalse(os.path.exists(faltando))
        leitor.fechar()

    def test_validar_destino_arquivo_nao_pasta(self):
        leitor = LeitorArquivo(self.img)
        res = validar_destino(leitor, self.img, permitir_mesmo_disco=True)
        self.assertFalse(res.ok)
        self.assertIn("não é uma pasta", res.motivo)
        leitor.fechar()

    def test_mesmo_disco_confirmado_bloqueia(self):
        leitor = LeitorArquivo(self.img)
        # destino no mesmo disco do arquivo de origem (mesmo st_dev)
        res = validar_destino(leitor, self.destino, permitir_mesmo_disco=False)
        self.assertFalse(res.ok)
        self.assertIn("mesmo disco físico", res.motivo)
        leitor.fechar()

    def test_permitir_mesmo_disco_apenas_para_imagem(self):
        leitor = LeitorArquivo(self.img)
        res = validar_destino(leitor, self.destino, permitir_mesmo_disco=True)
        self.assertTrue(res.ok)
        self.assertTrue(any("autorizad" in a for a in res.advertencias))
        leitor.fechar()

    def test_origem_pasta_mesmo_disco_bloqueada_sem_flag(self):
        # OrigemPasta sem leitor: disco indeterminado => bloqueio explicado
        o = OrigemPasta(self.origem_pasta, None, 0, None)
        res = validar_destino(o, self.destino, permitir_mesmo_disco=False)
        # pasta sem leitor: não há como confirmar separação física
        self.assertFalse(res.ok)
        self.assertTrue("mesmo disco" in res.motivo or "confirmar" in res.motivo
                        or "dentro" in res.motivo or "contém" in res.motivo)

    def test_origem_pasta_destino_valido(self):
        o = OrigemPasta(self.origem_pasta, None, 0, None)
        fora = tempfile.mkdtemp(prefix="rf4_fora_")
        try:
            res = validar_destino(o, fora, permitir_mesmo_disco=True)
            self.assertTrue(res.ok)
        finally:
            shutil.rmtree(fora, ignore_errors=True)


class TesteListagemDispositivos(unittest.TestCase):
    def test_listar_retorna_lista_de_dicts(self):
        if sys.platform == "win32" and not _tem_permissao_windows():
            self.skipTest("exige administrador no Windows")
        dispositivos = aquisicao.listar_dispositivos(todos=True)
        self.assertIsInstance(dispositivos, list)
        for d in dispositivos:
            for chave in ("id", "nome", "capacidade", "unidades", "removivel",
                          "disco_sistema", "caminho"):
                self.assertIn(chave, d)


def _tem_permissao_windows() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
