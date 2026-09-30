"""Interface gráfica (Tkinter): fila de arquivos, preview, progresso e cancelamento."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import core
from . import video_io as vio
from .backends import BACKENDS


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Video Upscale")
        root.geometry("760x520")
        self.cancel = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.files: list[Path] = []

        top = ttk.Frame(root, padding=10)
        top.pack(fill="both", expand=True)

        ttk.Button(top, text="Adicionar vídeos", command=self.add_files).grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="Adicionar pasta", command=self.add_folder).grid(row=0, column=1, sticky="w")
        ttk.Button(top, text="Limpar", command=self.clear).grid(row=0, column=2, sticky="w")

        self.list = tk.Listbox(top, height=6)
        self.list.grid(row=1, column=0, columnspan=7, sticky="ew", pady=6)

        self.backend = tk.StringVar(value="torch")
        self.model = tk.StringVar()
        self.target = tk.StringVar(value="4k")
        self.codec = tk.StringVar(value="auto")
        self.crf = tk.IntVar(value=18)
        self.workers = tk.IntVar(value=0)
        self.out_dir = tk.StringVar()

        row = 2
        ttk.Label(top, text="Backend").grid(row=row, column=0, sticky="w")
        self.cb_backend = ttk.Combobox(top, textvariable=self.backend, values=list(BACKENDS),
                                       state="readonly", width=14)
        self.cb_backend.grid(row=row, column=1, sticky="w")
        self.cb_backend.bind("<<ComboboxSelected>>", self.on_backend)
        ttk.Label(top, text="Modelo").grid(row=row, column=2, sticky="w")
        self.cb_model = ttk.Combobox(top, textvariable=self.model, width=26)
        self.cb_model.grid(row=row, column=3, sticky="w")
        ttk.Label(top, text="Alvo").grid(row=row, column=4, sticky="w")
        ttk.Combobox(top, textvariable=self.target, values=list(vio.TARGETS),
                     width=8).grid(row=row, column=5, sticky="w")

        row = 3
        ttk.Label(top, text="Codec").grid(row=row, column=0, sticky="w")
        ttk.Combobox(top, textvariable=self.codec,
                     values=["auto", "libx264", "libx265", "h264_nvenc", "hevc_nvenc", "hevc_qsv"], width=14
                     ).grid(row=row, column=1, sticky="w")
        ttk.Label(top, text="CRF").grid(row=row, column=2, sticky="w")
        ttk.Spinbox(top, from_=0, to=51, textvariable=self.crf, width=5).grid(row=row, column=3, sticky="w")
        ttk.Label(top, text="Workers (0=auto)").grid(row=row, column=4, sticky="w")
        ttk.Spinbox(top, from_=0, to=64, textvariable=self.workers, width=5).grid(row=row, column=5, sticky="w")

        row = 4
        ttk.Label(top, text="Saída em").grid(row=row, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.out_dir, width=40).grid(row=row, column=1, columnspan=3, sticky="w")
        ttk.Button(top, text="...", width=3, command=self.pick_outdir).grid(row=row, column=4, sticky="w")

        row = 5
        self.btn_run = ttk.Button(top, text="Iniciar", command=self.start)
        self.btn_run.grid(row=row, column=0, pady=8, sticky="w")
        self.btn_cancel = ttk.Button(top, text="Cancelar", command=self.do_cancel, state="disabled")
        self.btn_cancel.grid(row=row, column=1, sticky="w")
        ttk.Button(top, text="Preview (1 frame)", command=self.preview).grid(row=row, column=2, sticky="w")
        ttk.Button(top, text="Diagnóstico", command=self.doctor).grid(row=row, column=3, sticky="w")

        self.bar = ttk.Progressbar(top, length=700, mode="determinate")
        self.bar.grid(row=6, column=0, columnspan=7, pady=4, sticky="ew")
        self.status = tk.StringVar(value="Pronto")
        ttk.Label(top, textvariable=self.status).grid(row=7, column=0, columnspan=7, sticky="w")
        self.log = tk.Text(top, height=10, state="disabled")
        self.log.grid(row=8, column=0, columnspan=7, sticky="nsew")

        self.on_backend()
        self.root.after(150, self.pump)

    # ------------------------------------------------------------------ utils
    def say(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def options(self) -> core.Options:
        return core.Options(backend=self.backend.get(), model=self.model.get() or None,
                            target=self.target.get(), codec=self.codec.get(), crf=int(self.crf.get()),
                            workers=int(self.workers.get()), overwrite=True)

    def add_files(self) -> None:
        for f in filedialog.askopenfilenames(filetypes=[("Vídeo", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v")]):
            p = Path(f)
            if p not in self.files:
                self.files.append(p)
                self.list.insert("end", p.name)

    def add_folder(self) -> None:
        d = filedialog.askdirectory()
        if not d:
            return
        for p in core.find_videos(d, recursive=True):
            if p not in self.files:
                self.files.append(p)
                self.list.insert("end", str(p))

    def clear(self) -> None:
        self.files.clear()
        self.list.delete(0, "end")

    def pick_outdir(self) -> None:
        d = filedialog.askdirectory()
        if d:
            self.out_dir.set(d)

    def on_backend(self, *_a) -> None:
        caps = BACKENDS[self.backend.get()].capabilities
        self.cb_model["values"] = list(caps.models)
        self.model.set(caps.default_model or "")
        self.say(f"{self.backend.get()}: {BACKENDS[self.backend.get()].description}")

    # ------------------------------------------------------------------ ações
    def start(self) -> None:
        if not self.files:
            messagebox.showwarning("Aviso", "Adicione pelo menos um vídeo.")
            return
        self.cancel.clear()
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="enabled")
        threading.Thread(target=self.worker, daemon=True).start()

    def do_cancel(self) -> None:
        self.cancel.set()
        self.status.set("Cancelando...")

    def progress(self, done: int, total: int) -> None:
        self.q.put(("progress", done, total))

    def worker(self) -> None:
        opts = self.options()
        outdir = self.out_dir.get() or None
        for i, src in enumerate(self.files, 1):
            if self.cancel.is_set():
                break
            self.q.put(("status", f"[{i}/{len(self.files)}] {src.name}"))
            try:
                dst = None
                if outdir:
                    dst = str(Path(outdir) / f"{src.stem}_{opts.backend}.mp4")
                r = core.run_job(str(src), dst, opts, progress=self.progress,
                                 should_cancel=self.cancel.is_set)
                self.q.put(("log", f"OK  {src.name}: {r.src_size[0]}x{r.src_size[1]} -> "
                                   f"{r.dst_size[0]}x{r.dst_size[1]} em {r.seconds:.1f}s"))
            except core.pipeline.Cancelled:
                self.q.put(("log", f"Cancelado: {src.name}"))
                break
            except Exception as e:
                self.q.put(("log", f"FALHOU {src.name}: {e}"))
        self.q.put(("done", 0, 0))

    def preview(self) -> None:
        if not self.files:
            return
        src = self.files[0]
        out = filedialog.asksaveasfilename(defaultextension=".png", initialfile=f"{src.stem}_preview.png")
        if not out:
            return
        opts = self.options()
        info, w, h = core.plan(str(src), opts)
        try:
            core.preview(str(src), opts.backend, w, h, out, opts)
        except Exception as e:
            messagebox.showerror("Erro", str(e))
            return
        self.say(f"Preview salvo: {out}")
        messagebox.showinfo("Preview", f"Comparação salva em:\n{out}")

    def doctor(self) -> None:
        for k, v in core.doctor():
            self.say(f"{k}: {v}")

    def pump(self) -> None:
        try:
            while True:
                msg, a, b = self.q.get_nowait()
                if msg == "progress" and b:
                    self.bar["value"] = 100 * a / b
                    self.status.set(f"{a}/{b} frames")
                elif msg == "status":
                    self.status.set(a)
                elif msg == "log":
                    self.say(a)
                elif msg == "done":
                    self.btn_run.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    self.bar["value"] = 0
                    self.status.set("Pronto")
        except queue.Empty:
            pass
        self.root.after(150, self.pump)


def run() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
