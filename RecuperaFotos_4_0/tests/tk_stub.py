"""
tk_stub.py — Tkinter headless para testes (sem servidor gráfico).

Não é um emulador de Tk: é um dublê de teste. Ele registra tudo o que a
interface faz (widgets criados, textos definidos, botões habilitados,
mensagens e diálogos) e devolve o controle para o teste, que pode:

  * disparar eventos/cliques        -> widget.disparar(seq) / widget.invoke()
  * ler o que foi mostrado          -> widget.cget("text"), texto_do_log()
  * executar os callbacks agendados -> drenar_after()
  * responder diálogos              -> RESPOSTAS_ARQUIVO / RESPOSTAS_PASTA
  * conferir avisos                 -> MENSAGENS

Com isso dá para testar a lógica real de interface.py (validações, mapeamento
de opções, fila thread-safe, pausa/cancelamento, galeria, limites de memória)
em qualquer sistema, inclusive sem X11 e sem o pacote python3-tk.
"""

from __future__ import annotations

import types

# ------------------------------------------------------------------ registros
REGISTRO: list[Widget] = []          # todos os widgets criados
FILA_AFTER: list[tuple] = []           # callbacks agendados com after()
MENSAGENS: list[tuple] = []            # (tipo, titulo, texto) de messagebox
RESPOSTAS_ARQUIVO: list[str] = []      # fila de respostas do askopenfilename
RESPOSTAS_PASTA: list[str] = []        # fila de respostas do askdirectory
RESPOSTAS_SALVAR: list[str] = []       # fila de respostas do asksaveasfilename
RESPOSTAS_CAIXA: list[bool] = []       # respostas de askyesno/askokcancel
CHAMADAS_MAINLOOP: list[int] = []      # quantas vezes mainloop() foi chamado
CHAMADAS_SISTEMA: list[list] = []      # subprocess.Popen (abrir com o sistema)


def limpar() -> None:
    """Zera todos os registros (chamado no setUp dos testes)."""
    REGISTRO.clear()
    FILA_AFTER.clear()
    MENSAGENS.clear()
    RESPOSTAS_ARQUIVO.clear()
    RESPOSTAS_PASTA.clear()
    RESPOSTAS_SALVAR.clear()
    RESPOSTAS_CAIXA.clear()
    CHAMADAS_MAINLOOP.clear()
    CHAMADAS_SISTEMA.clear()


def drenar_after(maximo: int = 200) -> int:
    """Executa os callbacks agendados por after(); retorna quantos rodaram.

    A interface se reagenda a cada volta (polling da fila), então o limite
    existe para o teste não entrar em laço infinito.
    """
    n = 0
    while FILA_AFTER and n < maximo:
        _ms, func, args = FILA_AFTER.pop(0)
        if func is None:
            continue
        func(*args)
        n += 1
    return n


def widgets_de(classe=None, **com_config):
    """Busca widgets criados, opcionalmente filtrando por classe e opções."""
    saida = []
    for w in REGISTRO:
        if classe is not None and not isinstance(w, classe):
            continue
        if any(w.configuracoes.get(k) != v for k, v in com_config.items()):
            continue
        saida.append(w)
    return saida


def por_texto(texto: str):
    """Widgets cujo 'text' (ou conteúdo) contém o texto informado."""
    achados = []
    for w in REGISTRO:
        t = w.configuracoes.get("text")
        if isinstance(t, str) and texto in t:
            achados.append(w)
    return achados


class Evento:
    """Evento sintético mínimo (width/height/x/y/widget)."""

    def __init__(self, widget=None, **kw):
        self.widget = widget
        self.width = kw.pop("width", 800)
        self.height = kw.pop("height", 600)
        self.x = kw.pop("x", 0)
        self.y = kw.pop("y", 0)
        self.x_root = self.x
        self.y_root = self.y
        self.state = 0
        self.keysym = ""
        self.__dict__.update(kw)


class TclError(Exception):
    pass


# ------------------------------------------------------------------- variáveis
class _Var:
    PADRAO = None

    def __init__(self, master=None, value=None, name=None):
        self._valor = self.PADRAO if value is None else value
        self.name = name or f"var{id(self)}"

    def get(self):
        return self._valor

    def set(self, valor):
        self._valor = valor

    def trace_add(self, *a, **k):
        return ""

    def trace_remove(self, *a, **k):
        pass

    def __str__(self):
        return str(self._valor)


