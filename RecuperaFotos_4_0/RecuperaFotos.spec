# -*- mode: python ; coding: utf-8 -*-
"""
RecuperaFotos.spec — receita do PyInstaller para gerar o executável Windows.

Como usar (ou apenas dê dois cliques em Gerar_EXE.bat):

    python -m pip install --user pyinstaller
    python -m PyInstaller --noconfirm RecuperaFotos.spec

O resultado fica em dist\\RecuperaFotos\\ (pasta com o .exe e as DLLs). Copie
essa pasta inteira para o computador de destino — é portátil, não instala nada.

Por que "onedir" e não "onefile":
  * onefile descompacta ~40 MB em %TEMP% a cada execução (lento num pen drive);
  * onedir abre na hora e pode ser copiado para um pendrive e executado de lá;
  * para gerar onefile, basta trocar exclude_binaries=True por False, apagar o
    bloco COLLECT no fim e adicionar upx=False — nada mais muda.

Console:
  console=False  -> o .exe abre só a janela (o padrão aqui).
  console=True   -> abre também a janela preta, útil para usar a linha de
                    comando (recupera_fotos.exe imagem.img -o E:\\Recuperadas).
  Nos dois casos a CLI funciona: sem console, a saída é gravada em
  recupera_fotos_log.txt ao lado do .exe (cuidado do próprio programa).
"""

import os

from PyInstaller.utils.hooks import collect_submodules

RAIZ = os.path.abspath(SPECPATH)

# módulos do próprio programa (importados dinamicamente em alguns pontos)
MODULOS_APP = [
    "aquisicao", "assinaturas", "candidatos", "exfat", "fatfs", "interface",
    "motor", "ntfs", "particoes", "verificacao",
]

# tkinter: nomes que o PyInstaller às vezes não vê sozinho
MODULOS_TK = ["tkinter", "tkinter.ttk", "tkinter.filedialog",
              "tkinter.messagebox", "tkinter.simpledialog"]

# Pillow: se estiver instalado, inclui todos os submódulos (plugins de formato)
MODULOS_PIL = []
try:
    MODULOS_PIL = collect_submodules("PIL")
except Exception:
    pass  # Pillow ausente: o programa funciona sem ele (avisos próprios)

hiddenimports = MODULOS_APP + MODULOS_TK + MODULOS_PIL

# nada de testes, nem de bibliotecas gráficas/científicas gigantes que algum
# plugin do Pillow possa importar por engano
excludes = [
    "tests", "unittest", "pytest", "pydoc", "doctest",
    "numpy", "matplotlib", "pandas", "scipy", "IPython", "notebook",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
]

a = Analysis(
    ["recupera_fotos.py"],
    pathex=[RAIZ],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="RecuperaFotos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RecuperaFotos",
)
