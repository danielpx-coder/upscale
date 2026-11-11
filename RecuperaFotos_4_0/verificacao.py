"""
verificacao.py — Verificação de integridade e classificação dos arquivos
recuperados.

Estados (obrigatórios):
  * ok            — passou nas verificações EFETIVAMENTE implementadas
                    (estruturais e, quando compatível, decodificação completa
                    pelo Pillow). Não é garantia forense absoluta.
  * nao_verificado — extraído, mas sem verificador suficiente (RAW, HEIC sem
                    decodificador, PSD, vídeos...).
  * corrompido    — corrupção COMPROVADA (estrutura inválida, CRC errado,
                    falha de decodificação em formato suportado).
  * parcial       — fração recuperada por mecanismo real (metadados indicam
                    tamanho maior que os dados fisicamente recuperáveis).

O verificador de JPEG é INDEPENDENTE do Pillow (marcadores, segmentos, SOS,
EOI, sequência RST0..RST7 com DRI). O Pillow é verificador adicional.
"""

from __future__ import annotations

import struct

try:
    from PIL import Image
    # ImageFile.LOAD_TRUNCATED_IMAGES fica DESLIGADO de propósito: com ele o
    # Pillow carregaria uma imagem cortada sem reclamar e o arquivo truncado
    # seria classificado como "ok" — exatamente a mentira que este programa
    # existe para não contar.
    PIL_DISPONIVEL = True
except ImportError:      # pragma: no cover - Pillow é opcional
    Image = None
    PIL_DISPONIVEL = False

import assinaturas
from aquisicao import LeitorBase

# Formatos que o Pillow decodifica com confiança (falha => corrupção)
PILLOW_CONFIÁVEIS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff"}
# Formatos apenas estruturais (Pillow não cobre ou cobre parcialmente)
SO_ESTRUTURAL = {"cr2", "cr3", "nef", "nrw", "arw", "dng", "orf", "raf",
                 "rw2", "pef", "srw", "heic", "heif", "avif", "psd",
                 "mp4", "mov", "m4v", "3gp", "3g2", "mts", "m2ts", "mkv"}
FAMILIA_TIFF = {"tif", "tiff", "cr2", "nef", "nrw", "arw", "dng", "orf",
                "rw2", "pef", "srw"}
VIDEOS = {"mp4", "mov", "m4v", "3gp", "3g2", "mts", "m2ts", "mkv", "avi"}


class LeitorBytes(LeitorBase):
    """Leitor sobre um buffer em memória (usado nos verificadores)."""

    def __init__(self, dados: bytes, nome: str = "bytes"):
        super().__init__()
        self._dados = dados
        self.nome = nome

    @property
    def tamanho(self) -> int:
        return len(self._dados)

    def read(self, offset: int, size: int) -> bytes:
        if offset >= len(self._dados) or size <= 0:
            return b""
        return self._dados[offset:offset + size]


class ResultadoVerificacao:
    def __init__(self, estado: str, motivo: str = "", fracao: float | None = None,
                 verificador: str = "", diagnostico: dict | None = None):
        self.estado = estado
        self.motivo = motivo
        self.fracao = fracao
        self.verificador = verificador
        self.diagnostico = diagnostico or {}

    def __repr__(self):
        return f"ResultadoVerificacao({self.estado!r}, {self.motivo!r})"


# ---------------------------------------------------------------------------
# JPEG independente do Pillow
# ---------------------------------------------------------------------------


def verificar_jpeg_estrutural(dados: bytes) -> ResultadoVerificacao:
    """Verificador próprio: marcadores, segmentos, SOS, EOI, RST (com DRI)."""
    leitor = LeitorBytes(dados)
    res = assinaturas.analisar_jpeg(leitor, 0)
    if res is None:
        return ResultadoVerificacao("corrompido", "não é um JPEG (SOI ausente)",
                                    verificador="estrutural_jpeg")
    diag = res.diagnostico
    if res.comprimento is None:
        return ResultadoVerificacao("corrompido", res.motivo or "EOI não encontrado",
                                    verificador="estrutural_jpeg", diagnostico=diag)
    if diag.get("rst_quebras"):
        return ResultadoVerificacao(
            "corrompido",
            f"sequência de marcadores RST quebrada ({diag['rst_quebras']}x) — "
            "possíveis fragmentos de outra fotografia intercalados",
            verificador="estrutural_jpeg", diagnostico=diag)
    if diag.get("sem_eoi") or res.truncado:
        return ResultadoVerificacao(
            "parcial",
            (res.motivo or "EOI não encontrado")
            + f" — {res.comprimento} byte(s) de imagem aproveitáveis",
            verificador="estrutural_jpeg", diagnostico=diag)
    if res.comprimento < len(dados):
        # dados após o EOI: miniatura/motion photo/duplicidade
        resto = len(dados) - res.comprimento
        mv = diag.get("motion_video_offset")
        if mv and _mp4_valido_em(dados, len(dados) - mv):
            return ResultadoVerificacao(
                "ok", "Motion Photo: vídeo MP4 anexo verificável",
                fracao=1.0, verificador="estrutural_jpeg+motion", diagnostico=diag)
        mpf = diag.get("mpf")
        if mpf and mpf.get("valido"):
            fim = max(o + t for o, t in mpf["imagens"])
            if fim == len(dados):
                return ResultadoVerificacao(
                    "ok", "JPEG MPF: páginas contíguas e índices válidos",
                    fracao=1.0, verificador="estrutural_jpeg+mpf", diagnostico=diag)
        return ResultadoVerificacao(
            "ok", f"JPEG íntegro com {resto} byte(s) após o EOI "
                  "(anexo não verificável, preservado)",
            fracao=1.0, verificador="estrutural_jpeg", diagnostico=diag)
    return ResultadoVerificacao("ok", "", fracao=1.0,
                                verificador="estrutural_jpeg", diagnostico=diag)