class StringVar(_Var):
    PADRAO = ""

    def get(self):
        return "" if self._valor is None else str(self._valor)


class BooleanVar(_Var):
    PADRAO = False

    def get(self):
        return bool(self._valor)

    def set(self, valor):
        self._valor = bool(valor)


class IntVar(_Var):
    PADRAO = 0

    def get(self):
        return int(self._valor)


class DoubleVar(_Var):
    PADRAO = 0.0

    def get(self):
        return float(self._valor)


# --------------------------------------------------------------------- widgets
class Widget:
    _seq = 0

    def __init__(self, master=None, **kw):
        Widget._seq += 1
        self.id = Widget._seq
        self.master = master
        self.configuracoes = dict(kw)
        self.filhos: list[Widget] = []
        self.eventos: dict[str, list] = {}
        self.destruido = False
        self.empacotado = None
        self.grade = None
        self.tk = _Interp()
        if isinstance(master, Widget):
            master.filhos.append(self)
        REGISTRO.append(self)

    # ------------------------------------------------------------ layout
    def pack(self, **kw):
        self.empacotado = kw or {}
        return self

    def pack_forget(self):
        self.empacotado = None

    def pack_info(self):
        return dict(self.empacotado or {})

    def grid(self, **kw):
        self.grade = kw
        return self

    def grid_forget(self):
        self.grade = None

    def place(self, **kw):
        return self

    def columnconfigure(self, *a, **k):
        pass

    def rowconfigure(self, *a, **k):
        pass

    # ------------------------------------------------------- configuração
    def config(self, cnf=None, **kw):
        return self.configure(cnf, **kw)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw.update(cnf)
        elif isinstance(cnf, str):
            return self.configuracoes.get(cnf)
        self.configuracoes.update(kw)
        return None

    def cget(self, chave):
        return self.configuracoes.get(chave)

    def __getitem__(self, chave):
        return self.configuracoes.get(chave)

    def __setitem__(self, chave, valor):
        self.configuracoes[chave] = valor

    def keys(self):
        return list(self.configuracoes)

    def state(self, *a):
        return "normal"

    # ----------------------------------------------------------- eventos
    def bind(self, seq, func=None, add=None):
        self.eventos.setdefault(seq, []).append(func)
        return f"bind{len(self.eventos)}"

    def unbind(self, seq, *a):
        self.eventos.pop(seq, None)

    def bind_all(self, seq, func=None, add=None):
        return self.bind(seq, func, add)

    def unbind_all(self, seq):
        self.unbind(seq)

    def disparar(self, seq, evento=None):
        """Helper de teste: dispara os callbacks registrados para seq."""
        ev = evento or Evento(self)
        for fn in list(self.eventos.get(seq, [])):
            if fn is not None:
                fn(ev)
        return ev

    def event_generate(self, seq, **kw):
        self.disparar(seq, Evento(self, **kw))

    # ------------------------------------------------------------ diversos
    def destroy(self):
        self.destruido = True
        for f in self.filhos:
            f.destroy()

    def winfo_children(self):
        return [f for f in self.filhos if not f.destruido]

    def winfo_exists(self):
        return not self.destruido

    def winfo_width(self):
        return 800

    def winfo_height(self):
        return 600

    def winfo_toplevel(self):
        w = self
        while isinstance(w.master, Widget):
            w = w.master
        return w

    def after(self, ms, func=None, *args):
        FILA_AFTER.append((ms, func, args))
        return len(FILA_AFTER)

    def after_idle(self, func=None, *args):
        return self.after(0, func, *args)

    def after_cancel(self, ident):
        pass

    def update(self):
        drenar_after(maximo=1)

    def update_idletasks(self):
        pass

    def focus_set(self):
        pass

    def focus_force(self):
        pass

    def yview(self, *a):
        pass

    def xview(self, *a):
        pass

    def lift(self):
        pass

    def __str__(self):
        return f".stub{self.id}"


class _Interp:
    """'tk' mínimo (PIL.ImageTk só precisa de .call)."""

    def call(self, *args, **kw):
        return ""

    def getboolean(self, v):
        return bool(v)


