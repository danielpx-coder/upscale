"""Interface gráfica simples (Tkinter)."""
import threading, tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from .backends import BACKENDS
from .video_io import TARGETS, probe, target_size


def run() -> int:
    root = tk.Tk(); root.title("Video Upscale"); root.geometry("560x260")
    src, backend, target, model = tk.StringVar(), tk.StringVar(value="ffmpeg"), tk.StringVar(value="4k"), tk.StringVar()
    status = tk.StringVar(value="Selecione um vídeo")
    f = ttk.Frame(root, padding=12); f.pack(fill="both", expand=True)
    ttk.Label(f, text="Vídeo:").grid(row=0, column=0, sticky="w")
    ttk.Entry(f, textvariable=src, width=50).grid(row=0, column=1)
    ttk.Button(f, text="...", command=lambda: src.set(filedialog.askopenfilename())).grid(row=0, column=2)
    ttk.Label(f, text="Backend:").grid(row=1, column=0, sticky="w")
    ttk.Combobox(f, textvariable=backend, values=list(BACKENDS), state="readonly").grid(row=1, column=1, sticky="w")
    ttk.Label(f, text="Resolução:").grid(row=2, column=0, sticky="w")
    ttk.Combobox(f, textvariable=target, values=list(TARGETS)).grid(row=2, column=1, sticky="w")
    ttk.Label(f, text="Modelo (opc.):").grid(row=3, column=0, sticky="w")
    ttk.Entry(f, textvariable=model).grid(row=3, column=1, sticky="w")
    bar = ttk.Progressbar(f, length=400); bar.grid(row=5, column=0, columnspan=3, pady=10)
    ttk.Label(f, textvariable=status).grid(row=6, column=0, columnspan=3)

    def prog(d, t):
        root.after(0, lambda: (bar.configure(value=100 * d / t if t else 0), status.set(f"{d}/{t}")))

    def work():
        try:
            info = probe(src.get()); w, h = target_size(info, None, target.get())
            out = str(Path(src.get()).with_name(f"{Path(src.get()).stem}_{w}x{h}.mp4"))
            root.after(0, status.set, f"Processando {w}x{h}...")
            BACKENDS[backend.get()](model=model.get() or None).upscale(src.get(), out, w, h, prog)
            root.after(0, lambda: messagebox.showinfo("Pronto", out))
        except Exception as e:
            root.after(0, lambda: messagebox.showerror("Erro", str(e)))
        finally:
            root.after(0, status.set, "Pronto")

    ttk.Button(f, text="Iniciar upscale", command=lambda: threading.Thread(target=work, daemon=True).start()
               ).grid(row=4, column=1, pady=8)
    root.mainloop(); return 0