def _mp4_valido_em(dados: bytes, off: int) -> bool:
    res = assinaturas.analisar_mp4(LeitorBytes(dados), off, fim_regiao=len(dados))
    return bool(res and res.comprimento and off + res.comprimento == len(dados))


# ---------------------------------------------------------------------------
# Verificadores estruturais por formato
# ---------------------------------------------------------------------------


def verificar_estrutural(dados: bytes, ext: str) -> ResultadoVerificacao | None:
    """Aplica o detector estrutural correspondente à extensão."""
    leitor = LeitorBytes(dados)
    n = len(dados)
    ext = ext.lower()
    if ext in ("jpg", "jpeg"):
        return verificar_jpeg_estrutural(dados)
    if ext == "png":
        res = assinaturas.analisar_png(leitor, 0)
        return _de_resultado_carving(res, n, "png")
    if ext == "gif":
        res = assinaturas.analisar_gif(leitor, 0)
        return _de_resultado_carving(res, n, "gif")
    if ext == "bmp":
        res = assinaturas.analisar_bmp(leitor, 0)
        return _de_resultado_carving(res, n, "bmp")
    if ext in FAMILIA_TIFF:
        res = assinaturas.analisar_tiff(leitor, 0)
        return _de_resultado_carving(res, n, "tiff")
    if ext == "raf":
        res = assinaturas.analisar_raf(leitor, 0)
        return _de_resultado_carving(res, n, "raf")
    if ext == "psd":
        res = assinaturas.analisar_psd(leitor, 0)
        return _de_resultado_carving(res, n, "psd")
    if ext == "webp":
        res = assinaturas.analisar_riff(leitor, 0)
        return _de_resultado_carving(res, n, "webp")
    if ext == "avi":
        res = assinaturas.analisar_riff(leitor, 0)
        return _de_resultado_carving(res, n, "avi")
    if ext in ("mp4", "mov", "m4v", "3gp", "3g2", "heic", "heif", "avif", "cr3"):
        res = assinaturas.analisar_mp4(leitor, 0)
        return _de_resultado_carving(res, n, "mp4")
    if ext in ("mts", "m2ts"):
        res = assinaturas.analisar_mpegts(leitor, 0)
        return _de_resultado_carving(res, n, "mpegts")
    if ext == "mkv":
        res = assinaturas.analisar_mkv(leitor, 0)
        return _de_resultado_carving(res, n, "mkv")
    return None


def _de_resultado_carving(res, n: int, fmt: str) -> ResultadoVerificacao:
    if res is None:
        return ResultadoVerificacao("corrompido",
                                    f"estrutura {fmt} não reconhecida",
                                    verificador=f"estrutural_{fmt}")
    if res.comprimento is None:
        return ResultadoVerificacao("corrompido", res.motivo or f"estrutura {fmt} inválida",
                                    verificador=f"estrutural_{fmt}",
                                    diagnostico=res.diagnostico)
    motivo = res.motivo
    if res.comprimento < n:
        motivo = (motivo + "; " if motivo else "") + \
            f"estrutura termina em {res.comprimento} de {n} bytes"
        return ResultadoVerificacao("ok", motivo, fracao=res.comprimento / n,
                                    verificador=f"estrutural_{fmt}",
                                    diagnostico=res.diagnostico)
    return ResultadoVerificacao("ok", motivo, fracao=1.0,
                                verificador=f"estrutural_{fmt}",
                                diagnostico=res.diagnostico)


# ---------------------------------------------------------------------------
# Pillow (verificador adicional)
# ---------------------------------------------------------------------------