class Misc(Widget):
    pass


class Tk(Widget):
    def title(self, t=None):
        if t is not None:
            self.configuracoes["title"] = t
        return self.configuracoes.get("title", "")

    wm_title = title

    def geometry(self, g=None):
        if g is not None:
            self.configuracoes["geometry"] = g
        return self.configuracoes.get("geometry", "")

    def minsize(self, w=None, h=None):
        if w is None and h is None:
            return self.configuracoes.get("minsize", (1, 1))
        self.configuracoes["minsize"] = (w, h)
        return None

    def maxsize(self, w=None, h=None):
        if w is None and h is None:
            return self.configuracoes.get("maxsize", (0, 0))
        self.configuracoes["maxsize"] = (w, h)
        return None

    def resizable(self, w=None, h=None):
        if w is None and h is None:
            return self.configuracoes.get("resizable", (True, True))
        self.configuracoes["resizable"] = (w, h)
        return None

    def protocol(self, nome=None, func=None):
        self.configuracoes.setdefault("protocolos", {})[nome] = func

    def attributes(self, *a):
        return None

    def iconbitmap(self, *a):
        pass

    def wm_state(self, *a):
        return "normal"

    def mainloop(self, n=0):
        CHAMADAS_MAINLOOP.append(n)

    def quit(self):
        pass

    def wait_window(self, *a):
        pass


class Toplevel(Widget):
    def title(self, t=None):
        if t is not None:
            self.configuracoes["title"] = t
        return self.configuracoes.get("title", "")

    def geometry(self, g=None):
        if g is not None:
            self.configuracoes["geometry"] = g
        return self.configuracoes.get("geometry", "")

    def resizable(self, w=None, h=None):
        pass

    def protocol(self, nome=None, func=None):
        pass

    def attributes(self, *a):
        return None

    def transient(self, *a):
        pass

    def grab_set(self):
        pass

    def wait_window(self, *a):
        pass


class Frame(Widget):
    pass


class Label(Widget):
    pass


class Button(Widget):
    def invoke(self):
        cmd = self.configuracoes.get("command")
        if callable(cmd):
            return cmd()
        return None

    def flash(self):
        pass


class Checkbutton(Button):
    def invoke(self):
        var = self.configuracoes.get("variable")
        if var is not None:
            var.set(not var.get())
        return super().invoke()


class Radiobutton(Button):
    def invoke(self):
        var = self.configuracoes.get("variable")
        if var is not None:
            var.set(self.configuracoes.get("value"))
        return super().invoke()


