# -*- coding: utf-8 -*-
"""
candidatos.py — Estruturas comuns entre os módulos do motor de recuperação.

CandidatoArquivo descreve um arquivo candidato à recuperação, com sua
localização física (sequência correta de fragmentos quando os metadados
permitem), método de descoberta e dados de relatório.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime

# Estados possíveis de integridade
ESTADO_OK = "ok"
ESTADO_NAO_VERIFICADO = "nao_verificado"
ESTADO_CORROMPIDO = "corrompido"
ESTADO_PARCIAL = "parcial"

# Métodos de recuperação (relatório CSV)
METODO_PASTA = "pasta"          # metadados do sistema de arquivos
METODO_MFT = "mft"              # MFT do NTFS (registros em uso/apagados)
METODO_MFT_ORFA = "mft_orfa"    # registros FILE antigos fora da MFT atual
METODO_VARREDURA = "varredura"  # file carving por assinatura

EXTENSOES_FOTO = {
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "psd",
    "cr2", "cr3", "nef", "nrw", "arw", "dng", "orf", "raf", "rw2", "pef",
    "srw", "heic", "heif", "avif",
}
EXTENSOES_VIDEO = {
    "mp4", "mov", "m4v", "3gp", "3g2", "avi", "mts", "m2ts", "mkv",
}


def extensao_de(nome: str) -> str:
    return os.path.splitext(nome)[1].lstrip(".").lower()


class Fragmento:
    __slots__ = ("offset", "tamanho")

    def __init__(self, offset: int, tamanho: int):
        self.offset = offset
        self.tamanho = tamanho

    def __repr__(self):
        return f"Fragmento({self.offset}, {self.tamanho})"

    def __eq__(self, outro):
        return (isinstance(outro, Fragmento) and self.offset == outro.offset
                and self.tamanho == outro.tamanho)


class CandidatoArquivo:
    """Arquivo candidato, descoberto por metadados ou por assinatura."""

    def __init__(self, nome: str, fragmentos: list[Fragmento], metodo: str,
                 sistema: str = "", caminho_original: str = "",
                 tamanho: int | None = None, data: datetime | None = None,
                 confiavel: bool = True, motivo: str = "", extra: dict | None = None):
        self.nome = nome
        self.fragmentos = fragmentos
        self.metodo = metodo
        self.sistema = sistema            # 'fat16'|'fat32'|'exfat'|'ntfs'|''
        self.caminho_original = caminho_original
        self.tamanho = tamanho if tamanho is not None else sum(f.tamanho for f in fragmentos)
        self.data = data
        self.confiavel = confiavel
        self.motivo = motivo
        self.extra = extra or {}
        # preenchidos pelo motor após extração/verificação
        self.estado = None
        self.fracao_recuperada = None
        self.arquivo_destino = None
        self.sha256 = None
        self.verificado_por = ""

    @property
    def extensao(self) -> str:
        return extensao_de(self.nome)

    @property
    def offset_fisico(self) -> int:
        return self.fragmentos[0].offset if self.fragmentos else -1

    @property
    def posicao_no_cartao(self) -> str:
        if not self.fragmentos:
            return ""
        if len(self.fragmentos) == 1:
            return str(self.fragmentos[0].offset)
        return "+".join(f"{f.offset}({f.tamanho})" for f in self.fragmentos[:4]) + (
            "..." if len(self.fragmentos) > 4 else "")

    def __repr__(self):
        return f"CandidatoArquivo({self.nome!r}, {self.metodo}, {self.tamanho}B)"


def dos_para_datetime(d: int, t: int) -> datetime | None:
    """Converte data/hora FAT (valores brutos de 16 bits)."""
    try:
        ano = ((d >> 9) & 0x7F) + 1980
        mes = (d >> 5) & 0x0F
        dia = d & 0x1F
        h = (t >> 11) & 0x1F
        mi = (t >> 5) & 0x3F
        s = (t & 0x1F) * 2
        if not (1980 <= ano <= 2107 and 1 <= mes <= 12 and 1 <= dia <= 31):
            return None
        return datetime(ano, mes, dia, h, mi, s)
    except (ValueError, OverflowError):
        return None


def datetime_para_dos(dt: datetime) -> tuple[int, int]:
    d = (((dt.year - 1980) & 0x7F) << 9) | (dt.month << 5) | dt.day
    t = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    return d, t


_caracteres_proibidos = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def nome_seguro(nome: str, max_len: int = 180) -> str:
    """Gera um nome de arquivo seguro para gravação no destino."""
    nome = unicodedata.normalize("NFC", nome or "")
    nome = _caracteres_proibidos.sub("_", nome).strip().strip(".")
    nome = nome.replace("\u00a0", " ")
    if not nome:
        nome = "sem_nome"
    base, ext = os.path.splitext(nome)
    if len(ext) > 10:
        base, ext = nome, ""
    if len(base) > max_len - len(ext):
        base = base[:max_len - len(ext)]
    return base + ext


def nome_unico(destino: str, nome: str, existentes: set[str] | None = None) -> str:
    """Evita sobrescrita acidental de arquivos preexistentes no destino."""
    nome = nome_seguro(nome)
    candidatos = [nome]
    base, ext = os.path.splitext(nome)
    i = 1
    while True:
        cand = candidatos[-1]
        ocupado = os.path.exists(os.path.join(destino, cand)) or (
            existentes is not None and cand.lower() in existentes)
        if not ocupado:
            if existentes is not None:
                existentes.add(cand.lower())
            return cand
        cand = f"{base}_{i}{ext}"
        candidatos.append(cand)
        i += 1
        if i > 100000:
            raise OSError("muitas colisões de nome no destino")


def formatar_data(dt: datetime | None) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S") if dt else ""
