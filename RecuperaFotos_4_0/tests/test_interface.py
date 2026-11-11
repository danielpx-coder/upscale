"""
test_interface.py — testes da interface gráfica (interface.py) sem servidor
gráfico.

O ambiente de testes pode não ter Tk compilado (Linux sem python3-tk) nem
display. Por isso interface.py é importado com um Tkinter *dublê* (tk_stub),
que registra tudo o que a janela faz e devolve o controle ao teste. Assim dá
para verificar de verdade:

  * a janela inteira é construída (4 seções, abas, botões, barra de progresso);
  * cada opção da tela vira o campo certo de motor.Opcoes;
  * as validações de segurança bloqueiam o início (origem/destino/data);
  * o destino em outro disco físico é aceito e a recuperação roda até o fim
    pela GUI, com log, progresso, contadores e galeria;
  * pausa após a etapa 1 habilita "Continuar"/"Finalizar" e obedece ao clique;
  * cancelar encerra com segurança e o status diz "cancelada";
  * a fila de atualização é thread-safe (nada de tocar widget fora da thread);
  * o log não cresce sem limite e a galeria respeita LIMITE_MINIATURAS;
  * diálogos, avisos e "abrir pasta/diagnóstico" se comportam como o esperado.

Os testes usam /dev/shm (tmpfs) como "outro disco físico" quando precisam que
a validação real de destino passe.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tk_stub

_SALVOS: dict = {}
interface = None


def setUpModule():
    global interface
    _SALVOS.update(tk_stub.instalar())
    sys.modules.pop("interface", None)
    interface = importlib.import_module("interface")


def tearDownModule():
    sys.modules.pop("interface", None)
    tk_stub.desinstalar(_SALVOS)
    _SALVOS.clear()


import motor  # noqa: E402
from candidatos import ESTADO_CORROMPIDO, ESTADO_OK, METODO_PASTA, METODO_VARREDURA  # noqa: E402
from construtor import ArquivoTeste, construir_fat32, gravar, jpeg_real  # noqa: E402

OUTRO_DISCO = "/dev/shm" if os.path.isdir("/dev/shm") else None


def png_ruidoso(largura: int, altura: int) -> bytes:
    """PNG de ruído (grande o bastante para passar no filtro de 8 KB)."""
    import io
    import random

    from PIL import Image
    rnd = random.Random(largura * 1000 + altura)
    brutos = bytes(rnd.randrange(256) for _ in range(largura * altura * 3))
    img = Image.frombytes("RGB", (largura, altura), brutos)
    bio = io.BytesIO()
    img.save(bio, "PNG")
    return bio.getvalue()


class BaseGui(unittest.TestCase):
    def setUp(self):
        tk_stub.limpar()
        self.pasta = tempfile.mkdtemp(prefix="rf4_gui_")
        # fotos acima do filtro padrão de 8 KB (a GUI não tem --min-kb 0)
        self.jp = jpeg_real(160, 120, semente=42)
        self.png = png_ruidoso(90, 70)
        self.assertGreater(len(self.jp), 8192)
        self.assertGreater(len(self.png), 8192)
        self.root = interface.tk.Tk()
        self.app = interface.Aplicativo(self.root)

    def tearDown(self):
        for t in (getattr(self.app, "thread", None),):
            if t and t.is_alive():
                self.app.cancelar.set()
                t.join(timeout=10)
        shutil.rmtree(self.pasta, ignore_errors=True)
        if getattr(self, "pasta_outro_disco", None):
            shutil.rmtree(self.pasta_outro_disco, ignore_errors=True)

    # ------------------------------------------------------------- utilidades
    def imagem_com_fotos(self, n=1, sistema="fat32"):
        arquivos = [ArquivoTeste(f"foto{k}.jpg", jpeg_real(160, 120, semente=k),
                                 apagado=True) for k in range(n)]
        arquivos.append(ArquivoTeste("extra.png", self.png, apagado=True))
        img = construir_fat32(arquivos, tam_setores=16384)
        return gravar(os.path.join(self.pasta, "origem.img"), img)

    def destino_outro_disco(self):
        """Pasta em outro dispositivo (tmpfs): a validação real de destino
        passa sem precisar burlar nenhuma regra de segurança."""
        if not OUTRO_DISCO:
            self.skipTest("sem segundo dispositivo (tmpfs) para o destino")
        self.pasta_outro_disco = tempfile.mkdtemp(prefix="rf4_gui_dest_",
                                                  dir=OUTRO_DISCO)
        destino = os.path.join(self.pasta_outro_disco, "recuperadas")
        os.makedirs(destino, exist_ok=True)      # destino precisa existir
        return destino

    def rodar_ate_o_fim(self, maximo=400):
        """Junta a thread de trabalho e processa a fila da interface."""
        if self.app.thread:
            self.app.thread.join(timeout=60)
            self.assertFalse(self.app.thread.is_alive(), "a thread não terminou")
        return tk_stub.drenar_after(maximo=maximo)

    def texto_do_log(self):
        return self.app.txt_log.get("1.0", "end")

    def celulas_da_galeria(self):
        return [w for w in self.app.quadro_gal.winfo_children()
                if not w.destruido]

    def mensagens(self, tipo=None):
        return [m for m in tk_stub.MENSAGENS if tipo is None or m[0] == tipo]


# ===========================================================================
# 1. Construção da janela
# ===========================================================================


class TesteConstrucaoDaJanela(BaseGui):
    def test_titulo_geometria_e_tamanho_minimo(self):
        self.assertEqual(self.root.title(), "Recupera Fotos 4.0")
        self.assertEqual(self.root.geometry(), "1240x780")
        self.assertEqual(self.root.minsize(), (1080, 660))

    def test_as_quatro_secoes_de_configuracao(self):
        textos = [w.configuracoes.get("text", "") for w in tk_stub.REGISTRO]
        for secao in ("1. Cartão, pendrive, HD ou pasta", "2. Onde salvar",
                      "3. Período", "4. Opções"):
            self.assertIn(secao, textos, f"seção ausente: {secao}")

    def test_abas_de_resultado_e_registro(self):
        abas = [kw.get("text") for _w, kw in self.app.abas.abas]
        self.assertEqual(abas, ["Fotos recuperadas", "Registro"])

    def test_botoes_de_acao_e_seus_estados_iniciais(self):
        self.assertNotEqual(self.app.btn_iniciar.configuracoes.get("state"),
                            "disabled")
        for botao in (self.app.btn_cancelar, self.app.btn_continuar,
                      self.app.btn_finalizar):
            self.assertEqual(botao.configuracoes.get("state"), "disabled")
        # Continuar/Finalizar só aparecem quando há pausa
        self.assertIsNone(self.app.btn_continuar.empacotado)
        self.assertIsNone(self.app.btn_finalizar.empacotado)

    def test_todas_as_opcoes_da_secao_4_existem(self):
        textos = [w.configuracoes.get("text", "") for w in tk_stub.REGISTRO]
        for opcao in ("Recuperar também vídeos",
                      "Buscar assinaturas apenas no espaço livre",
                      "Conferir as imagens e separar as corrompidas",
                      "Busca profunda", "Incluir arquivos menores de 8 KB",
                      "Pausar depois da primeira etapa",
                      "Buscar partições perdidas"):
            self.assertTrue(any(opcao in t for t in textos), opcao)

    def test_periodos_oferecidos(self):
        valores = [w.configuracoes.get("value") for w in tk_stub.REGISTRO
                   if isinstance(w, interface.tk.Radiobutton)]
        for periodo in ("todas", "hoje", "ontem", "7d", "30d", "especifica"):
            self.assertIn(periodo, valores)

    def test_labels_de_informacao_criados(self):
        for chave in ("dispositivo", "sistema", "etapa", "recuperados", "bytes",
                      "erros", "progresso", "tempo"):
            self.assertIn(chave, self.app.labels_info)

    def test_painel_de_seguranca_do_disco_do_sistema_existe(self):
        self.assertIsNotNone(self.app.lbl_disco_sistema)

    def test_executar_gui_abre_janela_e_entra_no_mainloop(self):
        interface.executar_gui()
        self.assertEqual(tk_stub.CHAMADAS_MAINLOOP, [0])


# ===========================================================================
# 2. Funções puras
# ===========================================================================


class TesteFuncoesPuras(unittest.TestCase):
    def test_formatar_bytes(self):
        casos = [(0, "0.0 B"), (512, "512.0 B"), (1023, "1023.0 B"),
                 (1024, "1.0 KB"), (1536, "1.5 KB"), (1024 ** 2, "1.0 MB"),
                 (5 * 1024 ** 3, "5.0 GB"), (2 * 1024 ** 4, "2.0 TB")]
        for valor, esperado in casos:
            self.assertEqual(interface.formatar_bytes(valor), esperado, valor)

    def test_formatar_bytes_pb(self):
        self.assertTrue(interface.formatar_bytes(3 * 1024 ** 5).endswith("PB"))

    def test_formatar_duracao(self):
        casos = [(0, "0s"), (9, "9s"), (65, "1min05s"), (600, "10min00s"),
                 (3725, "1h02min"), (7325, "2h02min")]
        for valor, esperado in casos:
            self.assertEqual(interface._formatar_duracao(valor), esperado, valor)

    def test_formatar_duracao_negativa(self):
        self.assertEqual(interface._formatar_duracao(-5), "0s")

    def test_limite_de_miniaturas_e_modesto(self):
        self.assertGreater(interface.LIMITE_MINIATURAS, 0)
        self.assertLessEqual(interface.LIMITE_MINIATURAS, 500)


# ===========================================================================
# 3. Mapeamento tela -> motor.Opcoes
# ===========================================================================


class TesteOpcoesDaGui(BaseGui):
    def test_valores_padrao(self):
        self.app.origem_selecionada = "/tmp/x.img"
        self.app.var_destino.set("/tmp/saida")
        op = self.app._opcoes_da_gui()
        self.assertEqual(op.origem, "/tmp/x.img")
        self.assertEqual(op.destino, "/tmp/saida")
        self.assertFalse(op.videos)
        self.assertTrue(op.apenas_espaco_livre)
        self.assertTrue(op.verificar)
        self.assertFalse(op.sem_verificar)
        self.assertFalse(op.profunda)
        self.assertFalse(op.incluir_pequenos)
        self.assertIsNone(op.min_kb)
        self.assertFalse(op.pausar_apos_etapa1)
        self.assertFalse(op.buscar_particoes)
        self.assertEqual(op.desde, "")
        self.assertFalse(op.ignorar_sem_data)
        self.assertFalse(op.permitir_mesmo_disco)

    def test_destino_com_espacos_e_trim(self):
        self.app.var_destino.set("   /tmp/  saida  ")
        self.assertEqual(self.app._opcoes_da_gui().destino, "/tmp/  saida")

    def test_periodos_relativos(self):
        for periodo, esperado in (("todas", ""), ("hoje", "hoje"),
                                  ("ontem", "ontem"), ("7d", "7d"),
                                  ("30d", "30d")):
            self.app.var_periodo.set(periodo)
            self.assertEqual(self.app._opcoes_da_gui().desde, esperado, periodo)

    def test_periodo_especifico_usa_o_campo_de_data(self):
        self.app.var_periodo.set("especifica")
        self.app.var_data.set("25/12/2019")
        self.assertEqual(self.app._opcoes_da_gui().desde, "25/12/2019")

    def test_opcoes_marcadas(self):
        self.app.var_videos.set(True)
        self.app.var_espaco_livre.set(False)
        self.app.var_verificar.set(False)
        self.app.var_profunda.set(True)
        self.app.var_pequenos.set(True)
        self.app.var_pausar.set(True)
        self.app.var_particoes.set(True)
        self.app.var_sem_data.set(False)
        op = self.app._opcoes_da_gui()
        self.assertTrue(op.videos)
        self.assertFalse(op.apenas_espaco_livre)
        self.assertFalse(op.verificar)
        self.assertTrue(op.sem_verificar)
        self.assertTrue(op.profunda)
        self.assertTrue(op.incluir_pequenos)
        self.assertEqual(op.min_kb, 0)
        self.assertTrue(op.pausar_apos_etapa1)
        self.assertTrue(op.buscar_particoes)
        self.assertTrue(op.ignorar_sem_data)

    def test_nunca_autoriza_mesmo_disco_por_conta_propria(self):
        """A GUI não tem interruptor para ignorar a regra do disco físico."""
        for var in (self.app.var_videos, self.app.var_pequenos,
                    self.app.var_profunda, self.app.var_particoes,
                    self.app.var_espaco_livre, self.app.var_pausar):
            var.set(True)
        self.assertFalse(self.app._opcoes_da_gui().permitir_mesmo_disco)


# ===========================================================================
# 4. Validações antes de começar
# ===========================================================================


class TesteValidacoesDeInicio(BaseGui):
    def test_sem_origem_avisa_e_nao_inicia(self):
        self.app.origem_selecionada = None
        self.app.var_destino.set("/tmp/x")
        self.app._iniciar()
        self.assertIsNone(self.app.thread)
        self.assertTrue(any("origem" in m[2].lower()
                            for m in self.mensagens("showwarning")),
                        tk_stub.MENSAGENS)

    def test_sem_destino_avisa_e_nao_inicia(self):
        self.app.origem_selecionada = "/tmp/x.img"
        self.app.var_destino.set("   ")
        self.app._iniciar()
        self.assertIsNone(self.app.thread)
        self.assertTrue(any("destino" in m[2].lower()
                            for m in self.mensagens("showwarning")))

    def test_destino_no_mesmo_disco_e_bloqueado(self):
        caminho = self.imagem_com_fotos(1)
        destino = os.path.join(self.pasta, "saida")     # mesmo disco da imagem
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.assertFalse(self.app._validar_destino_gui())
        self.assertIn("bloqueado", self.app.lbl_destino_aviso.cget("text"))
        self.app._iniciar()
        self.assertIsNone(self.app.thread)
        self.assertTrue(self.mensagens("showerror"))

    def test_destino_valido_em_outro_disco_e_aceito(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.assertTrue(self.app._validar_destino_gui())
        self.assertIn("válido", self.app.lbl_destino_aviso.cget("text"))

    def test_validar_destino_sem_origem_exige_origem(self):
        self.app.origem_selecionada = None
        self.app.var_destino.set("/tmp/qualquer")
        self.assertFalse(self.app._validar_destino_gui())
        self.assertIn("origem", self.app.lbl_destino_aviso.cget("text"))

    def test_destino_vazio_nao_gera_aviso(self):
        self.app.var_destino.set("")
        self.assertTrue(self.app._validar_destino_gui())
        self.assertEqual(self.app.lbl_destino_aviso.cget("text"), "")

    def test_data_especifica_invalida_bloqueia(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app.var_periodo.set("especifica")
        self.app.var_data.set("45/45/2020")
        self.app._iniciar()
        self.assertIsNone(self.app.thread)
        self.assertTrue(any("Data específica inválida" in m[2]
                            for m in self.mensagens("showerror")))


# ===========================================================================
# 5. Recuperação completa pela interface
# ===========================================================================


class TesteRecuperacaoPelaGui(BaseGui):
    def test_fluxo_completo(self):
        caminho = self.imagem_com_fotos(2)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app._iniciar()
        self.assertIsNotNone(self.app.thread)
        self.assertEqual(self.app.btn_iniciar.configuracoes.get("state"),
                         "disabled")
        self.assertEqual(self.app.btn_cancelar.configuracoes.get("state"),
                         "normal")
        self.rodar_ate_o_fim()

        res = self.app.resultado
        self.assertIsNotNone(res)
        self.assertEqual(res.recuperados, 3)          # 2 jpg + 1 png
        self.assertIn("Recuperação concluída: 3 arquivo(s)",
                      self.app.lbl_status.cget("text"))
        self.assertEqual(self.app.var_progresso.get(), 100)
        self.assertEqual(self.app.labels_info["recuperados"].cget("text"), "3")
        self.assertEqual(self.app.labels_info["etapa"].cget("text"), "concluído")
        self.assertIn("fat32", self.app.labels_info["sistema"].cget("text"))
        log = self.texto_do_log()
        self.assertIn("RECUPERADO", log)
        self.assertIn("Etapa 1", log)
        # botões voltam ao estado inicial
        self.assertEqual(self.app.btn_iniciar.configuracoes.get("state"), "normal")
        self.assertEqual(self.app.btn_cancelar.configuracoes.get("state"),
                         "disabled")
        # arquivos realmente gravados no destino escolhido
        gravados = sorted(os.listdir(destino))
        self.assertIn("foto0.jpg", gravados)
        self.assertIn("relatorio_recuperacao.csv", gravados)

    def test_galeria_mostra_as_fotos_com_estado(self):
        caminho = self.imagem_com_fotos(2)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app._iniciar()
        self.rodar_ate_o_fim()
        celulas = self.celulas_da_galeria()
        self.assertEqual(len(celulas), 3)
        textos = []
        for c in celulas:
            for filho in c.winfo_children():
                textos.append(filho.configuracoes.get("text", ""))
        self.assertTrue(any("foto0.jpg" in t for t in textos), textos)
        self.assertTrue(any(ESTADO_OK in t for t in textos), textos)
        # contadores no topo da galeria
        contadores = self.app.lbl_contadores.cget("text")
        self.assertIn("ok: 3", contadores)
        self.assertIn(f"{ESTADO_CORROMPIDO}: 0", contadores)

    def test_miniatura_e_gerada_para_fotos(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app._iniciar()
        self.rodar_ate_o_fim()
        if interface.PIL_OK:
            self.assertGreaterEqual(len(self.app.miniaturas), 1)
        else:
            self.assertEqual(self.app.miniaturas, {})

    def test_filtro_da_galeria_por_estado(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app._iniciar()
        self.rodar_ate_o_fim()
        total = len(self.celulas_da_galeria())
        self.app.var_filtro.set(ESTADO_OK)
        self.app._refiltrar_galeria()
        self.assertEqual(len(self.celulas_da_galeria()), total)
        self.app.var_filtro.set(ESTADO_CORROMPIDO)
        self.app._refiltrar_galeria()
        self.assertEqual(len(self.celulas_da_galeria()), 0)
        self.app.var_filtro.set("todos")
        self.app._refiltrar_galeria()
        self.assertEqual(len(self.celulas_da_galeria()), total)

    def test_pausa_apos_etapa1_habilita_continuar_e_finalizar(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app.var_pausar.set(True)
        self.app.var_profunda.set(True)
        self.app._iniciar()
        # espera a pausa chegar pela fila
        limite = time.time() + 30
        while time.time() < limite:
            tk_stub.drenar_after(maximo=5)
            if self.app.btn_continuar.configuracoes.get("state") == "normal":
                break
            time.sleep(0.02)
        self.assertEqual(self.app.btn_continuar.configuracoes.get("state"),
                         "normal")
        self.assertEqual(self.app.btn_finalizar.configuracoes.get("state"),
                         "normal")
        self.assertIsNotNone(self.app.btn_continuar.empacotado)
        self.assertIn("Pausado após a etapa 1",
                      self.app.lbl_status.cget("text"))
        self.assertTrue(self.app.thread.is_alive())
        # usuário clica em "Continuar (etapa 2)"
        self.app.btn_continuar.invoke()
        self.rodar_ate_o_fim()
        self.assertIsNotNone(self.app.resultado)
        self.assertFalse(self.app.resultado.cancelado)
        self.assertEqual(self.app.btn_continuar.configuracoes.get("state"),
                         "disabled")
        self.assertIsNone(self.app.btn_continuar.empacotado)

    def test_finalizar_na_pausa_encerra_sem_etapa2(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app.var_pausar.set(True)
        self.app._iniciar()
        limite = time.time() + 30
        while time.time() < limite:
            tk_stub.drenar_after(maximo=5)
            if self.app.btn_finalizar.configuracoes.get("state") == "normal":
                break
            time.sleep(0.02)
        self.app.btn_finalizar.invoke()
        self.rodar_ate_o_fim()
        res = self.app.resultado
        self.assertIsNotNone(res)
        self.assertTrue(res.pausado)
        self.assertFalse(res.cancelado)
        self.assertEqual(res.por_metodo.get(METODO_VARREDURA, 0), 0)
        status = self.app.lbl_status.cget("text")
        self.assertIn("finalizada após a etapa 1", status)
        self.assertNotIn("concluída", status)

    def test_cancelar_encerra_com_seguranca(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app.var_profunda.set(True)
        self.app.var_videos.set(True)
        self.app._iniciar()
        time.sleep(0.02)
        self.app.btn_cancelar.invoke()
        self.assertIn("Cancelando", self.app.lbl_status.cget("text"))
        self.rodar_ate_o_fim()
        self.assertIsNotNone(self.app.resultado)
        self.assertTrue(self.app.resultado.cancelado)
        self.assertIn("cancelada", self.app.lbl_status.cget("text"))

    def test_dois_cliques_nao_iniciam_duas_recuperacoes(self):
        caminho = self.imagem_com_fotos(1)
        destino = self.destino_outro_disco()
        self.app.origem_selecionada = caminho
        self.app.var_destino.set(destino)
        self.app._iniciar()
        primeira = self.app.thread
        self.app._iniciar()                       # clique duplicado
        self.assertIs(self.app.thread, primeira)
        self.rodar_ate_o_fim()

    def test_erro_do_motor_vira_mensagem_e_nao_quebra_a_janela(self):
        self.app.origem_selecionada = os.path.join(self.pasta, "nao_existe.img")
        destino = self.destino_outro_disco()
        self.app.var_destino.set(destino)
        # a origem não existe: abrir_origem falha dentro da thread de trabalho
        self.app._validar_destino_gui = lambda: True
        self.app._iniciar()
        self.rodar_ate_o_fim()
        self.assertTrue(self.mensagens("showerror"), tk_stub.MENSAGENS)
        self.assertIn("Erro", self.app.lbl_status.cget("text"))
        self.assertEqual(self.app.btn_iniciar.configuracoes.get("state"), "normal")


# ===========================================================================
# 6. Fila thread-safe e log
# ===========================================================================


class TesteFilaELog(BaseGui):
    def test_callbacks_so_colocam_na_fila(self):
        """Nenhum widget é tocado pela thread de trabalho: os callbacks apenas
        enfileiram (é isso que evita travar/corromper a janela)."""
        antes = [w.configuracoes.copy() for w in tk_stub.REGISTRO[:5]]
        erros = []

        def chamar():
            try:
                for k in range(50):
                    self.app._cb_log(f"mensagem {k}")
                    self.app._cb_progresso(k / 50, 1.0, "etapa1")
            except Exception as exc:            # pragma: no cover
                erros.append(exc)

        threads = [threading.Thread(target=chamar) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(erros, [])
        self.assertEqual(self.app.fila.qsize(), 400)
        depois = [w.configuracoes for w in tk_stub.REGISTRO[:5]]
        self.assertEqual(antes, depois, "widget alterado fora da thread da GUI")

    def test_fila_e_processada_na_ordem(self):
        self.app._cb_log("primeira")
        self.app._cb_log("segunda")
        self.app._cb_progresso(0.5, 1.0, "carving")
        tk_stub.drenar_after(maximo=1)
        log = self.texto_do_log()
        self.assertLess(log.index("primeira"), log.index("segunda"))
        self.assertEqual(self.app.labels_info["etapa"].cget("text"), "carving")
        self.assertEqual(self.app.var_progresso.get(), 50.0)

    def test_log_tem_limite_de_linhas(self):
        for k in range(5200):
            self.app._log(f"linha {k}")
        linhas = self.texto_do_log().count("\n")
        self.assertLess(linhas, 4100, "o log cresceu sem limite")
        self.assertGreater(linhas, 900)
        self.assertIn("linha 5199", self.texto_do_log())
        self.assertNotIn("linha 0\n", self.texto_do_log())

    def test_log_tem_hora(self):
        self.app._log("teste")
        self.assertRegex(self.texto_do_log(), r"\[\d\d:\d\d:\d\d\] teste")

    def test_limpar_resultados_zera_tudo(self):
        self.app._log("alguma coisa")
        self.app.var_progresso.set(77)
        self.app.lbl_contadores.config(text="ok: 5")
        self.app._limpar_resultados()
        self.assertEqual(self.texto_do_log(), "")
        self.assertEqual(self.app.var_progresso.get(), 0)
        self.assertEqual(self.app.lbl_contadores.cget("text"), "")
        self.assertEqual(self.app.miniaturas, {})


# ===========================================================================
# 7. Galeria: limites de memória e cliques
# ===========================================================================


class TesteGaleria(BaseGui):
    def _resultado_falso(self, n, estado=ESTADO_OK):
        from candidatos import CandidatoArquivo, Fragmento
        res = motor.ResultadoRecuperacao()
        caminho_real = gravar(os.path.join(self.pasta, "mini.jpg"), self.jp)
        for k in range(n):
            c = CandidatoArquivo(f"foto_{k:04d}.jpg", [Fragmento(k * 4096, 2048)],
                                 METODO_VARREDURA, "jpeg", tamanho=2048)
            c.estado = estado
            c.arquivo_destino = caminho_real if k < 3 else None
            res.candidatos.append(c)
        res.recuperados = n
        res.por_estado[estado] = n
        return res

    def test_galeria_respeita_o_limite_de_miniaturas(self):
        self.app.resultado = self._resultado_falso(interface.LIMITE_MINIATURAS + 300)
        self.app._reconstruir_galeria()
        self.assertLessEqual(len(self.celulas_da_galeria()),
                             interface.LIMITE_MINIATURAS)
        self.assertLessEqual(len(self.app.miniaturas),
                             interface.LIMITE_MINIATURAS)

    def test_sem_resultado_galeria_fica_vazia(self):
        self.app.resultado = None
        self.app._reconstruir_galeria()
        self.assertEqual(self.celulas_da_galeria(), [])

    def test_miniatura_apenas_para_formatos_de_imagem(self):
        from candidatos import CandidatoArquivo, Fragmento
        caminho = gravar(os.path.join(self.pasta, "m.jpg"), self.jp)
        c = CandidatoArquivo("m.jpg", [Fragmento(0, 100)], METODO_PASTA, "jpeg",
                             tamanho=100)
        c.arquivo_destino = caminho
        if interface.PIL_OK:
            self.assertIsNotNone(self.app._miniatura(c))
        c2 = CandidatoArquivo("v.mp4", [Fragmento(0, 100)], METODO_VARREDURA,
                              "mp4", tamanho=100)
        c2.arquivo_destino = caminho
        self.assertIsNone(self.app._miniatura(c2))
        c3 = CandidatoArquivo("x.jpg", [Fragmento(0, 100)], METODO_PASTA, "jpeg",
                              tamanho=100)
        c3.arquivo_destino = os.path.join(self.pasta, "nao_existe.jpg")
        self.assertIsNone(self.app._miniatura(c3))

    def test_clique_na_miniatura_abre_janela_ampliada(self):
        self.app.resultado = self._resultado_falso(1)
        self.app._reconstruir_galeria()
        janelas_antes = len(tk_stub.widgets_de(interface.tk.Toplevel))
        cand = self.app.resultado.candidatos[0]
        self.app._abrir_ampliada(cand)
        janelas = tk_stub.widgets_de(interface.tk.Toplevel)
        self.assertEqual(len(janelas), janelas_antes + 1)
        textos = []
        for w in tk_stub.REGISTRO:
            t = w.configuracoes.get("text")
            if isinstance(t, str):
                textos.append(t)
        self.assertTrue(any("estado:" in t for t in textos), textos[-5:])
        self.assertTrue(any(cand.estado in t for t in textos))

    def test_abrir_ampliada_sem_arquivo_nao_faz_nada(self):
        from candidatos import CandidatoArquivo, Fragmento
        c = CandidatoArquivo("some.jpg", [Fragmento(0, 10)], METODO_PASTA,
                             "jpeg", tamanho=10)
        c.arquivo_destino = os.path.join(self.pasta, "inexistente.jpg")
        antes = len(tk_stub.widgets_de(interface.tk.Toplevel))
        self.app._abrir_ampliada(c)
        self.assertEqual(len(tk_stub.widgets_de(interface.tk.Toplevel)), antes)

    def test_nome_longo_e_cortado_na_celula(self):
        from candidatos import CandidatoArquivo, Fragmento
        res = motor.ResultadoRecuperacao()
        c = CandidatoArquivo("n" * 80 + ".jpg", [Fragmento(0, 10)],
                             METODO_PASTA, "jpeg", tamanho=10)
        c.arquivo_destino = None
        res.candidatos.append(c)
        self.app.resultado = res
        self.app._reconstruir_galeria()
        textos = [w.configuracoes.get("text", "")
                  for cel in self.celulas_da_galeria()
                  for w in cel.winfo_children()]
        self.assertTrue(any(t.endswith("…") for t in textos), textos)


# ===========================================================================
# 8. Dispositivos, diálogos e utilidades
# ===========================================================================


class TesteDispositivosEDialogos(BaseGui):
    def _patch_listar(self, retorno=None, erro=None):
        original = interface.aquisicao.listar_dispositivos

        def falso(**kw):
            if erro:
                raise erro
            return retorno
        interface.aquisicao.listar_dispositivos = falso
        self.addCleanup(setattr, interface.aquisicao, "listar_dispositivos",
                        original)

    def test_lista_de_dispositivos_formatada(self):
        self._patch_listar([
            {"id": "d0", "nome": "/dev/sda", "modelo": "Cartão SD",
             "capacidade": 32 * 1024 ** 3, "unidades": [], "removivel": True,
             "disco_sistema": False, "caminho": "/dev/sda"},
            {"id": "d1", "nome": "Disco 0", "modelo": "SSD",
             "capacidade": 512 * 1024 ** 3, "unidades": ["C:"],
             "removivel": False, "disco_sistema": True, "caminho": "\\\\.\\PhysicalDrive0"},
        ])
        self.app._atualizar_dispositivos()
        itens = self.app.lista_disp.get()
        self.assertEqual(len(itens), 2)
        self.assertIn("32.0 GB", itens[0])
        self.assertIn("removível", itens[0])
        self.assertIn("DISCO DO SISTEMA", itens[1])
        self.assertIn("C:", itens[1])

    def test_sem_dispositivos_orienta_o_usuario(self):
        self._patch_listar([])
        self.app._atualizar_dispositivos()
        itens = self.app.lista_disp.get()
        self.assertEqual(len(itens), 1)
        self.assertIn("nenhum dispositivo detectado", itens[0])
        self.assertIn("imagem", itens[0])

    def test_falha_ao_listar_nao_quebra_a_janela(self):
        self._patch_listar(erro=RuntimeError("sem permissão"))
        self.app._atualizar_dispositivos()
        self.assertEqual(self.app.dispositivos, [])
        self.assertIn("falha ao listar dispositivos", self.texto_do_log())

    def test_selecionar_disco_do_sistema_gera_alerta(self):
        self._patch_listar([
            {"id": "d0", "nome": "Disco 0", "modelo": "SSD", "capacidade": 1,
             "unidades": ["C:"], "removivel": False, "disco_sistema": True,
             "caminho": "\\\\.\\PhysicalDrive0"}])
        self.app._atualizar_dispositivos()
        self.app.lista_disp.ativar_selecao(0)
        self.assertEqual(self.app.origem_selecionada, "\\\\.\\PhysicalDrive0")
        self.assertIn("disco do sistema", self.app.lbl_disco_sistema.cget("text"))
        self.assertIn("somente-leitura", self.app.lbl_disco_sistema.cget("text"))

    def test_selecionar_sem_indice_nao_faz_nada(self):
        self.app.lista_disp.selecao = ()
        self.app._ao_selecionar_dispositivo()
        self.assertIsNone(self.app.origem_selecionada)

    def test_dialogo_de_imagem_define_a_origem(self):
        tk_stub.RESPOSTAS_ARQUIVO.append("/mnt/cartao/dump.img")
        self.app._escolher_imagem()
        self.assertEqual(self.app.origem_selecionada, "/mnt/cartao/dump.img")
        self.assertIn("arquivo de imagem", self.app.lbl_origem.cget("text"))

    def test_dialogo_cancelado_nao_altera_a_origem(self):
        self.app.origem_selecionada = "/antes.img"
        tk_stub.RESPOSTAS_ARQUIVO.append("")            # usuário cancelou
        self.app._escolher_imagem()
        self.assertEqual(self.app.origem_selecionada, "/antes.img")

    def test_dialogo_de_pasta_de_origem(self):
        tk_stub.RESPOSTAS_PASTA.append("/mnt/cartao/DCIM")
        self.app._escolher_pasta_origem()
        self.assertEqual(self.app.origem_selecionada, "/mnt/cartao/DCIM")
        self.assertIn("metadados do volume físico",
                      self.app.lbl_origem.cget("text"))

    def test_dialogo_de_destino_valida_em_seguida(self):
        caminho = self.imagem_com_fotos(1)
        self.app.origem_selecionada = caminho
        tk_stub.RESPOSTAS_PASTA.append(self.pasta)      # mesmo disco: bloqueia
        self.app._escolher_destino()
        self.assertEqual(self.app.var_destino.get(), self.pasta)
        self.assertIn("bloqueado", self.app.lbl_destino_aviso.cget("text"))

    def test_abrir_destino_invalido_avisa(self):
        self.app.var_destino.set("")
        self.app._abrir_destino()
        self.assertTrue(any("Nenhum destino válido" in m[2]
                            for m in self.mensagens("showinfo")))

    def test_abrir_destino_valido_chama_o_sistema(self):
        import subprocess
        chamadas = []
        original = interface.subprocess.Popen
        interface.subprocess.Popen = lambda cmd, *a, **k: chamadas.append(cmd)
        self.addCleanup(setattr, interface.subprocess, "Popen", original)
        self.app.var_destino.set(self.pasta)
        self.app._abrir_destino()
        self.assertTrue(chamadas)
        self.assertEqual(chamadas[0][-1], self.pasta)
        del subprocess

    def test_abrir_diagnostico_sem_ntfs_avisa(self):
        self.app.resultado = None
        self.app._abrir_diagnostico()
        self.assertTrue(any("diagnóstico NTFS" in m[2]
                            for m in self.mensagens("showinfo")))

    def test_abrir_diagnostico_quando_existe(self):
        res = motor.ResultadoRecuperacao()
        res.diagnostico_csv = gravar(os.path.join(self.pasta, "diag.csv"),
                                     b"registro_MFT;nome;motivo\r\n")
        self.app.resultado = res
        chamadas = []
        original = interface.subprocess.Popen
        interface.subprocess.Popen = lambda cmd, *a, **k: chamadas.append(cmd)
        self.addCleanup(setattr, interface.subprocess, "Popen", original)
        self.app._abrir_diagnostico()
        self.assertEqual(chamadas[0][-1], res.diagnostico_csv)

    def test_falha_ao_abrir_com_o_sistema_avisa(self):
        original = interface.subprocess.Popen

        def explode(*a, **k):
            raise OSError("sem xdg-open")
        interface.subprocess.Popen = explode
        self.addCleanup(setattr, interface.subprocess, "Popen", original)
        interface.Aplicativo._abrir_com_sistema("/tmp/qualquer")
        self.assertTrue(any("Não foi possível abrir" in m[2]
                            for m in self.mensagens("showwarning")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