class Entry(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._texto = ""

    def get(self):
        var = self.configuracoes.get("textvariable")
        if var is not None:
            return var.get()
        return self._texto

    def insert(self, indice, texto):
        var = self.configuracoes.get("textvariable")
        if var is not None:
            var.set(var.get() + texto)
        else:
            self._texto += texto

    def delete(self, primeiro, ultimo=None):
        var = self.configuracoes.get("textvariable")
        if var is not None:
            var.set("")
        else:
            self._texto = ""

    def selection_range(self, *a):
        pass

    def icursor(self, *a):
        pass


class Text(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.conteudo = ""

    def insert(self, indice, texto, *tags):
        self.conteudo += texto

    def delete(self, primeiro, ultimo=None):
        if primeiro == "1.0" and ultimo in ("end", "end-1c", None):
            self.conteudo = ""
            return
        if primeiro == "1.0" and isinstance(ultimo, str) and ultimo.endswith(".0"):
            try:
                n = int(ultimo.split(".")[0])
            except ValueError:
                return
            linhas = self.conteudo.split("\n")
            self.conteudo = "\n".join(linhas[max(0, n - 1):])
            return
        self.conteudo = ""

    def get(self, primeiro="1.0", ultimo="end-1c"):
        return self.conteudo

    def index(self, indice):
        if indice in ("end", "end-1c"):
            return f"{self.conteudo.count(chr(10)) + 1}.0"
        return indice

    def see(self, indice):
        pass

    def scan_mark(self, *a):
        pass

    def yview_moveto(self, *a):
        pass


class Listbox(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.itens: list[str] = []
        self.selecao: tuple = ()

    def insert(self, indice, *itens):
        if indice == "end":
            self.itens.extend(itens)
        else:
            for k, it in enumerate(itens):
                self.itens.insert(int(indice) + k, it)

    def delete(self, primeiro, ultimo=None):
        if primeiro == 0 and ultimo in ("end", None):
            self.itens.clear()
            self.selecao = ()
            return
        try:
            del self.itens[int(primeiro):(int(ultimo) + 1 if ultimo not in
                                            (None, "end") else None)]
        except (TypeError, ValueError):
            pass

    def get(self, primeiro=None, ultimo=None):
        if primeiro is None:
            return tuple(self.itens)
        if ultimo is None:
            return self.itens[int(primeiro)]
        return tuple(self.itens[int(primeiro):int(ultimo) + 1])

    def size(self):
        return len(self.itens)

    def curselection(self):
        return self.selecao

    def selection_set(self, primeiro, ultimo=None):
        self.selecao = (int(primeiro),)

    def selection_clear(self, *a):
        self.selecao = ()

    def see(self, indice):
        pass

    def ativar_selecao(self, indice: int):
        """Helper de teste: seleciona e dispara <<ListboxSelect>>."""
        self.selecao = (indice,)
        self.disparar("<<ListboxSelect>>")


class Canvas(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._itens = 0

    def _novo_item(self, tipo, *a, **k):
        self._itens += 1
        return self._itens

    def create_window(self, *a, **k):
        return self._novo_item("window", *a, **k)

    def create_image(self, *a, **k):
        return self._novo_item("image", *a, **k)

    def create_rectangle(self, *a, **k):
        return self._novo_item("rect", *a, **k)

    def create_line(self, *a, **k):
        return self._novo_item("line", *a, **k)

    def create_text(self, *a, **k):
        return self._novo_item("text", *a, **k)

    def bbox(self, *a):
        return (0, 0, 800, 600)

    def itemconfigure(self, item=None, **kw):
        pass

    def itemconfig(self, item=None, **kw):
        pass

    def coords(self, *a):
        return (0, 0)

    def delete(self, *a):
        pass

    def yview_scroll(self, *a):
        pass

    def scan_mark(self, *a):
        pass

    def scan_dragto(self, *a):
        pass


class Scrollbar(Widget):
    def set(self, *a):
        pass


class Menu(Widget):
    def add_command(self, **kw):
        pass

    def add_separator(self, **kw):
        pass

    def add_cascade(self, **kw):
        pass


class PhotoImage:
    """PhotoImage do stub: guarda a imagem (PIL ou bytes) sem Tk real."""

    def __init__(self, image=None, size=None, master=None, data=None, **kw):
        self.image = image
        self.size = size or (getattr(image, "width", lambda: 1)(),
                             getattr(image, "height", lambda: 1)())
        self.master = master
        self.name = f"photo{id(self)}"
        self.tk = _Interp()
        self.configuracoes = dict(kw)

    def width(self):
        return self.size[0]

    def height(self):
        return self.size[1]

    def put(self, data, to=None):
        pass

    def write(self, filename, **kw):
        pass

    def configure(self, **kw):
        self.configuracoes.update(kw)

    config = configure

    def cget(self, k):
        return self.configuracoes.get(k)

    def __str__(self):
        return self.name


class BitmapImage(PhotoImage):
    pass


# constantes usadas por convenção
for _nome, _valor in (("END", "end"), ("INSERT", "insert"), ("SEL", "sel"),
                      ("VERTICAL", "vertical"), ("HORIZONTAL", "horizontal"),
                      ("BOTH", "both"), ("X", "x"), ("Y", "y"), ("LEFT", "left"),
                      ("RIGHT", "right"), ("TOP", "top"), ("BOTTOM", "bottom"),
                      ("CENTER", "center"), ("N", "n"), ("S", "s"), ("E", "e"),
                      ("W", "w"), ("NE", "ne"), ("NW", "nw"), ("SE", "se"),
                      ("SW", "sw"), ("YES", True), ("NO", False),
                      ("NORMAL", "normal"), ("DISABLED", "disabled"),
                      ("ACTIVE", "active"), ("WORD", "word"), ("CHAR", "char"),
                      ("NONE", "none"), ("SINGLE", "single"),
                      ("BROWSE", "browse"), ("EXTENDED", "extended"),
                      ("RAISED", "raised"), ("SUNKEN", "sunken"),
                      ("FLAT", "flat"), ("SOLID", "solid"), ("GROOVE", "groove"),
                      ("RIDGE", "ridge"), ("TRUE", True), ("FALSE", False)):
    globals()[_nome] = _valor


def _construir_ttk() -> types.ModuleType:
    m = types.ModuleType("tkinter.ttk")

    class Style:
        def __init__(self, master=None):
            self.master = master
            self._tema = "clam"
            self._estilos: dict = {}

        def theme_use(self, nome=None):
            if nome is not None:
                self._tema = nome
            return self._tema

        def theme_names(self):
            return ("clam", "default", "alt", "classic", "vista", "winnative", "xpnative")

        def configure(self, estilo=None, **kw):
            self._estilos.setdefault(estilo, {}).update(kw)

        def map(self, estilo=None, **kw):
            self._estilos.setdefault(estilo, {}).setdefault("_map", {}).update(kw)

        def layout(self, estilo=None, *a, **k):
            return []

        def lookup(self, estilo=None, opcao=None, *a, **k):
            return self._estilos.get(estilo, {}).get(opcao)

    class TWidget(Widget):
        pass

    class TButton(Button, TWidget):
        pass

    class TCheckbutton(Checkbutton, TWidget):
        pass

    class TRadiobutton(Radiobutton, TWidget):
        pass

    class TFrame(Frame, TWidget):
        pass

    class TLabel(Label, TWidget):
        pass

    class TEntry(Entry, TWidget):
        pass

    class TScrollbar(Scrollbar, TWidget):
        pass

    class TSeparator(TWidget):
        pass

    class TCombobox(TEntry):
        def __init__(self, master=None, **kw):
            super().__init__(master, **kw)
            self._valores = kw.get("values", ())

        def current(self, i=None):
            return 0

    class Progressbar(TWidget):
        def start(self, *a, **k):
            self.configuracoes["rodando"] = True

        def stop(self):
            self.configuracoes["rodando"] = False

        def step(self, *a):
            pass

    class Notebook(TWidget):
        def __init__(self, master=None, **kw):
            super().__init__(master, **kw)
            self.abas: list[tuple] = []

        def add(self, widget, **kw):
            self.abas.append((widget, kw))

        def select(self, indice=None):
            if indice is None:
                return 0
            return None

        def tab(self, indice, **kw):
            return self.abas[int(indice)][1] if self.abas else {}

        def index(self, *a):
            return 0

        def forget(self, *a):
            pass

    class LabelFrame(Frame, TWidget):
        pass

    class PanedWindow(TWidget):
        def add(self, *a, **k):
            pass

    class Treeview(TWidget):
        def __init__(self, master=None, **kw):
            super().__init__(master, **kw)
            self.itens: list = []

        def insert(self, *a, **k):
            self.itens.append((a, k))
            return f"I{len(self.itens)}"

        def delete(self, *a):
            self.itens.clear()

        def set(self, *a, **k):
            pass

        def selection(self, *a):
            return ()

    m.Style = Style
    m.Button = TButton
    m.Checkbutton = TCheckbutton
    m.Radiobutton = TRadiobutton
    m.Frame = TFrame
    m.Label = TLabel
    m.Entry = TEntry
    m.Scrollbar = TScrollbar
    m.Separator = TSeparator
    m.Combobox = TCombobox
    m.Progressbar = Progressbar
    m.Notebook = Notebook
    m.LabelFrame = LabelFrame
    m.PanedWindow = PanedWindow
    m.Treeview = Treeview
    m.Widget = TWidget
    return m


def _construir_filedialog() -> types.ModuleType:
    m = types.ModuleType("tkinter.filedialog")

    def askopenfilename(**kw):
        m.ULTIMA_CHAMADA = ("askopenfilename", kw)
        return RESPOSTAS_ARQUIVO.pop(0) if RESPOSTAS_ARQUIVO else ""

    def askdirectory(**kw):
        m.ULTIMA_CHAMADA = ("askdirectory", kw)
        return RESPOSTAS_PASTA.pop(0) if RESPOSTAS_PASTA else ""

    def asksaveasfilename(**kw):
        m.ULTIMA_CHAMADA = ("asksaveasfilename", kw)
        return RESPOSTAS_SALVAR.pop(0) if RESPOSTAS_SALVAR else ""

    m.askopenfilename = askopenfilename
    m.askdirectory = askdirectory
    m.asksaveasfilename = asksaveasfilename
    m.ULTIMA_CHAMADA = None
    return m


def _construir_messagebox() -> types.ModuleType:
    m = types.ModuleType("tkinter.messagebox")

    def _registrar(tipo):
        def fn(titulo, mensagem, **kw):
            MENSAGENS.append((tipo, titulo, mensagem))
            if tipo in ("askyesno", "askokcancel"):
                return RESPOSTAS_CAIXA.pop(0) if RESPOSTAS_CAIXA else True
            return True
        return fn

    for tipo in ("showinfo", "showwarning", "showerror", "askquestion",
                 "askokcancel", "askyesno", "askyesnocancel", "askretrycancel"):
        setattr(m, tipo, _registrar(tipo))
    return m


def _construir_pil_imagetk() -> types.ModuleType:
    """PIL.ImageTk sem o C _imagingtk: só guarda a referência da imagem."""
    m = types.ModuleType("PIL.ImageTk")

    class PhotoImage:
        def __init__(self, image=None, size=None, master=None):
            self.image = image
            if image is not None and hasattr(image, "size"):
                self._tam = image.size
            else:
                self._tam = size or (1, 1)
            self.name = f"pilphoto{id(self)}"

        def width(self):
            return self._tam[0]

        def height(self):
            return self._tam[1]

        def paste(self, im, *a):
            pass

        def __str__(self):
            return self.name

    class BitmapImage(PhotoImage):
        pass

    m.PhotoImage = PhotoImage
    m.BitmapImage = BitmapImage
    return m


def construir_tkinter() -> types.ModuleType:
    """Módulo 'tkinter' falso com tudo o que interface.py usa."""
    m = types.ModuleType("tkinter")
    for nome in ("Widget", "Misc", "Tk", "Toplevel", "Frame", "Label", "Button",
                 "Checkbutton", "Radiobutton", "Entry", "Text", "Listbox",
                 "Canvas", "Scrollbar", "Menu", "PhotoImage", "BitmapImage",
                 "TclError", "Evento", "StringVar", "BooleanVar", "IntVar",
                 "DoubleVar"):
        setattr(m, nome, globals()[nome])
    m.Variable = _Var
    m.__version__ = "8.6 (stub de teste)"
    m.TkVersion = 8.6
    m.TclVersion = 8.6
    m.mainloop = lambda *a: CHAMADAS_MAINLOOP.append(0)
    return m


def instalar() -> dict:
    """Coloca os stubs em sys.modules e devolve o mapa para restaurar."""
    import sys
    tk_mod = construir_tkinter()
    ttk_mod = _construir_ttk()
    fd_mod = _construir_filedialog()
    mb_mod = _construir_messagebox()
    pil_mod = _construir_pil_imagetk()
    tk_mod.ttk = ttk_mod
    tk_mod.filedialog = fd_mod
    tk_mod.messagebox = mb_mod

    salvos = {}
    for nome, mod in (("tkinter", tk_mod), ("tkinter.ttk", ttk_mod),
                      ("tkinter.filedialog", fd_mod),
                      ("tkinter.messagebox", mb_mod),
                      ("PIL.ImageTk", pil_mod)):
        salvos[nome] = sys.modules.get(nome)
        sys.modules[nome] = mod
    import PIL
    salvos["_PIL_attr"] = getattr(PIL, "ImageTk", None)
    PIL.ImageTk = pil_mod
    return salvos


def desinstalar(salvos: dict) -> None:
    import sys
    for nome in ("tkinter", "tkinter.ttk", "tkinter.filedialog",
                 "tkinter.messagebox", "PIL.ImageTk"):
        anterior = salvos.get(nome)
        if anterior is None:
            sys.modules.pop(nome, None)
        else:
            sys.modules[nome] = anterior
    import PIL
    if salvos.get("_PIL_attr") is not None:
        PIL.ImageTk = salvos["_PIL_attr"]
    elif hasattr(PIL, "ImageTk"):
        delattr(PIL, "ImageTk")
