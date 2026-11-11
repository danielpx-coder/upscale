# -*- coding: utf-8 -*-
"""
aquisicao.py — Acesso físico a dispositivos, leitura somente-leitura e
aquisição forense de imagens de disco (Recupera Fotos 4.0).

Conteúdo:
  * LeitorBase / LeitorArquivo / LeitorDispositivo / LeitorRecorte
  * abrir_origem(): aceita "E:", "disco2", "\\\\.\\PhysicalDrive2", "/dev/sdb",
    caminhos de imagens e pastas.
  * listar_dispositivos(): enumeração de discos físicos (Windows via
    DeviceIoControl/WMI; Linux via /sys/block).
  * destino_mesmo_disco_fisico() / validar_destino(): segurança origem x destino.
  * Intervalos: conjunto de intervalos físicos ordenados e mesclados.
  * Adquiridor: criação de imagem com mapa .map.jsonl (versão 1), SHA-256 por
    bloco, retomada com verificação e registro de setores ilegíveis.

A origem é SEMPRE aberta somente para leitura. Este módulo nunca grava na
origem, nunca monta volumes e nunca altera tabelas de partição.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import sys
import threading
import time

SETOR_PADRAO = 512
BLOCO_AQUISICAO = 4 * 1024 * 1024  # 4 MiB por bloco do mapa

# ---------------------------------------------------------------------------
# Leitores
# ---------------------------------------------------------------------------


class ErroLeitura(OSError):
    """Falha de leitura no dispositivo/imagem de origem."""


class LeitorBase:
    """Interface de leitura somente-leitura sobre uma origem de dados."""

    nome = "?"
    setor_size = SETOR_PADRAO
    disco_fisico = None      # identificador do disco físico (nº no Windows, dev no Linux)
    e_dispositivo = False    # True quando a origem é um dispositivo físico/lógico real

    def __init__(self):
        self._fechado = False
        self.errores_leitura = 0

    @property
    def tamanho(self) -> int:
        raise NotImplementedError

    def read(self, offset: int, size: int) -> bytes:
        raise NotImplementedError

    def fechar(self):
        self._fechado = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()
        return False


class LeitorArquivo(LeitorBase):
    """Leitor para arquivos de imagem de disco (aberto somente-leitura)."""

    e_dispositivo = False

    def __init__(self, caminho: str):
        super().__init__()
        self.caminho = os.path.abspath(caminho)
        self.nome = os.path.basename(self.caminho)
        # 'rb' sem '+' garante que não há como gravar por este descritor.
        self._fh = open(self.caminho, "rb", buffering=0)
        self._tam = os.fstat(self._fh.fileno()).st_size
        st = os.stat(self.caminho)
        self.disco_fisico = _disco_do_caminho(self.caminho)
        self._st_dev = st.st_dev

    @property
    def tamanho(self) -> int:
        return self._tam

    def read(self, offset: int, size: int) -> bytes:
        if self._fechado:
            raise ErroLeitura("leitor fechado")
        if offset < 0 or size < 0:
            raise ErroLeitura("offset/tamanho inválidos")
        if offset >= self._tam:
            return b""
        try:
            self._fh.seek(offset)
            dados = self._fh.read(size)
        except OSError as exc:
            self.errores_leitura += 1
            raise ErroLeitura(f"falha de leitura em {offset}: {exc}") from exc
        if len(dados) < size:
            dados += b"\x00" * (size - len(dados))  # além do fim: zeros
        return dados

    def fechar(self):
        """Fecha o descritor de verdade. Sem isso o arquivo de imagem fica
        travado no Windows (não pode ser movido/apagado) e o descritor vaza a
        cada recuperação — o usuário roda várias numa mesma sessão."""
        if not self._fechado:
            try:
                self._fh.close()
            except Exception:
                pass
        super().fechar()


class LeitorDispositivo(LeitorBase):
    """Leitor de dispositivo físico (\\\\.\\PhysicalDriveN no Windows,
    /dev/sdX no Linux). Abre sempre somente-leitura e alinha as leituras ao
    tamanho real do setor."""

    e_dispositivo = True

    def __init__(self, caminho: str, disco_fisico=None):
        super().__init__()
        self.caminho = caminho
        self.nome = caminho
        self.disco_fisico = disco_fisico
        if sys.platform == "win32":
            self._fh = _abrir_windows_somente_leitura(caminho)
        else:
            self._fh = os.open(caminho, os.O_RDONLY)
        self.setor_size = _setor_logico(caminho, self._fh)
        self._tam = _tamanho_dispositivo(caminho, self._fh, self.setor_size)
        self._cache_off = -1
        self._cache_buf = b""

    @property
    def tamanho(self) -> int:
        return self._tam

    def read(self, offset: int, size: int) -> bytes:
        if self._fechado:
            raise ErroLeitura("leitor fechado")
        if offset >= self._tam or size == 0:
            return b""
        size = min(size, self._tam - offset)
        s = self.setor_size
        ini = (offset // s) * s
        fim = min(((offset + size + s - 1) // s) * s, self._tam)
        if self._cache_off == ini and len(self._cache_buf) >= (fim - ini):
            buf = self._cache_buf
        else:
            buf = self._leia_bruto(ini, fim - ini)
            self._cache_off = ini
            self._cache_buf = buf
        return buf[offset - ini: offset - ini + size]

    def _leia_bruto(self, ini: int, tam: int) -> bytes:
        partes = []
        resto = tam
        cur = ini
        while resto > 0:
            pedaco = min(resto, 4 * 1024 * 1024)
            try:
                if sys.platform == "win32":
                    _seek_windows(self._fh, cur)
                    dados = _read_windows(self._fh, pedaco)
                else:
                    os.lseek(self._fh, cur, os.SEEK_SET)
                    dados = os.read(self._fh, pedaco)
            except OSError as exc:
                self.errores_leitura += 1
                raise ErroLeitura(f"falha de leitura em {cur}: {exc}") from exc
            if not dados:
                dados = b"\x00" * pedaco  # além do fim
            partes.append(dados)
            resto -= len(dados)
            cur += len(dados)
        return b"".join(partes)

    def fechar(self):
        if not self._fechado:
            try:
                if sys.platform == "win32":
                    ctypes.windll.kernel32.CloseHandle(self._fh)
                else:
                    os.close(self._fh)
            except OSError:
                pass
        super().fechar()


class LeitorRecorte(LeitorBase):
    """Visão de uma sub-região (partição) de outro leitor. Não copia dados."""

    def __init__(self, base: LeitorBase, inicio: int, tamanho: int, nome: str = "",
                 disco_fisico=None, setor_size: int | None = None):
        super().__init__()
        self.base = base
        self.inicio = inicio
        self._tam = min(tamanho, max(0, base.tamanho - inicio))
        self.nome = nome or f"{base.nome}[{inicio}:{inicio + tamanho}]"
        self.disco_fisico = disco_fisico if disco_fisico is not None else base.disco_fisico
        self.setor_size = setor_size or base.setor_size
        self.e_dispositivo = base.e_dispositivo

    @property
    def tamanho(self) -> int:
        return self._tam

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0:
            raise ErroLeitura("offset negativo")
        if offset >= self._tam:
            return b""
        size = min(size, self._tam - offset)
        dados = self.base.read(self.inicio + offset, size)
        if len(dados) < size:
            dados += b"\x00" * (size - len(dados))
        return dados

    def fechar(self):
        super().fechar()  # não fecha a base: ela pode ter outros recortes


def _abrir_windows_somente_leitura(caminho: str):
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    OPEN_EXISTING = 3
    FILE_FLAG_NO_BUFFERING = 0x20000000
    handle = ctypes.windll.kernel32.CreateFileW(
        caminho, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, None)
    if handle == -1 or handle == 0xFFFFFFFFFFFFFFFF:
        raise ErroLeitura(f"não foi possível abrir {caminho} somente-leitura "
                          f"(erro {ctypes.GetLastError()}); execute como administrador")
    return handle


def _seek_windows(handle, pos: int):
    LARGE = ctypes.c_longlong(pos)
    ok = ctypes.windll.kernel32.SetFilePointerEx(handle, LARGE, None, 0)
    if not ok:
        raise OSError(f"seek falhou (erro {ctypes.GetLastError()})")


def _read_windows(handle, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    lidos = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.ReadFile(handle, buf, size, ctypes.byref(lidos), None)
    if not ok:
        raise OSError(f"read falhou (erro {ctypes.GetLastError()})")
    return buf.raw[:lidos.value]


def _setor_logico(caminho: str, fh) -> int:
    """Tamanho do setor lógico do dispositivo."""
    if sys.platform == "win32":
        class DISK_GEOMETRY_EX(ctypes.Structure):
            _fields_ = [("MediaType", ctypes.c_int), ("Cylinders", ctypes.c_longlong),
                        ("TracksPerCylinder", ctypes.c_ulong),
                        ("SectorsPerTrack", ctypes.c_ulong),
                        ("BytesPerSector", ctypes.c_ulong),
                        ("DiskSize", ctypes.c_longlong)]
        g = DISK_GEOMETRY_EX()
        ret = ctypes.c_ulong(0)
        IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
        ok = ctypes.windll.kernel32.DeviceIoControl(
            fh, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 0,
            ctypes.byref(g), ctypes.sizeof(g), ctypes.byref(ret), None)
        if ok and g.BytesPerSector:
            return int(g.BytesPerSector)
        return SETOR_PADRAO
    for candidato in (f"/sys/block/{os.path.basename(caminho).rstrip('0123456789')}/queue/logical_block_size",):
        try:
            with open(candidato) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            pass
    return SETOR_PADRAO


def _tamanho_dispositivo(caminho: str, fh, setor: int) -> int:
    if sys.platform == "win32":
        size = ctypes.c_longlong(0)
        IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
        ok = ctypes.windll.kernel32.DeviceIoControl(
            fh, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
            ctypes.byref(size), ctypes.sizeof(size), ctypes.byref(ctypes.c_ulong(0)), None)
        if ok:
            return int(size.value)
        return 0
    try:
        return os.lseek(fh, 0, os.SEEK_END)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Enumeração de dispositivos
# ---------------------------------------------------------------------------


def listar_dispositivos(todos: bool = False) -> list[dict]:
    """Lista discos físicos e unidades. Campos: id, nome, modelo, capacidade,
    unidades (letras), removivel, disco_sistema, caminho."""
    if sys.platform == "win32":
        return _listar_windows(todos)
    return _listar_linux(todos)


def _listar_windows(todos: bool) -> list[dict]:
    discos = []
    IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
    for i in range(64):
        caminho = f"\\\\.\\PhysicalDrive{i}"
        try:
            h = ctypes.windll.kernel32.CreateFileW(
                caminho, 0, 3, None, 3, 0, None)  # nenhum acesso: só metadados
        except Exception:
            continue
        if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
            continue
        try:
            class STORAGE_DEVICE_NUMBER(ctypes.Structure):
                _fields_ = [("DeviceType", ctypes.c_int),
                            ("DeviceNumber", ctypes.c_ulong),
                            ("PartitionNumber", ctypes.c_ulong)]
            n = STORAGE_DEVICE_NUMBER()
            ret = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.DeviceIoControl(
                h, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
                ctypes.byref(n), ctypes.sizeof(n), ctypes.byref(ret), None)
            num = int(n.DeviceNumber) if ok else i
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
        modelo, capacidade, removivel = _modelo_windows(i)
        letras = _letras_do_disco(i)
        disco_sistema = _e_disco_sistema_windows(i)
        if removivel or todos or letras:
            discos.append({
                "id": f"disco{num}", "numero": num, "nome": f"Disco {num}",
                "modelo": modelo, "capacidade": capacidade,
                "unidades": letras, "removivel": removivel,
                "disco_sistema": disco_sistema, "caminho": caminho,
            })
    return discos


def _modelo_windows(i: int):
    modelo, capacidade, removivel = "", 0, False
    try:
        import wmi  # opcional; sem ele usa fallback WMI via subprocess
        raise ImportError
    except Exception:
        try:
            import subprocess
            out = subprocess.run(
                ["wmic", "diskdrive", "get", "Index,Model,Size,MediaType",
                 "/format:csv"],
                capture_output=True, text=True, timeout=30).stdout
            for linha in out.splitlines():
                campos = linha.split(",")
                if len(campos) >= 5 and campos[1].strip() == str(i):
                    modelo = campos[2].strip()
                    try:
                        capacidade = int(campos[4].strip())
                    except ValueError:
                        capacidade = 0
                    mt = campos[3].strip().lower()
                    removivel = "removable" in mt or "usb" in modelo.lower()
        except Exception:
            pass
    return modelo, capacidade, removivel


def _letras_do_disco(i: int) -> list[str]:
    letras = []
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "path", "Win32_LogicalDiskToPartition", "get",
             "Antecedent,Dependent", "/format:csv"],
            capture_output=True, text=True, timeout=30).stdout
        for linha in out.splitlines():
            if f"DiskIndex={i}" in linha.replace(" ", "") and ":" in linha:
                m = re.search(r"([A-Z]):\\", linha)
                if m:
                    letras.append(m.group(1))
    except Exception:
        pass
    return sorted(set(letras))


def _e_disco_sistema_windows(i: int) -> bool:
    try:
        letra_sistema = os.environ.get("SystemDrive", "C:")[:1]
        return letra_sistema in _letras_do_disco(i)
    except Exception:
        return False


def _listar_linux(todos: bool) -> list[dict]:
    discos = []
    base = "/sys/block"
    if not os.path.isdir(base):
        return discos
    for nome in sorted(os.listdir(base)):
        if not re.match(r"(sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+)$", nome):
            continue
        dev = f"/dev/{nome}"
        capacidade = 0
        removivel = False
        modelo = ""
        try:
            with open(f"{base}/{nome}/size") as f:
                capacidade = int(f.read().strip()) * SETOR_PADRAO
        except OSError:
            continue
        try:
            with open(f"{base}/{nome}/removable") as f:
                removivel = f.read().strip() == "1"
        except OSError:
            pass
        try:
            with open(f"{base}/{nome}/device/model") as f:
                modelo = f.read().strip()
        except OSError:
            pass
        unidades = _pontos_montagem_linux(dev)
        if removivel or todos or unidades:
            discos.append({
                "id": nome, "numero": None, "nome": dev, "modelo": modelo,
                "capacidade": capacidade, "unidades": unidades,
                "removivel": removivel,
                "disco_sistema": "/" in unidades,
                "caminho": dev,
            })
    return discos


def _pontos_montagem_linux(dev: str) -> list[str]:
    pontos = []
    try:
        with open("/proc/mounts") as f:
            for linha in f:
                campos = linha.split()
                if len(campos) >= 2 and (campos[0] == dev or campos[0].startswith(dev + "p")
                                         or re.match(re.escape(dev) + r"\d+$", campos[0])):
                    pontos.append(campos[1])
    except OSError:
        pass
    return pontos


# ---------------------------------------------------------------------------
# Resolução da origem
# ---------------------------------------------------------------------------


def abrir_origem(entrada: str) -> LeitorBase:
    """Abre a origem somente-leitura. Aceita:
      * "E:" ou "E:\\"            -> volume físico do Windows
      * "disco2"                  -> PhysicalDrive2 (Windows) ou /dev/… (Linux)
      * "\\\\.\\PhysicalDrive2"   -> caminho direto
      * "/dev/sdb", "/dev/sdb2"   -> Linux
      * "C:\\Imagens\\cartao.img" -> arquivo de imagem
      * pasta do Windows/Linux    -> volume físico que contém a pasta
    """
    entrada = entrada.strip().strip('"')
    if not entrada:
        raise ValueError("origem vazia")

    m = re.fullmatch(r"disco\s*(\d+)", entrada, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if sys.platform == "win32":
            return LeitorDispositivo(f"\\\\.\\PhysicalDrive{n}", disco_fisico=n)
        candidatos = sorted(d["caminho"] for d in _listar_linux(todos=True))
        if n < len(candidatos):
            return LeitorDispositivo(candidatos[n], disco_fisico=candidatos[n])
        raise ValueError(f"disco{n} não encontrado neste sistema")

    if re.fullmatch(r"[A-Za-z]:\\\\?", entrada):
        if sys.platform != "win32":
            raise ValueError("letra de unidade só é suportada no Windows")
        letra = entrada[0].upper()
        num = _numero_disco_do_volume_windows(letra)
        return LeitorDispositivo(f"\\\\.\\{letra}:", disco_fisico=num)

    if entrada.startswith("\\\\.\\PhysicalDrive") or entrada.startswith("/dev/"):
        disco = None
        m = re.search(r"PhysicalDrive(\d+)", entrada)
        if m:
            disco = int(m.group(1))
        else:
            disco = entrada
        return LeitorDispositivo(entrada, disco_fisico=disco)

    caminho = os.path.abspath(entrada)
    if os.path.isdir(caminho):
        # Pasta: o motor resolve o volume físico e usa metadados do FS.
        return _abrir_pasta(caminho)
    if os.path.isfile(caminho):
        return LeitorArquivo(caminho)
    raise ValueError(f"origem não encontrada: {entrada}")


class OrigemPasta:
    """Origem do tipo 'pasta do Windows': guarda o caminho e o volume físico.
    O motor usa metadados do sistema de arquivos do volume (não os.walk)."""

    def __init__(self, pasta: str, leitor: LeitorBase | None,
                 offset_volume: int, caminho_dispositivo: str | None):
        self.pasta = os.path.abspath(pasta)
        self.leitor = leitor          # leitor do volume inteiro (pode ser None se indisponível)
        self.offset_volume = offset_volume
        self.caminho_dispositivo = caminho_dispositivo


def _abrir_pasta(pasta: str) -> OrigemPasta:
    if not os.path.isdir(pasta):
        raise ValueError(f"pasta não encontrada: {pasta}")
    leitor = None
    offset = 0
    caminho_dev = None
    if sys.platform == "win32":
        letra = os.path.splitdrive(pasta)[0][:1].upper()
        try:
            num = _numero_disco_do_volume_windows(letra)
            caminho_dev = f"\\\\.\\{letra}:"
            leitor = LeitorDispositivo(caminho_dev, disco_fisico=num)
            offset = _offset_do_volume_na_unidade(letra)
        except (OSError, ValueError):
            leitor = None
    else:
        dev, off = _dispositivo_do_caminho_linux(pasta)
        if dev:
            caminho_dev = dev
            offset = off
            try:
                leitor = LeitorDispositivo(dev, disco_fisico=dev)
            except OSError:
                leitor = None
    return OrigemPasta(pasta, leitor, offset, caminho_dev)


def _numero_disco_do_volume_windows(letra: str):
    class STORAGE_DEVICE_NUMBER(ctypes.Structure):
        _fields_ = [("DeviceType", ctypes.c_int),
                    ("DeviceNumber", ctypes.c_ulong),
                    ("PartitionNumber", ctypes.c_ulong)]
    h = ctypes.windll.kernel32.CreateFileW(f"\\\\.\\{letra}:", 0, 3, None, 3, 0, None)
    if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
        raise OSError(f"não foi possível abrir o volume {letra}:")
    try:
        n = STORAGE_DEVICE_NUMBER()
        ok = ctypes.windll.kernel32.DeviceIoControl(
            h, 0x002D1080, None, 0, ctypes.byref(n), ctypes.sizeof(n),
            ctypes.byref(ctypes.c_ulong(0)), None)
        if not ok:
            raise OSError("IOCTL_STORAGE_GET_DEVICE_NUMBER falhou")
        return int(n.DeviceNumber)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _offset_do_volume_na_unidade(letra: str) -> int:
    class VOLUME_DISK_EXTENTS_L(ctypes.Structure):
        _fields_ = [("NumberOfDiskExtents", ctypes.c_ulong),
                    ("ExtentType", ctypes.c_int),
                    ("StartingOffset", ctypes.c_longlong),
                    ("ExtentLength", ctypes.c_longlong)]
    h = ctypes.windll.kernel32.CreateFileW(f"\\\\.\\{letra}:", 0, 3, None, 3, 0, None)
    if h == -1:
        return 0
    try:
        e = VOLUME_DISK_EXTENTS_L()
        ok = ctypes.windll.kernel32.DeviceIoControl(
            h, 0x00560000, None, 0, ctypes.byref(e), ctypes.sizeof(e),
            ctypes.byref(ctypes.c_ulong(0)), None)
        if ok and e.NumberOfDiskExtents >= 1:
            return int(e.StartingOffset)
        return 0
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _dispositivo_do_caminho_linux(caminho: str):
    """Retorna (dispositivo, offset_da_particao_no_disco) ou (None, 0)."""
    try:
        st = os.stat(caminho)
    except OSError:
        return None, 0
    try:
        with open("/proc/self/mountinfo") as f:
            for linha in f:
                campos = linha.split()
                if len(campos) >= 10 and campos[0] == str(st.st_dev & 0xFFFF):
                    pass
    except OSError:
        pass
    # mapeamento simples via /proc/mounts + stat do dispositivo
    try:
        with open("/proc/mounts") as f:
            for linha in f:
                campos = linha.split()
                if len(campos) >= 2 and campos[1] == caminho and campos[0].startswith("/dev/"):
                    dev = campos[0]
                    base = re.sub(r"p?\d+$", "", dev)
                    try:
                        with open(f"{dev.replace('/dev/', '/sys/block/')}/start") as f2:
                            off = int(f2.read().strip()) * SETOR_PADRAO
                    except OSError:
                        # partição raiz do disco inteiro
                        off = 0
                        base = dev
                    return base, off
    except OSError:
        pass
    return None, 0


def _disco_do_caminho(caminho: str):
    try:
        st = os.stat(caminho)
        return ("stdev", st.st_dev)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Validação de destino (segurança origem x destino)
# ---------------------------------------------------------------------------


class ResultadoValidacao:
    def __init__(self, ok: bool, motivo: str, advertencias: list[str] | None = None):
        self.ok = ok
        self.motivo = motivo
        self.advertencias = advertencias or []

    def __repr__(self):
        return f"ResultadoValidacao(ok={self.ok}, motivo={self.motivo!r})"


def _disco_fisico_do_caminho_destino(destino: str):
    if sys.platform == "win32":
        raiz = os.path.splitdrive(os.path.abspath(destino))[0]
        if raiz and raiz[0].isalpha():
            try:
                return _numero_disco_do_volume_windows(raiz[0].upper())
            except OSError:
                return None
        return None
    dev, _ = _dispositivo_do_caminho_linux(os.path.abspath(destino))
    return dev


def destino_mesmo_disco_fisico(origem, destino: str):
    """Retorna (mesmo_disco: True/False/None, motivo). None = não confirmado."""
    disco_dest = _disco_fisico_do_caminho_destino(destino)
    disco_orig = None
    if isinstance(origem, LeitorBase):
        disco_orig = origem.disco_fisico
    elif isinstance(origem, OrigemPasta):
        disco_orig = origem.leitor.disco_fisico if origem.leitor else None

    if isinstance(origem, OrigemPasta):
        # pasta: nunca gravar dentro da própria pasta pesquisada
        p = os.path.abspath(destino).rstrip("\\/")
        if p == origem.pasta.rstrip("\\/") or p.startswith(origem.pasta.rstrip("\\/") + os.sep):
            return True, "o destino está dentro da pasta pesquisada"

    # origem é arquivo de imagem: compara o dispositivo do sistema de
    # arquivos que contém a imagem e o destino (st_dev)
    if isinstance(origem, LeitorArquivo):
        try:
            st = os.stat(destino)
            mesmo = st.st_dev == origem._st_dev
            return mesmo, ("origem e destino estão no mesmo disco físico "
                           "(mesmo dispositivo de arquivo)"
                           if mesmo else "discos físicos diferentes")
        except (OSError, AttributeError):
            pass

    if disco_orig is None or disco_dest is None:
        return None, "não foi possível confirmar o disco físico de origem e/ou destino"

    if isinstance(disco_orig, int) and isinstance(disco_dest, int):
        return (disco_orig == disco_dest), (
            "origem e destino estão no mesmo disco físico"
            if disco_orig == disco_dest else "discos físicos diferentes")
    if disco_orig == disco_dest:
        return True, "origem e destino estão no mesmo disco físico"
    if type(disco_orig) is type(disco_dest):
        return False, "discos físicos diferentes"
    return None, "não foi possível comparar os discos físicos"


def validar_destino(origem, destino: str, permitir_mesmo_disco: bool = False,
                    criar_se_nao_existe: bool = True) -> ResultadoValidacao:
    """Valida o destino para gravação dos arquivos recuperados.

    Regras (obrigatórias):
      * Destino dentro da pasta de origem -> bloqueado.
      * Origem e destino no mesmo disco físico -> bloqueado, salvo
        permitir_mesmo_disco=True (apenas quando a origem é um ARQUIVO de
        imagem, que é uma cópia e não a mídia original).
      * Não confirmado -> bloqueado (com explicação), salvo permitir_mesmo_disco.
    """
    destino = os.path.abspath(destino)
    adv = []
    if os.path.exists(destino) and not os.path.isdir(destino):
        return ResultadoValidacao(False, "o destino existe e não é uma pasta")
    if criar_se_nao_existe and not os.path.exists(destino):
        try:
            os.makedirs(destino, exist_ok=True)
        except OSError as exc:
            return ResultadoValidacao(False, f"não foi possível criar o destino: {exc}")
    if not os.path.exists(destino):
        return ResultadoValidacao(
            False, "o destino não existe (crie a pasta ou escolha outra)")
    if not os.access(destino, os.W_OK):
        return ResultadoValidacao(False, "sem permissão de gravação no destino")

    if isinstance(origem, OrigemPasta):
        p = destino.rstrip("\\/")
        o = origem.pasta.rstrip("\\/")
        if p == o or p.startswith(o + os.sep) or o.startswith(p + os.sep):
            return ResultadoValidacao(False,
                                      "o destino está dentro (ou contém) a pasta pesquisada")
        if origem.leitor is not None and origem.leitor.disco_fisico is not None:
            dd = _disco_fisico_do_caminho_destino(destino)
            if dd is not None and dd == origem.leitor.disco_fisico:
                if not permitir_mesmo_disco:
                    return ResultadoValidacao(
                        False, "o destino está no mesmo disco físico do volume "
                               "pesquisado; escolher outro disco")
                adv.append("destino no mesmo disco do volume pesquisado (autorizado)")

    mesmo, motivo = destino_mesmo_disco_fisico(origem, destino)
    if mesmo is None:
        if permitir_mesmo_disco:
            adv.append(motivo)
        else:
            return ResultadoValidacao(
                False, f"{motivo}. Por segurança, a gravação foi bloqueada: "
                       "escolha um destino em outro disco físico.")
    elif mesmo:
        if isinstance(origem, LeitorArquivo) and permitir_mesmo_disco:
            adv.append("origem é um arquivo de imagem; gravação no mesmo disco autorizada")
        else:
            return ResultadoValidacao(False,
                                      "o destino pertence ao mesmo disco físico da origem; "
                                      "escolher outro disco para não sobrescrever dados")
    return ResultadoValidacao(True, motivo, adv)


# ---------------------------------------------------------------------------
# Intervalos físicos (deduplicação de regiões recuperadas)
# ---------------------------------------------------------------------------


class Intervalos:
    """Conjunto de intervalos [inicio, fim) ordenados e mesclados."""

    def __init__(self):
        self._lista: list[list[int]] = []

    def adicionar(self, inicio: int, fim: int):
        if fim <= inicio:
            return
        novo = [inicio, fim]
        saida = []
        inserido = False
        for it in self._lista:
            if it[1] < novo[0] or it[0] > novo[1]:
                saida.append(it)
            else:
                novo[0] = min(novo[0], it[0])
                novo[1] = max(novo[1], it[1])
        for i, it in enumerate(saida):
            if not inserido and it[0] > novo[0]:
                saida.insert(i, novo)
                inserido = True
                break
        if not inserido:
            saida.append(novo)
        self._lista = saida

    def contem(self, inicio: int, fim: int | None = None) -> bool:
        fim = inicio + 1 if fim is None else fim
        for it in self._lista:
            if it[0] <= inicio and fim <= it[1]:
                return True
        return False

    def sobrepoe(self, inicio: int, fim: int) -> bool:
        for it in self._lista:
            if inicio < it[1] and it[0] < fim:
                return True
        return False

    def total_bytes(self) -> int:
        return sum(f - i for i, f in self._lista)

    def __len__(self):
        return len(self._lista)

    def __iter__(self):
        return iter((tuple(x) for x in self._lista))


# ---------------------------------------------------------------------------
# Aquisição forense de imagens
# ---------------------------------------------------------------------------

MAPA_VERSAO = 1


def _sha256(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


class Adquiridor:
    """Cria imagens de disco em outro disco físico, com mapa .map.jsonl
    (versão 1), SHA-256 por bloco, retomada verificada e registro de erros."""

    PADRAO_ERRO = b"\xf8"

    def __init__(self, leitor: LeitorBase, caminho_imagem: str,
                 bloco: int = BLOCO_AQUISICAO,
                 progresso=None, cancelar: threading.Event | None = None):
        self.leitor = leitor
        self.caminho_imagem = caminho_imagem
        self.caminho_mapa = caminho_imagem + ".map.jsonl"
        self.bloco = bloco
        self.progresso = progresso  # fn(bytes_lidos, total, fase:str)
        self.cancelar = cancelar or threading.Event()
        self.blocos_ok = 0
        self.blocos_erro = 0
        self.blocos_zero = 0
        self.cancelado = False
        self.bytes_copiados = 0

    # -- mapa --------------------------------------------------------------

    def _carregar_mapa(self) -> dict[int, dict]:
        mapa = {}
        cab = None
        if os.path.exists(self.caminho_mapa):
            try:
                with open(self.caminho_mapa, "r", encoding="utf-8") as f:
                    for linha in f:
                        linha = linha.strip()
                        if not linha:
                            continue
                        reg = json.loads(linha)
                        if "versao" in reg:
                            cab = reg
                        else:
                            mapa[reg["i"]] = reg
            except (OSError, json.JSONDecodeError):
                return {}
        if cab and cab.get("versao") != MAPA_VERSAO:
            return {}
        if cab and cab.get("bloco") and cab["bloco"] != self.bloco:
            return {}
        return mapa

    def _escrever_cabecalho_mapa(self, f, retomada: bool):
        cab = {
            "versao": MAPA_VERSAO,
            "origem": getattr(self.leitor, "caminho", self.leitor.nome),
            "tamanho": self.leitor.tamanho,
            "setor": self.leitor.setor_size,
            "bloco": self.bloco,
            "modo": "retomada" if retomada else "nova",
            "data": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        f.write(json.dumps(cab, ensure_ascii=False) + "\n")

    # -- aquisição -----------------------------------------------------------

    def executar(self, retomar: bool = False) -> dict:
        total = self.leitor.tamanho
        n_blocos = (total + self.bloco - 1) // self.bloco
        mapa_antigo = self._carregar_mapa() if retomar else {}
        existe_img = os.path.exists(self.caminho_imagem)

        if not retomar and existe_img:
            # não sobrescrever silenciosamente
            raise FileExistsError(
                f"a imagem {self.caminho_imagem} já existe; use --retomar-imagem "
                "ou remova o arquivo")

        modo = "rb+" if (retomar and existe_img) else "wb"
        resumo = {"total_bytes": total, "blocos": n_blocos, "ok": 0,
                  "erro": 0, "zero": 0, "retomada": bool(retomar),
                  "revalidados": 0, "copiados": 0, "cancelado": False}

        with open(self.caminho_imagem, modo, buffering=0) as img, \
                open(self.caminho_mapa, "w", encoding="utf-8") as mapa_f:
            self._escrever_cabecalho_mapa(mapa_f, retomada=bool(mapa_antigo))
            for i in range(n_blocos):
                if self.cancelar.is_set():
                    self.cancelado = True
                    resumo["cancelado"] = True
                    break
                off = i * self.bloco
                tam = min(self.bloco, total - off)
                reg_antigo = mapa_antigo.get(i) if retomar else None
                dados = None
                if reg_antigo and reg_antigo.get("estado") == "ok" and existe_img:
                    # verifica o bloco já copiado antes de confiar nele
                    try:
                        img.seek(off)
                        dados = img.read(tam)
                    except OSError:
                        dados = None
                    if dados and len(dados) == tam and _sha256(dados) == reg_antigo.get("sha256"):
                        resumo["revalidados"] += 1
                        mapa_f.write(json.dumps(reg_antigo) + "\n")
                        resumo["ok"] += 1
                        self.blocos_ok += 1
                        self.bytes_copiados += tam
                        if self.progresso:
                            self.progresso(self.bytes_copiados, total, "aquisicao")
                        continue
                    dados = None
                estado = "ok"
                try:
                    dados = self.leitor.read(off, tam)
                    if len(dados) < tam:
                        dados += b"\x00" * (tam - len(dados))
                except (ErroLeitura, OSError):
                    dados = self.PADRAO_ERRO * tam
                    estado = "erro"
                if estado == "ok" and not dados.strip(b"\x00"):
                    estado = "zero"
                try:
                    img.seek(off)
                    img.write(dados)
                except OSError as exc:
                    raise OSError(f"falha ao gravar a imagem em {off}: {exc}") from exc
                sha = _sha256(dados)
                reg = {"i": i, "sha256": sha, "estado": estado}
                mapa_f.write(json.dumps(reg) + "\n")
                mapa_f.flush()
                resumo[estado] = resumo.get(estado, 0) + 1
                if estado == "ok":
                    self.blocos_ok += 1
                elif estado == "erro":
                    self.blocos_erro += 1
                else:
                    self.blocos_zero += 1
                self.bytes_copiados += tam
                resumo["copiados"] += tam
                if self.progresso:
                    self.progresso(self.bytes_copiados, total, "aquisicao")
        return resumo

    @staticmethod
    def sha256_arquivo(caminho: str, bloco: int = 4 * 1024 * 1024) -> str:
        h = hashlib.sha256()
        with open(caminho, "rb") as f:
            while True:
                d = f.read(bloco)
                if not d:
                    break
                h.update(d)
        return h.hexdigest()


def abrir_imagem(caminho: str) -> LeitorArquivo:
    return LeitorArquivo(caminho)