def verificar_pillow(dados: bytes, ext: str) -> ResultadoVerificacao | None:
    """Decodificação completa pelo Pillow. None se não aplicável."""
    if not PIL_DISPONIVEL:
        return None
    if ext == "heic" or ext == "heif":
        return None  # Pillow não decodifica HEIC sem plugin
    try:
        from io import BytesIO
        img = Image.open(BytesIO(dados))
        img.load()  # decodificação completa
        img.close()
        return ResultadoVerificacao("ok", "decodificação completa pelo Pillow",
                                    fracao=1.0, verificador="pillow")
    except Exception as exc:
        nome = type(exc).__name__
        return ResultadoVerificacao("corrompido",
                                    f"falha na decodificação Pillow ({nome}: {exc})",
                                    verificador="pillow")


# ---------------------------------------------------------------------------
# Classificação final
# ---------------------------------------------------------------------------


def classificar(dados: bytes, ext: str, tamanho_esperado: int | None = None,
                usar_pillow: bool = True) -> ResultadoVerificacao:
    """Classifica um arquivo recuperado.

    tamanho_esperado: vindo dos metadados; se os dados são menores, há base
    real para 'parcial' (fração recuperada conhecida).
    """
    ext = (ext or "").lower().lstrip(".")
    n = len(dados)
    if n == 0:
        return ResultadoVerificacao("corrompido", "arquivo vazio")

    estrut = verificar_estrutural(dados, ext) if ext else None

    # parcial: mecanismo real — metadados indicavam mais bytes do que foi
    # fisicamente possível recuperar
    if tamanho_esperado and tamanho_esperado > n:
        fracao = n / tamanho_esperado
        base = estrut.motivo if estrut else ""
        return ResultadoVerificacao(
            "parcial",
            f"recuperados {n} de {tamanho_esperado} bytes" + (f" ({base})" if base else ""),
            fracao=fracao, verificador=(estrut.verificador if estrut else ""))

    if ext in VIDEOS:
        # sem decodificador de vídeo: só estrutura
        if estrut is None:
            return ResultadoVerificacao("nao_verificado",
                                        "vídeo sem verificador disponível",
                                        fracao=1.0)
        if estrut.estado == "corrompido":
            return estrut
        return ResultadoVerificacao(
            "nao_verificado",
            "estrutura de vídeo válida; conteúdo não decodificado (sem garantia)",
            fracao=1.0, verificador=estrut.verificador,
            diagnostico=estrut.diagnostico)

    if ext in SO_ESTRUTURAL:
        if estrut and estrut.estado == "corrompido":
            return estrut
        if estrut and estrut.estado == "parcial":
            # a própria estrutura declara dados faltando: nunca afirmar 'ok'
            return estrut
        if ext in ("psd",) or ext.startswith(("cr", "nef", "nrw", "arw", "dng",
                                              "orf", "raf", "rw2", "pef", "srw")):
            pil = verificar_pillow(dados, ext) if usar_pillow else None
            if pil and pil.estado == "ok":
                return ResultadoVerificacao("ok", "estrutura válida + Pillow",
                                            fracao=1.0,
                                            verificador=(estrut.verificador if estrut else "") + "+pillow")
            return ResultadoVerificacao(
                "nao_verificado",
                "formato profissional/RAW sem verificador completo; estrutura "
                + ("válida" if estrut and estrut.estado == "ok" else "não confirmada"),
                fracao=1.0, verificador=estrut.verificador if estrut else "")
        if ext == "avif" and PIL_DISPONIVEL:
            pil = verificar_pillow(dados, ext) if usar_pillow else None
            if pil:
                if pil.estado == "ok":
                    return ResultadoVerificacao("ok", "AVIF decodificado pelo Pillow",
                                                fracao=1.0, verificador="pillow")
                return ResultadoVerificacao("nao_verificado",
                                            "Pillow não conseguiu decodificar o AVIF "
                                            "(pode faltar suporte)", fracao=1.0)
        return ResultadoVerificacao("nao_verificado",
                                    "sem verificador suficiente para este formato",
                                    fracao=1.0)

    # formatos com verificação completa: estrutura + Pillow
    if estrut is not None and estrut.estado == "corrompido":
        return estrut
    if estrut is not None and estrut.estado == "parcial":
        # estrutura incompleta (ex.: JPEG sem EOI): o Pillow costuma exibir a
        # parte presente, mas o arquivo NÃO está completo — mantém 'parcial'
        pil = verificar_pillow(dados, ext) if usar_pillow else None
        nota = ""
        verificador = estrut.verificador
        if pil and pil.estado == "ok":
            nota = " (o decodificador ainda exibe a parte recuperada)"
            verificador += "+pillow"
        return ResultadoVerificacao(
            "parcial", (estrut.motivo or "estrutura incompleta") + nota,
            fracao=estrut.fracao, verificador=verificador,
            diagnostico=estrut.diagnostico)
    pil = verificar_pillow(dados, ext) if usar_pillow else None
    if pil is None:
        if estrut is not None:
            if estrut.estado == "ok":
                return ResultadoVerificacao(
                    "ok", "verificação estrutural (sem Pillow disponível)"
                    if not PIL_DISPONIVEL else estrut.motivo,
                    fracao=estrut.fracao, verificador=estrut.verificador,
                    diagnostico=estrut.diagnostico)
            return estrut
        return ResultadoVerificacao("nao_verificado",
                                    "extensão sem verificador implementado", fracao=1.0)
    if pil.estado == "corrompido":
        # Pillow falhou: se a estrutura diz ok, mantém corrompido apenas para
        # formatos Pillow-confiáveis
        if ext in PILLOW_CONFIÁVEIS or ext in ("jpg", "jpeg"):
            return pil
        if estrut and estrut.estado == "ok":
            return ResultadoVerificacao("nao_verificado",
                                        "estrutura válida, mas Pillow não decodificou "
                                        f"({pil.motivo})", fracao=1.0)
        return pil
    # ambos ok
    motivo = "estrutura + decodificação Pillow"
    if estrut and estrut.motivo:
        motivo += f"; {estrut.motivo}"
    return ResultadoVerificacao("ok", motivo, fracao=1.0,
                                verificador="estrutural+pillow",
                                diagnostico=estrut.diagnostico if estrut else {})


