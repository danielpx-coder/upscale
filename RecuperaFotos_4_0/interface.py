# -*- coding: utf-8 -*-
"""
interface.py — Interface gráfica (Tkinter + ttk) do Recupera Fotos 4.0.

Painel esquerdo (configurações):
  Seção 1 — Cartão, pendrive, HD ou pasta (dispositivos, imagem, pasta)
  Seção 2 — Onde salvar (validação de disco físico distinto)
  Seção 3 — Período (datas)
  Seção 4 — Opções (vídeos, espaço livre, verificação, busca profunda...)
Painel direito (resultados):
  Aba 1 — Fotos recuperadas (galeria com miniaturas e filtro por estado)
  Aba 2 — Registro (log contínuo)
Barra inferior: progresso, status, "Abrir pasta de destino",
"Abrir diagnóstico NTFS".

A recuperação roda em uma thread de trabalho; a interface só é atualizada via
queue.Queue (thread-safe), evitando congelamento da janela.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import aquisicao
import motor
from candidatos import (ESTADO_CORROMPIDO, ESTADO_NAO_VERIFICADO, ESTADO_OK,
                        ESTADO_PARCIAL)

COR_FUNDO = "#f2f3f5"
COR_CARTAO = "#ffffff"
COR_TEXTO = "#1c1e21"
COR_AZUL = "#1a66d6"
COR_AZUL_ESCURO = "#124a9e"
LIMITE_MINIATURAS = 200

try:
    from PIL import Image as PILImage
    from PIL import ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False


def formatar_bytes(n: float) -> str:
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unidade}"
        n /= 1024
    return f"{n:.1f} PB"


class Aplicativo:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Recupera Fotos 4.0")
        root.configure(bg=COR_FUNDO)
        root.geometry("1240x780")
        root.minsize(1080, 660)

        self.fila = queue.Queue()
        self.cancelar = threading.Event()
        self.continuar = threading.Event()
        self.finalizar = threading.Event()
        self.thread: threading.Thread | None = None
        self.dispositivos: list[dict] = []
        self.miniaturas: dict[str, object] = {}
        self.origem_selecionada = None
        self.resultado: motor.ResultadoRecuperacao | None = None
        self._tempo_inicio = 0.0

        self._montar()
        self._atualizar_dispositivos()
        self.root.after(100, self._processar_fila)

    # ---------------------------------------------------------------- layout

    def _cartao(self, pai, titulo):
        quadro = tk.Frame(pai, bg=COR_CARTAO, bd=0, highlightthickness=1,
                          highlightbackground="#dcdfe3")
        cab = tk.Label(quadro, text=titulo, bg=COR_CARTAO, fg=COR_TEXTO,
                       font=("Segoe UI", 10, "bold"), anchor="w", padx=10, pady=6)
        cab.pack(fill="x")
        corpo = tk.Frame(quadro, bg=COR_CARTAO, padx=10, pady=(0, 8))
        corpo.pack(fill="both", expand=True)
        return quadro, corpo

    def _montar(self):
        estilo = ttk.Style(self.root)
        try:
            estilo.theme_use("clam")
        except tk.TclError:
            pass
        estilo.configure("TNotebook", background=COR_FUNDO, borderwidth=0)
        estilo.configure("TNotebook.Tab", font=("Segoe UI", 9, "bold"), padding=(14, 6))
        estilo.configure("Azul.TButton", background=COR_AZUL, foreground="white",
                         font=("Segoe UI", 9, "bold"), borderwidth=0, padding=(12, 6))
        estilo.map("Azul.TButton",
                   background=[("active", COR_AZUL_ESCURO), ("disabled", "#9db9e4")])
        estilo.configure("TCheckbutton", background=COR_CARTAO, foreground=COR_TEXTO)
        estilo.configure("TRadiobutton", background=COR_CARTAO, foreground=COR_TEXTO)
        estilo.configure("TProgressbar", background=COR_AZUL, borderwidth=0,
                         troughcolor="#e3e6ea", thickness=14)

        principal = tk.Frame(self.root, bg=COR_FUNDO)
        principal.pack(fill="both", expand=True, padx=10, pady=8)
        principal.columnconfigure(0, weight=2, uniform="g")
        principal.columnconfigure(1, weight=3, uniform="g")
        principal.rowconfigure(0, weight=1)

        # ---------------- painel esquerdo
        esquerda = tk.Frame(principal, bg=COR_FUNDO)
        esquerda.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        canvas = tk.Canvas(esquerda, bg=COR_FUNDO, highlightthickness=0)
        scroll = ttk.Scrollbar(esquerda, orient="vertical", command=canvas.yview)
        self.coluna_esq = tk.Frame(canvas, bg=COR_FUNDO)
        self.coluna_esq.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.coluna_esq, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # ---- Seção 1: origem
        c1, corpo1 = self._cartao(self.coluna_esq, "1. Cartão, pendrive, HD ou pasta")
        c1.pack(fill="x", pady=(0, 8))

        linha_lista = tk.Frame(corpo1, bg=COR_CARTAO)
        linha_lista.pack(fill="x")
        self.lista_disp = tk.Listbox(linha_lista, height=5, bg="#fbfbfc",
                                     fg=COR_TEXTO, relief="solid", bd=1,
                                     selectbackground=COR_AZUL,
                                     exportselection=False)
        self.lista_disp.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(linha_lista, orient="vertical",
                           command=self.lista_disp.yview)
        self.lista_disp.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.lista_disp.bind("<<ListboxSelect>>", self._ao_selecionar_dispositivo)

        self.lbl_disco_sistema = tk.Label(corpo1, text="", bg=COR_CARTAO,
                                          fg="#b3261e", font=("Segoe UI", 8),
                                          anchor="w", justify="left")
        self.lbl_disco_sistema.pack(fill="x", pady=(2, 0))
        self.var_todos_discos = tk.BooleanVar(value=False)
        tk.Checkbutton(corpo1, text="Mostrar todos os discos",
                       variable=self.var_todos_discos, bg=COR_CARTAO,
                       fg=COR_TEXTO, selectcolor=COR_CARTAO,
                       activebackground=COR_CARTAO,
                       command=self._atualizar_dispositivos).pack(anchor="w")

        botoes1 = tk.Frame(corpo1, bg=COR_CARTAO)
        botoes1.pack(fill="x", pady=(6, 0))
        ttk.Button(botoes1, text="Atualizar lista",
                   command=self._atualizar_dispositivos).pack(side="left", padx=(0, 6))
        ttk.Button(botoes1, text="Arquivo de imagem…",
                   command=self._escolher_imagem).pack(side="left", padx=(0, 6))
        ttk.Button(botoes1, text="Escolher pasta de origem…",
                   command=self._escolher_pasta_origem).pack(side="left")
        self.lbl_origem = tk.Label(corpo1, text="Origem: (nenhuma)", bg=COR_CARTAO,
                                   fg="#444", font=("Segoe UI", 8), anchor="w",
                                   justify="left", wraplength=420)
        self.lbl_origem.pack(fill="x", pady=(6, 0))

        # ---- Seção 2: destino
        c2, corpo2 = self._cartao(self.coluna_esq, "2. Onde salvar")
        c2.pack(fill="x", pady=(0, 8))
        linha_dest = tk.Frame(corpo2, bg=COR_CARTAO)
        linha_dest.pack(fill="x")
        self.var_destino = tk.StringVar()
        tk.Entry(linha_dest, textvariable=self.var_destino, bg="#fbfbfc",
                 relief="solid", bd=1).pack(side="left", fill="x", expand=True,
                                            ipady=3)
        ttk.Button(linha_dest, text="Procurar…",
                   command=self._escolher_destino).pack(side="left", padx=(6, 0))
        self.lbl_destino_aviso = tk.Label(corpo2, text="", bg=COR_CARTAO,
                                          fg="#b3261e", font=("Segoe UI", 8),
                                          anchor="w", justify="left", wraplength=420)
        self.lbl_destino_aviso.pack(fill="x", pady=(2, 0))

        # ---- Seção 3: período
        c3, corpo3 = self._cartao(self.coluna_esq, "3. Período")
        c3.pack(fill="x", pady=(0, 8))
        self.var_periodo = tk.StringVar(value="todas")
        opcoes_periodo = [
            ("Todas as datas", "todas"),
            ("Hoje", "hoje"),
            ("Ontem e hoje", "ontem"),
            ("Últimos 7 dias", "7d"),
            ("Últimos 30 dias", "30d"),
        ]
        for texto, valor in opcoes_periodo:
            tk.Radiobutton(corpo3, text=texto, value=valor, bg=COR_CARTAO,
                           fg=COR_TEXTO, selectcolor=COR_CARTAO,
                           activebackground=COR_CARTAO,
                           variable=self.var_periodo).pack(anchor="w")
        linha_data = tk.Frame(corpo3, bg=COR_CARTAO)
        linha_data.pack(fill="x", anchor="w")
        tk.Radiobutton(linha_data, text="Data específica:", value="especifica",
                       bg=COR_CARTAO, fg=COR_TEXTO, selectcolor=COR_CARTAO,
                       activebackground=COR_CARTAO,
                       variable=self.var_periodo).pack(side="left")
        self.var_data = tk.StringVar(value=time.strftime("%d/%m/%Y"))
        tk.Entry(linha_data, textvariable=self.var_data, width=10, bg="#fbfbfc",
                 relief="solid", bd=1).pack(side="left", padx=4, ipady=1)
        tk.Label(linha_data, text="(DD/MM/AAAA)", bg=COR_CARTAO, fg="#777",
                 font=("Segoe UI", 8)).pack(side="left")
        self.var_sem_data = tk.BooleanVar(value=True)
        tk.Checkbutton(corpo3, text="Considerar arquivos sem data conhecida",
                       variable=self.var_sem_data, bg=COR_CARTAO, fg=COR_TEXTO,
                       selectcolor=COR_CARTAO,
                       activebackground=COR_CARTAO).pack(anchor="w")

        # ---- Seção 4: opções
        c4, corpo4 = self._cartao(self.coluna_esq, "4. Opções")
        c4.pack(fill="x", pady=(0, 8))
        self.var_videos = tk.BooleanVar(value=False)
        self.var_espaco_livre = tk.BooleanVar(value=True)
        self.var_verificar = tk.BooleanVar(value=True)
        self.var_profunda = tk.BooleanVar(value=False)
        self.var_pequenos = tk.BooleanVar(value=False)
        self.var_pausar = tk.BooleanVar(value=False)
        self.var_particoes = tk.BooleanVar(value=False)
        for texto, var in [
            ("Recuperar também vídeos", self.var_videos),
            ("Buscar assinaturas apenas no espaço livre", self.var_espaco_livre),
            ("Conferir as imagens e separar as corrompidas", self.var_verificar),
            ("Busca profunda", self.var_profunda),
            ("Incluir arquivos menores de 8 KB", self.var_pequenos),
            ("Pausar depois da primeira etapa", self.var_pausar),
            ("Buscar partições perdidas", self.var_particoes),
        ]:
            tk.Checkbutton(corpo4, text=texto, variable=var, bg=COR_CARTAO,
                           fg=COR_TEXTO, selectcolor=COR_CARTAO,
                           activebackground=COR_CARTAO).pack(anchor="w")

        botoes_acao = tk.Frame(self.coluna_esq, bg=COR_FUNDO)
        botoes_acao.pack(fill="x", pady=(2, 0))
        self.btn_iniciar = ttk.Button(botoes_acao, text="Iniciar recuperação",
                                      style="Azul.TButton", command=self._iniciar)
        self.btn_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=2)
        self.btn_cancelar = ttk.Button(botoes_acao, text="Cancelar",
                                       command=self._cancelar, state="disabled")
        self.btn_cancelar.pack(side="left", fill="x", expand=True, ipady=2)
        self.btn_continuar = ttk.Button(botoes_acao, text="Continuar (etapa 2)",
                                        command=self._continuar, state="disabled")
        self.btn_finalizar = ttk.Button(botoes_acao, text="Finalizar",
                                        command=self._finalizar, state="disabled")

        # ---------------- painel direito
        direita = tk.Frame(principal, bg=COR_FUNDO)
        direita.grid(row=0, column=1, sticky="nsew")
        self.abas = ttk.Notebook(direita)
        self.abas.pack(fill="both", expand=True)

        # ---- Aba 1: galeria
        aba_galeria = tk.Frame(self.abas, bg=COR_FUNDO)
        self.abas.add(aba_galeria, text="Fotos recuperadas")
        topo_gal = tk.Frame(aba_galeria, bg=COR_FUNDO)
        topo_gal.pack(fill="x", padx=6, pady=6)
        tk.Label(topo_gal, text="Filtrar por estado:", bg=COR_FUNDO,
                 fg=COR_TEXTO).pack(side="left")
        self.var_filtro = tk.StringVar(value="todos")
        for texto, valor in [("todos", "todos"), (ESTADO_OK, ESTADO_OK),
                             (ESTADO_NAO_VERIFICADO, ESTADO_NAO_VERIFICADO),
                             (ESTADO_CORROMPIDO, ESTADO_CORROMPIDO),
                             (ESTADO_PARCIAL, ESTADO_PARCIAL)]:
            tk.Radiobutton(topo_gal, text=texto, value=valor, bg=COR_FUNDO,
                           fg=COR_TEXTO, selectcolor=COR_FUNDO,
                           activebackground=COR_FUNDO, variable=self.var_filtro,
                           command=self._refiltrar_galeria).pack(side="left", padx=4)
        self.lbl_contadores = tk.Label(topo_gal, text="", bg=COR_FUNDO,
                                       fg="#444", font=("Segoe UI", 8))
        self.lbl_contadores.pack(side="right")

        corpo_gal = tk.Frame(aba_galeria, bg=COR_FUNDO)
        corpo_gal.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.canvas_gal = tk.Canvas(corpo_gal, bg=COR_FUNDO, highlightthickness=0)
        sb_gal = ttk.Scrollbar(corpo_gal, orient="vertical",
                               command=self.canvas_gal.yview)
        self.quadro_gal = tk.Frame(self.canvas_gal, bg=COR_FUNDO)
        self.quadro_gal.bind(
            "<Configure>",
            lambda e: self.canvas_gal.configure(scrollregion=self.canvas_gal.bbox("all")))
        self._janela_gal = self.canvas_gal.create_window((0, 0),
                                                         window=self.quadro_gal,
                                                         anchor="nw")
        self.canvas_gal.configure(yscrollcommand=sb_gal.set)
        self.canvas_gal.pack(side="left", fill="both", expand=True)
        sb_gal.pack(side="right", fill="y")
        self.canvas_gal.bind(
            "<Configure>",
            lambda e: self.canvas_gal.itemconfigure(self._janela_gal, width=e.width))

        # ---- Aba 2: registro
        aba_log = tk.Frame(self.abas, bg=COR_FUNDO)
        self.abas.add(aba_log, text="Registro")
        painel_info = tk.Frame(aba_log, bg=COR_CARTAO, padx=8, pady=6)
        painel_info.pack(fill="x", padx=6, pady=6)
        self.labels_info = {}
        for chave, rotulo in [
            ("dispositivo", "Dispositivo escolhido"),
            ("sistema", "Sistema de arquivos"),
            ("etapa", "Etapa em andamento"),
            ("recuperados", "Arquivos recuperados"),
            ("bytes", "Total de bytes recuperados"),
            ("erros", "Erros de leitura"),
            ("progresso", "Progresso"),
            ("tempo", "Estimativa de tempo restante"),
        ]:
            linha = tk.Frame(painel_info, bg=COR_CARTAO)
            linha.pack(fill="x")
            tk.Label(linha, text=rotulo + ":", bg=COR_CARTAO, fg="#555",
                     width=26, anchor="w", font=("Segoe UI", 9)).pack(side="left")
            lbl = tk.Label(linha, text="—", bg=COR_CARTAO, fg=COR_TEXTO,
                           anchor="w", font=("Segoe UI", 9, "bold"))
            lbl.pack(side="left", fill="x", expand=True)
            self.labels_info[chave] = lbl
        corpo_log = tk.Frame(aba_log, bg=COR_FUNDO)
        corpo_log.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.txt_log = tk.Text(corpo_log, bg="#14161a", fg="#d7dbe0",
                               insertbackground="#d7dbe0", relief="flat",
                               font=("Consolas", 9), state="disabled", wrap="word")
        sb_log = ttk.Scrollbar(corpo_log, orient="vertical",
                               command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb_log.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb_log.pack(side="right", fill="y")

        # ---------------- barra inferior
        inferior = tk.Frame(self.root, bg=COR_CARTAO, bd=0, highlightthickness=1,
                            highlightbackground="#dcdfe3")
        inferior.pack(fill="x", side="bottom", padx=10, pady=(0, 8))
        self.var_progresso = tk.DoubleVar(value=0.0)
        self.barra = ttk.Progressbar(inferior, variable=self.var_progresso,
                                     maximum=100.0, style="TProgressbar")
        self.barra.pack(fill="x", padx=10, pady=(8, 2))
        linha_status = tk.Frame(inferior, bg=COR_CARTAO)
        linha_status.pack(fill="x", padx=10, pady=(0, 8))
        self.lbl_status = tk.Label(linha_status, text="Pronto.", bg=COR_CARTAO,
                                   fg=COR_TEXTO, anchor="w", font=("Segoe UI", 9))
        self.lbl_status.pack(side="left", fill="x", expand=True)
        ttk.Button(linha_status, text="Abrir pasta de destino",
                   command=self._abrir_destino).pack(side="left", padx=4)
        ttk.Button(linha_status, text="Abrir diagnóstico NTFS",
                   command=self._abrir_diagnostico).pack(side="left")

    # ------------------------------------------------------------ dispositivos

    def _atualizar_dispositivos(self):
        self.lista_disp.delete(0, "end")
        self.lbl_disco_sistema.config(text="")
        try:
            self.dispositivos = aquisicao.listar_dispositivos(
                todos=self.var_todos_discos.get())
        except Exception as exc:
            self._log(f"AVISO: falha ao listar dispositivos: {exc}")
            self.dispositivos = []
        if not self.dispositivos:
            self.lista_disp.insert("end", " (nenhum dispositivo detectado — use "
                                          "'Arquivo de imagem…' ou pasta)")
        for d in self.dispositivos:
            letras = ",".join(d.get("unidades", [])) or "sem unidade"
            cap = formatar_bytes(d.get("capacidade", 0)) if d.get("capacidade") else "?"
            tags = []
            if d.get("removivel"):
                tags.append("removível")
            if d.get("disco_sistema"):
                tags.append("DISCO DO SISTEMA")
            sufixo = f" [{'; '.join(tags)}]" if tags else ""
            self.lista_disp.insert(
                "end",
                f"{d['nome']} — {d.get('modelo') or 'modelo desconhecido'} — "
                f"{cap} — {letras}{sufixo}")

    def _ao_selecionar_dispositivo(self, _evento=None):
        sel = self.lista_disp.curselection()
        if not sel or sel[0] >= len(self.dispositivos):
            return
        d = self.dispositivos[sel[0]]
        self.origem_selecionada = d["caminho"]
        if sys.platform != "win32" and d.get("unidades"):
            self.origem_selecionada = d["caminho"]
        self.lbl_origem.config(text=f"Origem: {self.origem_selecionada}"
                                    f" ({d.get('modelo') or '?'})")
        if d.get("disco_sistema"):
            self.lbl_disco_sistema.config(
                text="⚠ ATENÇÃO: este é o disco do sistema operacional. "
                     "A leitura é somente-leitura, mas evite recuperá-lo em uso.")
        else:
            self.lbl_disco_sistema.config(text="")

    def _escolher_imagem(self):
        tipos = [("Imagens de disco", "*.img *.dd *.raw *.iso *.bin *.e01"),
                 ("Todos os arquivos", "*.*")]
        caminho = filedialog.askopenfilename(title="Selecionar arquivo de imagem",
                                             filetypes=tipos)
        if caminho:
            self.origem_selecionada = caminho
            self.lbl_origem.config(text=f"Origem: arquivo de imagem {caminho}")
            self.lbl_disco_sistema.config(text="")

    def _escolher_pasta_origem(self):
        caminho = filedialog.askdirectory(title="Selecionar pasta de origem")
        if caminho:
            self.origem_selecionada = caminho
            self.lbl_origem.config(
                text=f"Origem: pasta {caminho} (recuperação por metadados do "
                     "volume físico)")
            self.lbl_disco_sistema.config(text="")

    def _escolher_destino(self):
        caminho = filedialog.askdirectory(title="Selecionar pasta de destino")
        if caminho:
            self.var_destino.set(caminho)
            self._validar_destino_gui()

    def _validar_destino_gui(self):
        destino = self.var_destino.get().strip()
        if not destino:
            self.lbl_destino_aviso.config(text="")
            return True
        origem = self.origem_selecionada
        if not origem:
            self.lbl_destino_aviso.config(
                text="Escolha a origem antes de validar o destino.")
            return False
        leitor = None
        try:
            leitor = aquisicao.abrir_origem(origem)
            res = aquisicao.validar_destino(leitor, destino, criar_se_nao_existe=False)
        except (OSError, ValueError) as exc:
            res = aquisicao.ResultadoValidacao(False, str(exc))
        finally:
            if isinstance(leitor, aquisicao.LeitorBase):
                try:
                    leitor.fechar()
                except Exception:
                    pass
            elif isinstance(leitor, aquisicao.OrigemPasta) and leitor.leitor:
                try:
                    leitor.leitor.fechar()
                except Exception:
                    pass
        if res.ok:
            texto = "Destino válido (outro disco físico)."
            if res.advertencias:
                texto += " Avisos: " + "; ".join(res.advertencias)
            self.lbl_destino_aviso.config(text=texto, fg="#1b7f37")
            return True
        self.lbl_destino_aviso.config(text=f"Destino bloqueado: {res.motivo}",
                                      fg="#b3261e")
        return False

    # ----------------------------------------------------------------- ações

    def _opcoes_da_gui(self) -> motor.Opcoes:
        periodo = self.var_periodo.get()
        desde = ""
        if periodo == "especifica":
            desde = self.var_data.get().strip()
        elif periodo != "todas":
            desde = periodo
        return motor.Opcoes(
            origem=self.origem_selecionada or "",
            destino=self.var_destino.get().strip(),
            videos=self.var_videos.get(),
            apenas_espaco_livre=self.var_espaco_livre.get(),
            verificar=self.var_verificar.get(),
            profunda=self.var_profunda.get(),
            incluir_pequenos=self.var_pequenos.get(),
            min_kb=0 if self.var_pequenos.get() else None,
            pausar_apos_etapa1=self.var_pausar.get(),
            buscar_particoes=self.var_particoes.get(),
            desde=desde,
            ignorar_sem_data=not self.var_sem_data.get(),
            sem_verificar=not self.var_verificar.get(),
            permitir_mesmo_disco=False,
        )

    def _iniciar(self):
        if self.thread and self.thread.is_alive():
            return
        if not self.origem_selecionada:
            messagebox.showwarning("Recupera Fotos 4.0",
                                   "Selecione a origem (dispositivo, imagem ou pasta).")
            return
        destino = self.var_destino.get().strip()
        if not destino:
            messagebox.showwarning("Recupera Fotos 4.0",
                                   "Escolha a pasta de destino em 'Onde salvar'.")
            return
        if not self._validar_destino_gui():
            messagebox.showerror(
                "Recupera Fotos 4.0",
                "O destino não passou na verificação de segurança.\n\n"
                + self.lbl_destino_aviso.cget("text"))
            return
        periodo = self.var_periodo.get()
        if periodo == "especifica":
            from datetime import datetime
            try:
                datetime.strptime(self.var_data.get().strip(), "%d/%m/%Y")
            except ValueError:
                messagebox.showerror("Recupera Fotos 4.0",
                                     "Data específica inválida. Use DD/MM/AAAA.")
                return

        self.cancelar.clear()
        self.continuar.clear()
        self.finalizar.clear()
        self._limpar_resultados()
        op = self._opcoes_da_gui()
        self.btn_iniciar.config(state="disabled")
        self.btn_cancelar.config(state="normal")
        self.lbl_status.config(text="Iniciando recuperação…")
        self._set_info("dispositivo", op.origem)
        self._tempo_inicio = time.time()

        self.thread = threading.Thread(
            target=self._trabalho, args=(op,), daemon=True)
        self.thread.start()

    def _trabalho(self, op: motor.Opcoes):
        m = motor.Motor(op, progresso=self._cb_progresso, log=self._cb_log,
                        cancelar=self.cancelar, continuar=self.continuar,
                        finalizar=self.finalizar)
        try:
            resultado = m.executar()
            self.fila.put(("fim", resultado))
        except PermissionError as exc:
            self.fila.put(("erro", f"Destino recusado: {exc}"))
        except Exception as exc:
            self.fila.put(("erro", f"{type(exc).__name__}: {exc}"))

    def _cb_progresso(self, fracao, total, fase):
        self.fila.put(("progresso", fracao, fase))

    def _cb_log(self, msg):
        self.fila.put(("log", msg))

    def _cancelar(self):
        self.cancelar.set()
        self.lbl_status.config(text="Cancelando com segurança…")

    def _continuar(self):
        self.continuar.set()

    def _finalizar(self):
        self.finalizar.set()

    # ------------------------------------------------------------ fila / GUI

    def _processar_fila(self):
        try:
            while True:
                item = self.fila.get_nowait()
                tipo = item[0]
                if tipo == "log":
                    self._log(item[1])
                    if item[1].startswith("RECUPERADO"):
                        self._atualizar_contadores_leves()
                    elif item[1].startswith("Pausado após a etapa 1"):
                        self.fila.put(("pausa",))
                elif tipo == "progresso":
                    _fr, fracao, fase = item
                    self.var_progresso.set(fracao * 100)
                    self._set_info("etapa", fase)
                    self._set_info("progresso", f"{fracao * 100:.1f}%")
                    decorrido = time.time() - self._tempo_inicio
                    if 0.02 < fracao < 1.0 and decorrido > 3:
                        restante = decorrido / fracao * (1 - fracao)
                        self._set_info("tempo", _formatar_duracao(restante))
                elif tipo == "pausa":
                    self.lbl_status.config(
                        text="Pausado após a etapa 1 — escolha Continuar ou Finalizar.")
                    self.btn_continuar.pack(side="left", fill="x", expand=True,
                                            padx=(6, 0), ipady=2)
                    self.btn_finalizar.pack(side="left", fill="x", expand=True,
                                            padx=(6, 0), ipady=2)
                    self.btn_continuar.config(state="normal")
                    self.btn_finalizar.config(state="normal")
                elif tipo == "fim":
                    self._finalizar_trabalho(item[1], erro=None)
                elif tipo == "erro":
                    self._finalizar_trabalho(None, erro=item[1])
        except queue.Empty:
            pass
        self.root.after(100, self._processar_fila)

    def _finalizar_trabalho(self, resultado, erro):
        self.btn_iniciar.config(state="normal")
        self.btn_cancelar.config(state="disabled")
        self.btn_continuar.config(state="disabled", )
        self.btn_finalizar.config(state="disabled")
        self.btn_continuar.pack_forget()
        self.btn_finalizar.pack_forget()
        if erro:
            self.lbl_status.config(text=f"Erro: {erro}")
            messagebox.showerror("Recupera Fotos 4.0", erro)
            return
        self.resultado = resultado
        self.var_progresso.set(100)
        self._set_info("etapa", "concluído")
        self._set_info("tempo", "—")
        if resultado.cancelado:
            estado_txt = "cancelada"
        elif getattr(resultado, "pausado", False):
            # o usuário escolheu "Finalizar" na pausa: a etapa 2 não rodou
            estado_txt = "finalizada após a etapa 1 (varredura não executada)"
        else:
            estado_txt = "concluída"
        self.lbl_status.config(
            text=f"Recuperação {estado_txt}: {resultado.recuperados} arquivo(s), "
                 f"{formatar_bytes(resultado.bytes_recuperados)} "
                 f"({resultado.tempo_segundos:.1f}s). "
                 f"Duplicatas evitadas: {resultado.duplicados_hash + resultado.duplicados_regiao}.")
        for a in resultado.avisos:
            self._log("AVISO: " + a)
        self._atualizar_info(resultado)
        self._reconstruir_galeria()

    def _atualizar_contadores_leves(self):
        if self.resultado:
            self._atualizar_info(self.resultado)

    def _atualizar_info(self, res: motor.ResultadoRecuperacao):
        self._set_info("sistema", ", ".join(res.sistemas) or "—")
        self._set_info("recuperados", str(res.recuperados))
        self._set_info("bytes", formatar_bytes(res.bytes_recuperados))
        self._set_info("erros", str(res.erros_leitura))
        pe = res.por_estado
        self.lbl_contadores.config(
            text=f"ok: {pe.get(ESTADO_OK, 0)}  |  nao_verificado: "
                 f"{pe.get(ESTADO_NAO_VERIFICADO, 0)}  |  corrompido: "
                 f"{pe.get(ESTADO_CORROMPIDO, 0)}  |  parcial: "
                 f"{pe.get(ESTADO_PARCIAL, 0)}")

    def _set_info(self, chave, valor):
        if chave in self.labels_info:
            self.labels_info[chave].config(text=str(valor))

    def _log(self, msg: str):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        # limita o texto em memória
        linhas = int(self.txt_log.index("end-1c").split(".")[0])
        if linhas > 4000:
            self.txt_log.delete("1.0", f"{linhas - 3000}.0")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    # ---------------------------------------------------------------- galeria

    def _limpar_resultados(self):
        for w in self.quadro_gal.winfo_children():
            w.destroy()
        self.miniaturas.clear()
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")
        self.lbl_contadores.config(text="")
        self.var_progresso.set(0)

    def _reconstruir_galeria(self):
        for w in self.quadro_gal.winfo_children():
            w.destroy()
        self.miniaturas.clear()
        if not self.resultado:
            return
        filtro = self.var_filtro.get()
        cands = [c for c in self.resultado.candidatos
                 if filtro == "todos" or c.estado == filtro]
        # limita miniaturas mantidas em memória
        cands = cands[:LIMITE_MINIATURAS]
        colunas = 4
        for idx, cand in enumerate(cands):
            linha = idx // colunas
            col = idx % colunas
            celula = tk.Frame(self.quadro_gal, bg=COR_CARTAO, bd=1,
                              relief="solid", padx=4, pady=4)
            celula.grid(row=linha, column=col, padx=4, pady=4, sticky="n")
            img_ref = self._miniatura(cand)
            if img_ref is not None:
                lbl_img = tk.Label(celula, image=img_ref, bg=COR_CARTAO,
                                   cursor="hand2")
                lbl_img.pack()
                lbl_img.image = img_ref
                self.miniaturas[cand.arquivo_destino or cand.nome] = img_ref
                lbl_img.bind("<Button-1>",
                             lambda e, c=cand: self._abrir_ampliada(c))
            else:
                tk.Label(celula, text="(sem miniatura)", width=18, height=7,
                         bg="#e9ebee", fg="#777").pack()
            nome_txt = os.path.basename(cand.arquivo_destino or cand.nome)
            if len(nome_txt) > 24:
                nome_txt = nome_txt[:21] + "…"
            cor_estado = {ESTADO_OK: "#1b7f37", ESTADO_NAO_VERIFICADO: "#8a6d1a",
                          ESTADO_CORROMPIDO: "#b3261e", ESTADO_PARCIAL: "#7a4bb3"
                          }.get(cand.estado, "#444")
            tk.Label(celula, text=nome_txt, bg=COR_CARTAO, fg=COR_TEXTO,
                     font=("Segoe UI", 8), width=24).pack()
            tk.Label(celula, text=f"{cand.estado} • "
                                  f"{formatar_bytes(cand.tamanho or 0)}",
                     bg=COR_CARTAO, fg=cor_estado, font=("Segoe UI", 8, "bold"),
                     width=24).pack()

    def _miniatura(self, cand):
        caminho = cand.arquivo_destino
        if not caminho or not os.path.exists(caminho) or not PIL_OK:
            return None
        ext = cand.extensao
        if ext not in ("jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff"):
            return None
        try:
            img = PILImage.open(caminho)
            img.thumbnail((140, 140))
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _refiltrar_galeria(self):
        self._reconstruir_galeria()

    def _abrir_ampliada(self, cand):
        caminho = cand.arquivo_destino
        if not caminho or not os.path.exists(caminho):
            return
        if not PIL_OK:
            self._abrir_com_sistema(caminho)
            return
        jan = tk.Toplevel(self.root)
        jan.title(os.path.basename(caminho))
        jan.configure(bg="#111")
        try:
            img = PILImage.open(caminho)
            img.thumbnail((900, 700))
            foto = ImageTk.PhotoImage(img)
            lbl = tk.Label(jan, image=foto, bg="#111")
            lbl.image = foto
            lbl.pack(padx=10, pady=10)
        except Exception as exc:
            tk.Label(jan, text=f"Não foi possível exibir: {exc}", bg="#111",
                     fg="#eee").pack(padx=20, pady=20)
        info = (f"estado: {cand.estado} • método: {cand.metodo} • "
                f"{formatar_bytes(cand.tamanho or 0)}")
        if cand.caminho_original:
            info += f"\ncaminho original: {cand.caminho_original}"
        if cand.motivo:
            info += f"\n{cand.motivo}"
        tk.Label(jan, text=info, bg="#111", fg="#ccc", justify="left",
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=12, pady=(0, 10))

    # ------------------------------------------------------------ utilidades

    def _abrir_destino(self):
        destino = self.var_destino.get().strip()
        if not destino or not os.path.isdir(destino):
            messagebox.showinfo("Recupera Fotos 4.0",
                                "Nenhum destino válido para abrir.")
            return
        self._abrir_com_sistema(destino)

    def _abrir_diagnostico(self):
        if self.resultado and self.resultado.diagnostico_csv and \
                os.path.exists(self.resultado.diagnostico_csv):
            self._abrir_com_sistema(self.resultado.diagnostico_csv)
        else:
            messagebox.showinfo(
                "Recupera Fotos 4.0",
                "Nenhum diagnóstico NTFS foi gerado nesta recuperação.\n"
                "Ele é criado quando há problemas com registros NTFS.")

    @staticmethod
    def _abrir_com_sistema(caminho: str):
        try:
            if sys.platform == "win32":
                os.startfile(caminho)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", caminho])
            else:
                subprocess.Popen(["xdg-open", caminho])
        except Exception as exc:
            messagebox.showwarning("Recupera Fotos 4.0",
                                   f"Não foi possível abrir: {exc}")


def _formatar_duracao(segundos: float) -> str:
    segundos = int(max(0, segundos))
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h}h{m:02d}min"
    if m:
        return f"{m}min{s:02d}s"
    return f"{s}s"


def executar_gui():
    root = tk.Tk()
    Aplicativo(root)
    root.mainloop()