# ---------------------------------------------------------------------------
# EXIF — data original (para filtro por período quando os metadados do FS
# não têm data)
# ---------------------------------------------------------------------------


def data_exif(dados: bytes) -> tuple | None:
    """Extrai DateTimeOriginal (0x9003) ou DateTime (0x0132) de JPEG/TIFF.
    Retorna (ano, mes, dia, h, m, s) ou None. Implementação própria, sem
    dependências."""
    for inicio, endian in _localizar_tiff_em_jpeg(dados):
        dt = _data_em_tiff(dados, inicio, endian)
        if dt:
            return dt
    if dados[:4] in (b"II*\x00", b"MM\x00*"):
        dt = _data_em_tiff(dados, 0, "<" if dados[0] == 0x49 else ">")
        if dt:
            return dt
    return None


def _localizar_tiff_em_jpeg(dados: bytes):
    if dados[:3] != b"\xFF\xD8\xFF":
        return []
    i = 2
    n = len(dados)
    while i + 4 < n:
        if dados[i] != 0xFF:
            i += 1
            continue
        m = dados[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA or m == 0xD9:
            break
        if i + 4 > n:
            break
        (comp,) = struct.unpack_from(">H", dados, i + 2)
        if comp < 2 or i + 2 + comp > n:
            break
        if m == 0xE1 and dados[i + 4:i + 10] == b"Exif\x00\x00":
            tiff = i + 10
            if tiff + 8 <= n:
                if dados[tiff:tiff + 2] == b"II":
                    yield (tiff, "<")
                elif dados[tiff:tiff + 2] == b"MM":
                    yield (tiff, ">")
        i += 2 + comp


def _data_em_tiff(dados: bytes, base: int, endian: str):
    from datetime import datetime

    def u16(o):
        return struct.unpack_from(endian + "H", dados, base + o)[0]

    def u32(o):
        return struct.unpack_from(endian + "I", dados, base + o)[0]
    try:
        ifd = u32(4)
        if ifd < 8 or ifd > len(dados) - base:
            return None
        achados = {}

        def varrer(off_ifd, profundidade=0):
            if off_ifd + 2 > len(dados) - base or profundidade > 2:
                return
            n_ent = u16(off_ifd)
            if not (0 < n_ent <= 512):
                return
            exif_ifd = None
            for k in range(n_ent):
                e = off_ifd + 2 + k * 12
                if e + 12 > len(dados) - base:
                    break
                tag = u16(e)
                tipo = u16(e + 2)
                cnt = u32(e + 4)
                if tag == 0x8769 and cnt == 1:
                    exif_ifd = u32(e + 8)
                if tag in (0x9003, 0x0132, 0x9004) and tipo == 2 and cnt == 20:
                    ofs = u32(e + 8) if cnt > 4 else e + 8
                    if ofs + 20 <= len(dados) - base:
                        txt = dados[base + ofs:base + ofs + 19].decode("ascii", "replace")
                        try:
                            dt = datetime.strptime(txt, "%Y:%m:%d %H:%M:%S")
                            achados.setdefault(tag, dt)
                        except ValueError:
                            pass
            if exif_ifd:
                varrer(exif_ifd, profundidade + 1)
        varrer(ifd)
        for tag in (0x9003, 0x9004, 0x0132):
            if tag in achados:
                dt = achados[tag]
                return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    except (struct.error, IndexError, ValueError):
        pass
    return None
