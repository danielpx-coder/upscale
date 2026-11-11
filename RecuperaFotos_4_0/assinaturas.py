"""
assinaturas.py — Varredura por assinatura (file carving) com análise
estrutural para determinação do tamanho.

Cada formato tem um detector que interpreta a estrutura real (marcadores JPEG,
chunks PNG com CRC, caixas ISO-BMFF, IFDs TIFF, RIFF, pacotes MPEG-TS,
EBML/Matroska, seções PSD, cabeçalho RAF) e decide o comprimento. Quando o
comprimento não pode ser determinado com segurança, o resultado é marcado como
truncado/não determinável — nunca salvamos uma sequência arbitrária de bytes
só porque a assinatura foi encontrada.

A varredura é sequencial, em blocos com sobreposição, para capturar
cabeçalhos que cruzem limites de leitura.
"""

from __future__ import annotations

import binascii
import struct

# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


class ResultadoCarving:
    def __init__(self, comprimento: int | None, formato: str, extensao: str,
                 truncado: bool = False, diagnostico: dict | None = None,
                 motivo: str = ""):
        self.comprimento = comprimento    # None = não determinável
        self.formato = formato            # 'jpeg','png',...
        self.extensao = extensao          # extensão sugerida
        self.truncado = truncado          # True = estrutura incompleta/incoerente
        self.diagnostico = diagnostico or {}
        self.motivo = motivo

    def __repr__(self):
        return f"ResultadoCarving({self.formato}, {self.comprimento}, trunc={self.truncado})"


MIN_TAMANHOS = {
    "jpeg": 128, "png": 60, "gif": 30, "bmp": 70, "tiff": 64, "raf": 256,
    "psd": 128, "webp": 30, "avi": 32, "mp4": 32, "mpegts": 188 * 6,
    "mkv": 64,
}
MAX_CARV = 2 * 1024 * 1024 * 1024  # teto defensivo por arquivo
# dentro do fluxo entropy de um JPEG, bytes 0xFF aparecem com frequência ~1/256
# (byte stuffing e marcadores RST). 64 KB sem nenhum 0xFF não é entropy: é
# preenchimento ou outro arquivo — a imagem terminou antes disso.
LIMIAR_SEM_FF = 64 * 1024


def _ler(leitor, off: int, tam: int) -> bytes:
    if tam <= 0:
        return b""
    try:
        return leitor.read(off, tam)
    except (OSError, ValueError):
        return b""


def _ctx(leitor, off: int, contexto: bytes | None, minimo: int) -> bytes:
    """Garante um buffer inicial de pelo menos 'minimo' bytes."""
    if contexto is None or len(contexto) < minimo:
        return _ler(leitor, off, max(minimo, 1 << 12))
    return contexto


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

MARCADORES_SOZINHOS = set(range(0xD0, 0xD8)) | {0x01}


def analisar_jpeg(leitor, off: int, fim_regiao: int | None = None,
                  contexto: bytes | None = None) -> ResultadoCarving | None:
    """Percorre os marcadores JPEG até o EOI. Diagnóstico: DRI, sequência de
    RST, MPF (APP2), Motion Photo (XMP) e miniatura EXIF."""
    buf = bytearray(_ctx(leitor, off, contexto, 64))
    if len(buf) < 4 or bytes(buf[0:3]) != b"\xFF\xD8\xFF":
        return None
    diag = {"dri": None, "rst_vistos": [], "rst_quebras": 0, "mpf": None,
            "motion_video_offset": None, "miniatura_exif": None,
            "marcadores": []}
    i = 2
    pos = off
    sos_visto = False
    ultimo_rst = None
    fim = None

    def garantir(ate_abs: int) -> bool:
        nonlocal buf
        necessidade = ate_abs - off
        while len(buf) < necessidade and necessidade <= (1 << 24):
            extra = _ler(leitor, off + len(buf), max(necessidade - len(buf), 1 << 16))
            if not extra:
                break
            buf += extra
        return len(buf) >= necessidade

    while True:
        if fim_regiao and pos >= fim_regiao:
            break
        if not garantir(pos + 4):
            break
        if buf[i] != 0xFF:
            if not sos_visto:
                return None
            break
        while i + 1 < len(buf) and buf[i + 1] == 0xFF:
            i += 1
            pos += 1
        if not garantir(pos + 4):
            break
        marcador = buf[i + 1]
        if marcador == 0x00:
            i += 1
            pos += 1
            continue
        if marcador == 0xD9:  # EOI
            fim = pos + 2
            break
        if marcador in MARCADORES_SOZINHOS:
            if 0xD0 <= marcador <= 0xD7:
                diag["rst_vistos"].append(marcador)
                if ultimo_rst is not None:
                    esperado = 0xD0 + ((ultimo_rst - 0xD0 + 1) % 8)
                    if marcador != esperado:
                        diag["rst_quebras"] += 1
                ultimo_rst = marcador
            i += 2
            pos += 2
            continue
        if not garantir(pos + 4):
            break
        (comp,) = struct.unpack_from(">H", buf, i + 2)
        if comp < 2:
            return ResultadoCarving(None, "jpeg", "jpg", truncado=True,
                                    diagnostico=diag,
                                    motivo=f"segmento 0x{marcador:02X} com tamanho inválido")
        seg_ini_abs = pos + 4
        seg_tam_dados = comp - 2
        diag["marcadores"].append(marcador)
        if marcador == 0xDD and garantir(seg_ini_abs + 2):
            (intervalo,) = struct.unpack_from(">H", buf, i + 4)
            diag["dri"] = intervalo
        elif marcador == 0xE2:
            if garantir(seg_ini_abs + 4) and bytes(buf[i + 4:i + 8]) == b"MPF\x00":
                diag["mpf"] = _parsear_mpf(leitor, off, bytes(buf), i + 8,
                                           seg_tam_dados - 4)
        elif marcador == 0xE1:
            if garantir(seg_ini_abs + 6):
                if bytes(buf[i + 4:i + 10]) == b"Exif\x00\x00":
                    mini = _miniatura_exif(bytes(buf), i + 10, seg_tam_dados - 6)
                    if mini:
                        diag["miniatura_exif"] = mini
                else:
                    mv = _motion_video_xmp(bytes(buf), i + 4, seg_tam_dados)
                    if mv:
                        diag["motion_video_offset"] = mv
        elif marcador == 0xDA:  # SOS: varre o fluxo entropy até o EOI
            sos_visto = True
            i += 2 + comp
            pos += 2 + comp
            fim = _procurar_eoi(leitor, pos, fim_regiao, diag, ultimo_rst)
            break
        i += 2 + comp
        pos += 2 + comp
        if pos - off > MAX_CARV:
            break
    if fim is None:
        # sem EOI: o fluxo entropy foi interrompido (cartão truncado, clusters
        # sobrescritos). Ainda assim há bytes de imagem aproveitáveis — devolve
        # o comprimento recuperado com truncado=True para virar 'parcial'.
        parcial_ate = diag.get("parcial_ate") or (pos if sos_visto else 0)
        if parcial_ate:
            limite_leitor = getattr(leitor, "tamanho", None)
            if limite_leitor:
                parcial_ate = min(parcial_ate, limite_leitor)
            if fim_regiao:
                parcial_ate = min(parcial_ate, fim_regiao)
        if sos_visto and parcial_ate - off >= MIN_TAMANHOS["jpeg"]:
            diag["sem_eoi"] = True
            return ResultadoCarving(parcial_ate - off, "jpeg", "jpg",
                                    truncado=True, diagnostico=diag,
                                    motivo="EOI não encontrado — dados de imagem "
                                           "incompletos (recuperado até o fim do "
                                           "fluxo entropy)")
        return ResultadoCarving(None, "jpeg", "jpg", truncado=True,
                                diagnostico=diag, motivo="EOI não encontrado")
    tam = fim - off
    if tam < MIN_TAMANHOS["jpeg"]:
        return ResultadoCarving(None, "jpeg", "jpg", truncado=True,
                                diagnostico=diag, motivo="tamanho mínimo não atingido")
    truncado = diag["rst_quebras"] > 0
    motivo = (f"{diag['rst_quebras']} quebra(s) na sequência de marcadores RST"
              if truncado else "")
    return ResultadoCarving(tam, "jpeg", "jpg", truncado=truncado,
                            diagnostico=diag, motivo=motivo)


def _marca_parcial(diag: dict, ate: int) -> None:
    """Registra em diag até onde os dados de imagem eram válidos."""
    if ate and ate > diag.get("parcial_ate", 0):
        diag["parcial_ate"] = ate


def _procurar_eoi(leitor, pos: int, fim_regiao: int | None, diag: dict,
                  ultimo_rst: int | None) -> int | None:
    """Varre o fluxo entropy procurando FFD9 e conferindo a sequência dos RST.
    Em dados entropy, bytes 0xFF são seguidos de 0x00 (stuffing), então FFD9
    não aparece por acidente; RST são marcadores legítimos no fluxo.

    Quando o EOI não é encontrado, diag["parcial_ate"] registra a posição
    absoluta até onde os dados de imagem eram válidos (permite recuperação
    parcial em vez de descartar o arquivo)."""
    passo = 1 << 22
    pendente = b""          # bytes já lidos, ainda não processados (<=3)
    pos_leitura = pos       # próxima posição absoluta a ler
    sem_ff = 0              # bytes de "entropy" sem nenhum 0xFF
    fim_util = pos          # fim confirmado dos dados de imagem
    while True:
        if fim_regiao is not None and pos_leitura >= fim_regiao:
            _marca_parcial(diag, min(fim_util, fim_regiao))
            return None
        bloco = _ler(leitor, pos_leitura, passo)
        if not bloco:
            _marca_parcial(diag, fim_util)
            return None
        buf = pendente + bloco
        buf_base = pos_leitura - len(pendente)
        pendente = b""
        pos_leitura = buf_base + len(buf)
        k = 0
        n = len(buf)
        while k < n:
            j = buf.find(b"\xFF", k)
            if j < 0:
                sem_ff += n - k
                k = n
                break
            sem_ff += j - k
            if sem_ff > LIMIAR_SEM_FF:
                # dezenas de KB sem um único 0xFF não são dados entropy de um
                # JPEG (é preenchimento/outro arquivo): a imagem terminou antes
                _marca_parcial(diag, fim_util)
                return None
            if j + 1 >= n:
                # marcador no limite: guarda o byte final para o próximo bloco
                pendente = bytes(buf[j:])
                fim_util = buf_base + j + 1
                k = n
                break
            m = buf[j + 1]
            if m == 0xD9:
                return buf_base + j + 2
            if m == 0x00 or m == 0xFF:
                fim_util = buf_base + j + 2
                sem_ff = 0
                k = j + 1
                continue
            if 0xD0 <= m <= 0xD7:
                if ultimo_rst is not None:
                    esperado = 0xD0 + ((ultimo_rst - 0xD0 + 1) % 8)
                    if m != esperado:
                        diag["rst_quebras"] += 1
                ultimo_rst = m
                diag["rst_vistos"].append(m)
                fim_util = buf_base + j + 2
                sem_ff = 0
                k = j + 2
                continue
            if m == 0x01:
                fim_util = buf_base + j + 2
                sem_ff = 0
                k = j + 2
                continue
            # marcador com payload inesperado dentro do entropy: estrutura
            # divergente (possível início de outro arquivo)
            _marca_parcial(diag, buf_base + j)
            return None
        pos_leitura = buf_base + n
        if pos_leitura - pos > MAX_CARV:
            _marca_parcial(diag, fim_util)
            return None


def _parsear_mpf(leitor, off_base: int, buf: bytes, i_mpf: int,
                 tam_seg: int) -> dict | None:
    """Índice MPF (APP2 'MPF\\0'): TIFF com offsets relativos ao início do
    conteúdo MPF. Retorna {'imagens': [(off_abs, tam)], 'valido', 'motivo'}."""
    dados = buf[i_mpf:i_mpf + tam_seg]
    if len(dados) < tam_seg:
        dados += _ler(leitor, off_base + i_mpf + len(dados), tam_seg - len(dados))
    if len(dados) < 16:
        return None
    if dados[0:2] == b"II":
        endian = "<"
    elif dados[0:2] == b"MM":
        endian = ">"
    else:
        return {"imagens": [], "valido": False, "motivo": "endianess MPF inválida"}
    try:
        magic = struct.unpack_from(endian + "H", dados, 2)[0]
        ifd_off = struct.unpack_from(endian + "I", dados, 4)[0]
        if magic != 42 or ifd_off + 2 > len(dados) or ifd_off < 8:
            return {"imagens": [], "valido": False, "motivo": "IFD MPF inválido"}
        (n,) = struct.unpack_from(endian + "H", dados, ifd_off)
        if not (0 < n <= 64) or ifd_off + 2 + n * 12 > len(dados):
            return {"imagens": [], "valido": False, "motivo": "IFD MPF truncado"}
        n_individuais = off_individuais = None
        for k in range(n):
            e = ifd_off + 2 + k * 12
            tag, tipo, cnt = struct.unpack_from(endian + "HHI", dados, e)
            if tipo == 4 and cnt == 1:
                val = struct.unpack_from(endian + "I", dados, e + 8)[0]
                if tag == 0xB001:      # NumberOfImages
                    n_individuais = val
                elif tag == 0xB002:    # MPEntry (offset da lista de entradas)
                    off_individuais = val
        if n_individuais is None or off_individuais is None:
            return {"imagens": [], "valido": False,
                    "motivo": "tags obrigatórias do MPF (0xB001/0xB002) ausentes"}
        if not (0 < n_individuais <= 64):
            return {"imagens": [], "valido": False,
                    "motivo": "contagem de imagens MPF inválida"}
        if off_individuais + 16 > len(dados):
            return {"imagens": [], "valido": False,
                    "motivo": "offset da lista de imagens fora do segmento"}
        # offsets do MPF são relativos ao início do cabeçalho MPF ('MPF\0'),
        # que começa 4 bytes antes do TIFF
        base_mpf = off_base + i_mpf - 4
        limite = getattr(leitor, "tamanho", None) or None
        imagens = []
        anterior = None
        p = off_individuais
        for k in range(n_individuais):
            if p + 16 > len(dados):
                return {"imagens": imagens, "valido": False,
                        "motivo": "lista de imagens individuais truncada"}
            _atr, tam, off_img, _dep = struct.unpack_from(endian + "IIHH", dados, p)
            if tam == 0 or tam > MAX_CARV:
                return {"imagens": imagens, "valido": False,
                        "motivo": f"tamanho inválido na imagem individual {k + 1} "
                                  f"({tam} bytes)"}
            off_abs = off_base if k == 0 else base_mpf + off_img
            # validação real do índice: cada imagem precisa caber no arquivo e
            # vir depois da anterior (índices corrompidos apontam para fora)
            if k > 0 and off_img <= 0:
                return {"imagens": imagens, "valido": False,
                        "motivo": f"offset da imagem individual {k + 1} inválido "
                                  f"({off_img})"}
            if off_abs < off_base or (limite and off_abs + tam > limite) \
                    or off_abs + tam > off_base + MAX_CARV:
                return {"imagens": imagens, "valido": False,
                        "motivo": f"imagem individual {k + 1} aponta para fora do "
                                  f"arquivo (offset {off_abs}, {tam} bytes)"}
            if anterior is not None and off_abs < anterior:
                return {"imagens": imagens, "valido": False,
                        "motivo": "índice MPF com imagens fora de ordem"}
            anterior = off_abs + tam
            imagens.append((off_abs, tam))
            p += 16
        valido = bool(imagens)
        return {"imagens": imagens, "valido": valido,
                "motivo": "" if valido else "índice MPF vazio"}
    except struct.error:
        return {"imagens": [], "valido": False, "motivo": "erro de estrutura MPF"}


def _miniatura_exif(buf: bytes, i: int, tam: int) -> tuple[int, int] | None:
    """Miniatura JPEG do EXIF (tags 0x0201/0x0202), incluindo IFD1."""
    dados = buf[i:i + tam]
    if len(dados) < 8:
        return None
    endian = "<" if dados[0:2] == b"II" else ">" if dados[0:2] == b"MM" else None
    if endian is None:
        return None

    def varre_ifd(ifd: int):
        if ifd + 2 > len(dados):
            return None, 0
        try:
            n = struct.unpack_from(endian + "H", dados, ifd)[0]
        except struct.error:
            return None, 0
        if not (0 < n <= 256) or ifd + 2 + n * 12 > len(dados):
            return None, 0
        off_mini = tam_mini = None
        for k in range(n):
            e = ifd + 2 + k * 12
            try:
                tag = struct.unpack_from(endian + "H", dados, e)[0]
                val = struct.unpack_from(endian + "I", dados, e + 8)[0]
            except struct.error:
                break
            if tag == 0x0201:
                off_mini = val
            elif tag == 0x0202:
                tam_mini = val
        prox = 0
        e = ifd + 2 + n * 12
        if e + 4 <= len(dados):
            prox = struct.unpack_from(endian + "I", dados, e)[0]
        return (off_mini, tam_mini), prox

    try:
        ifd0 = struct.unpack_from(endian + "I", dados, 4)[0]
        resultado, prox = varre_ifd(ifd0)
        if resultado and resultado[0] is not None and resultado[1]:
            off_mini, tam_mini = resultado
            if off_mini + tam_mini <= len(dados):
                return (off_mini, tam_mini)
        if prox:
            resultado, _ = varre_ifd(prox)
            if resultado and resultado[0] is not None and resultado[1]:
                off_mini, tam_mini = resultado
                if off_mini + tam_mini <= len(dados):
                    return (off_mini, tam_mini)
    except struct.error:
        pass
    return None


def _motion_video_xmp(buf: bytes, i: int, tam: int) -> int | None:
    """Extrai MicroVideoOffset/MotionPhotoOffset (Google) do XMP."""
    seg = buf[i:i + tam]
    for chave in (b"MicroVideoOffset=\"", b"MotionPhotoOffset=\""):
        p = seg.find(chave)
        if p >= 0:
            q = p + len(chave)
            num = b""
            while q < len(seg) and 48 <= seg[q] <= 57:
                num += seg[q:q + 1]
                q += 1
            if num:
                v = int(num)
                if 0 < v < MAX_CARV:
                    return v
    return None


# ---------------------------------------------------------------------------
# PNG / GIF / BMP
# ---------------------------------------------------------------------------


def analisar_png(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = bytearray(_ctx(leitor, off, contexto, 64))
    if bytes(buf[:8]) != b"\x89PNG\r\n\x1a\n":
        return None
    n = len(buf)
    i = 8
    pos = off + 8
    tem_iend = False
    crc_ruim = 0
    while pos - off < MAX_CARV:
        if i + 8 > n:
            extra = _ler(leitor, off + n, 1 << 16)
            if not extra:
                break
            buf += extra
            n = len(buf)
            continue
        (comp,) = struct.unpack_from(">I", buf, i)
        tipo = bytes(buf[i + 4:i + 8])
        if comp > (1 << 31) or not _tipo_chunk_valido(tipo):
            return ResultadoCarving(None, "png", "png", truncado=True,
                                    motivo=f"chunk inválido em {pos}")
        if i + 12 + comp > n:
            extra = _ler(leitor, off + n, min(comp + 64, 1 << 20))
            if not extra:
                return ResultadoCarving(None, "png", "png", truncado=True,
                                        motivo="chunk além do fim da região legível")
            buf += extra
            n = len(buf)
            continue
        dados = bytes(buf[i + 8:i + 8 + comp])
        crc_arm = struct.unpack_from(">I", buf, i + 8 + comp)[0]
        if binascii.crc32(tipo + dados) & 0xFFFFFFFF != crc_arm:
            crc_ruim += 1
        i += 12 + comp
        pos += 12 + comp
        if tipo == b"IEND":
            tem_iend = True
            break
    if not tem_iend:
        return ResultadoCarving(None, "png", "png", truncado=True,
                                diagnostico={"crc_invalidos": crc_ruim},
                                motivo="IEND não encontrado")
    truncado = crc_ruim > 0
    return ResultadoCarving(pos - off, "png", "png", truncado=truncado,
                            diagnostico={"crc_invalidos": crc_ruim},
                            motivo=f"{crc_ruim} chunk(s) com CRC inválido" if crc_ruim else "")


def _tipo_chunk_valido(t: bytes) -> bool:
    return len(t) == 4 and all(65 <= b <= 90 or 97 <= b <= 122 for b in t)


def analisar_gif(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = bytearray(_ctx(leitor, off, contexto, 64))
    if bytes(buf[:6]) not in (b"GIF87a", b"GIF89a"):
        return None

    def garantir(qtd: int) -> bool:
        nonlocal buf
        while len(buf) < qtd and len(buf) < MAX_CARV:
            extra = _ler(leitor, off + len(buf), 1 << 16)
            if not extra:
                return len(buf) >= qtd
            buf += extra
        return len(buf) >= qtd

    if not garantir(13):
        return None
    tem_gct = bool(buf[10] & 0x80)
    tam_gct = 3 * (2 << (buf[10] & 0x07)) if tem_gct else 0
    i = 13 + tam_gct
    while i < MAX_CARV:
        if not garantir(i + 1):
            break
        b = buf[i]
        if b == 0x3B:  # trailer (pode ser o último byte legível da região)
            return ResultadoCarving(i + 1, "gif", "gif")
        if not garantir(i + 2):
            break
        if b == 0x21:  # extensão: label + sub-blocos
            i += 2
            while True:
                if not garantir(i + 1):
                    return ResultadoCarving(None, "gif", "gif", truncado=True,
                                            motivo="extensão truncada")
                if buf[i] == 0:
                    i += 1
                    break
                i += buf[i] + 1
            continue
        if b == 0x2C:  # descritor de imagem
            if not garantir(i + 10):
                break
            tem_lct = bool(buf[i + 9] & 0x80)
            tam_lct = 3 * (2 << (buf[i + 9] & 0x07)) if tem_lct else 0
            i += 10 + tam_lct + 1  # + LZW minimum code size
            while True:
                if not garantir(i + 1):
                    return ResultadoCarving(None, "gif", "gif", truncado=True,
                                            motivo="sub-blocos de imagem truncados")
                if buf[i] == 0:
                    i += 1
                    break
                i += buf[i] + 1
            continue
        return ResultadoCarving(None, "gif", "gif", truncado=True,
                                motivo=f"bloco inesperado 0x{b:02X}")
    return ResultadoCarving(None, "gif", "gif", truncado=True,
                            motivo="trailer 0x3B não encontrado")


def analisar_bmp(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = _ctx(leitor, off, contexto, 54)
    if buf[:2] != b"BM" or len(buf) < 54:
        return None
    (tamanho,) = struct.unpack_from("<I", buf, 2)
    (cab_tam,) = struct.unpack_from("<I", buf, 14)
    if cab_tam not in (12, 40, 52, 56, 108, 124):
        return None
    if cab_tam >= 40:
        largura, altura = struct.unpack_from("<ii", buf, 18)
        (bpp,) = struct.unpack_from("<H", buf, 28)
        (compressao,) = struct.unpack_from("<I", buf, 30)
        if not (0 < abs(largura) <= 100000 and 0 < abs(altura) <= 100000):
            return None
        if bpp not in (1, 4, 8, 16, 24, 32):
            return None
        if compressao > 6:
            return None
    if tamanho < 54 or tamanho > MAX_CARV:
        return None
    return ResultadoCarving(tamanho, "bmp", "bmp")


# ---------------------------------------------------------------------------
# TIFF / RAW (CR2, NEF, ARW, DNG, ORF, RW2, NRW, PEF, SRW...)
# ---------------------------------------------------------------------------


def analisar_tiff(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = bytearray(_ctx(leitor, off, contexto, 1 << 14))
    if len(buf) < 8:
        return None
    magic4 = bytes(buf[:4])
    if magic4 in (b"II*\x00", b"MM\x00*"):
        endian = "<" if magic4[0] == 0x49 else ">"
        variante = "tiff"
    elif magic4 in (b"IIRO", b"IIRS"):
        endian, variante = "<", "orf"
    elif magic4 == b"IIU\x00":
        endian, variante = "<", "rw2"
    else:
        return None
    n = len(buf)

    def garantir(ate: int):
        nonlocal buf, n
        tentativas = 0
        while ate > n and n < (1 << 22) and tentativas < 64:
            extra = _ler(leitor, off + n, max(min(ate - n, 1 << 18), 1 << 14))
            if not extra:
                break
            buf += extra
            n = len(buf)
            tentativas += 1
        return ate <= n

    try:
        (ifd0,) = struct.unpack_from(endian + "I", buf, 4)
    except struct.error:
        return None
    if ifd0 < 8 or ifd0 >= MAX_CARV or not garantir(ifd0 + 2):
        return ResultadoCarving(None, variante, variante, truncado=True,
                                motivo="offset de IFD0 inválido")
    max_fim = 8
    dng = cr2 = False
    if variante == "tiff":
        dng = _tiff_tem_tag(bytes(buf), ifd0, endian, 0xC612)
        cr2 = len(buf) > 12 and bytes(buf[8:10]) == b"CR" and buf[10] == 2
    filas = [(ifd0, 0)]
    vistos = set()
    while filas:
        ifd, profundidade = filas.pop(0)
        if ifd in vistos or profundidade > 4:
            continue
        vistos.add(ifd)
        if not garantir(ifd + 2):
            break
        try:
            (n_entradas,) = struct.unpack_from(endian + "H", buf, ifd)
        except struct.error:
            break
        if not (0 < n_entradas <= 1024) or not garantir(ifd + 2 + n_entradas * 12 + 4):
            break
        strips_off: list[int] = []
        strips_len: list[int] = []
        jpeg_thumb: tuple[int, int] | None = None
        for k in range(n_entradas):
            e = ifd + 2 + k * 12
            try:
                tag, tipo, cnt = struct.unpack_from(endian + "HHI", buf, e)
            except struct.error:
                break
            tam_tipo = _tamanho_tipo_tiff(tipo)
            if tam_tipo == 0 or cnt > (1 << 20):
                continue
            total = tam_tipo * cnt
            if total > 4:
                try:
                    (v_ofs,) = struct.unpack_from(endian + "I", buf, e + 8)
                except struct.error:
                    continue
                if v_ofs >= MAX_CARV:
                    continue
                fim_v = v_ofs + total
                if fim_v > max_fim:
                    max_fim = fim_v
                if tag in (0x14A, 0x8769, 0x8825) and total % 4 == 0 and garantir(v_ofs + total):
                    for q in range(min(cnt, 8)):
                        (sub,) = struct.unpack_from(endian + "I", buf, v_ofs + q * 4)
                        if 8 <= sub < MAX_CARV:
                            filas.append((sub, profundidade + 1))
            # dados de imagem referenciados por strips/tiles
            if tag in (273, 324):        # StripOffsets / TileOffsets
                vals = _tiff_valores(buf, endian, e, tipo, cnt, total, garantir)
                if vals:
                    strips_off.extend(vals[:4096])
            elif tag in (279, 325):      # StripByteCounts / TileByteCounts
                vals = _tiff_valores(buf, endian, e, tipo, cnt, total, garantir)
                if vals:
                    strips_len.extend(vals[:4096])
            elif tag == 201:             # JPEGInterchangeFormat (miniatura)
                vals = _tiff_valores(buf, endian, e, tipo, cnt, total, garantir)
                if vals:
                    jpeg_thumb = (vals[0], 0)
            elif tag == 202 and jpeg_thumb:
                vals = _tiff_valores(buf, endian, e, tipo, cnt, total, garantir)
                if vals:
                    jpeg_thumb = (jpeg_thumb[0], vals[0])
        for o, c in zip(strips_off, strips_len):
            if 0 < o < MAX_CARV and 0 < c <= MAX_CARV:
                fim = o + c
                if fim > max_fim:
                    max_fim = fim
        if jpeg_thumb and jpeg_thumb[1]:
            fim = jpeg_thumb[0] + jpeg_thumb[1]
            if 0 < fim <= MAX_CARV and fim > max_fim:
                max_fim = fim
        fim_ifd = ifd + 2 + n_entradas * 12 + 4
        if fim_ifd > max_fim:
            max_fim = fim_ifd
        try:
            (prox,) = struct.unpack_from(endian + "I", buf, fim_ifd - 4)
        except struct.error:
            prox = 0
        if 8 <= prox < MAX_CARV:
            filas.append((prox, profundidade + 1))
    ext = {"tiff": "tif", "orf": "orf", "rw2": "rw2"}[variante]
    fmt = variante
    if dng:
        ext, fmt = "dng", "dng"
    elif cr2:
        ext, fmt = "cr2", "cr2"
    if max_fim < MIN_TAMANHOS["tiff"]:
        return ResultadoCarving(None, fmt, ext, truncado=True,
                                motivo="estrutura TIFF sem dados referenciados")
    return ResultadoCarving(max_fim, fmt, ext,
                            motivo="tamanho determinado por strips/tiles e IFDs")


def _tiff_valores(buf, endian: str, e: int, tipo: int, cnt: int, total: int,
                  garantir, limite: int = 8192) -> list[int]:
    """Lê os valores inteiros de uma entrada de IFD (inline se total <= 4,
    senão a partir do deslocamento indicado)."""
    formato = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i"}.get(tipo)
    if formato is None:
        return []
    tam = {1: 1, 3: 2, 4: 4, 6: 1, 8: 2, 9: 4}[tipo]
    vals = []
    try:
        if total <= 4:
            for q in range(cnt):
                (v,) = struct.unpack_from(endian + formato, buf, e + 8 + q * tam)
                vals.append(v)
        else:
            (ofs,) = struct.unpack_from(endian + "I", buf, e + 8)
            if ofs >= MAX_CARV or not garantir(ofs + total):
                return []
            for q in range(min(cnt, limite)):
                (v,) = struct.unpack_from(endian + formato, buf, ofs + q * tam)
                vals.append(v)
    except struct.error:
        return vals
    return vals


def _tiff_tem_tag(buf: bytes, ifd: int, endian: str, alvo: int) -> bool:
    try:
        (n,) = struct.unpack_from(endian + "H", buf, ifd)
        for k in range(min(n, 512)):
            e = ifd + 2 + k * 12
            if e + 12 > len(buf):
                break
            if struct.unpack_from(endian + "H", buf, e)[0] == alvo:
                return True
    except struct.error:
        pass
    return False


def _tamanho_tipo_tiff(tipo: int) -> int:
    return {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
            11: 4, 12: 8}.get(tipo, 0)


def analisar_raf(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = _ctx(leitor, off, contexto, 0x78)
    if buf[:15] != b"FUJIFILMCCD-RAW" or len(buf) < 0x74:
        return None
    campos = struct.unpack_from(">IIIIIIII", buf, 0x54)
    max_fim = 0x100
    pares = [(campos[2], campos[3]), (campos[4], campos[5]), (campos[6], campos[7])]
    for o, t in pares:
        if o == 0 or t == 0 or o > MAX_CARV or t > MAX_CARV:
            return ResultadoCarving(None, "raf", "raf", truncado=True,
                                    motivo="campos de offset/tamanho do RAF inválidos")
        max_fim = max(max_fim, o + t)
    return ResultadoCarving(max_fim, "raf", "raf")


# ---------------------------------------------------------------------------
# PSD
# ---------------------------------------------------------------------------


def analisar_psd(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = bytearray(_ctx(leitor, off, contexto, 1 << 12))
    if bytes(buf[:4]) != b"8BPS" or len(buf) < 26:
        return None
    (versao,) = struct.unpack_from(">H", buf, 4)
    if versao not in (1, 2) or bytes(buf[6:12]) != b"\x00" * 6:
        return None
    canais, altura, largura, prof, modo = struct.unpack_from(">HIIHH", buf, 12)
    if not (1 <= canais <= 56) or altura == 0 or largura == 0:
        return None
    if prof not in (1, 8, 16, 32) or modo > 9:
        return None
    if versao == 1 and (altura > 30000 or largura > 30000):
        return None

    def garantir(qtd: int) -> bool:
        while len(buf) < qtd:
            extra = _ler(leitor, off + len(buf), 1 << 14)
            if not extra:
                return False
            buf.extend(extra)
        return True

    i = 26
    for _secao in range(3):
        if not garantir(i + 4):
            return ResultadoCarving(None, "psd", "psd", truncado=True,
                                    motivo="seções truncadas")
        (comp,) = struct.unpack_from(">I", buf, i)
        if comp > MAX_CARV:
            return ResultadoCarving(None, "psd", "psd", truncado=True,
                                    motivo="comprimento de seção implausível")
        i += 4 + comp + (comp % 2)
    if not garantir(i + 2):
        return ResultadoCarving(None, "psd", "psd", truncado=True,
                                motivo="seção de dados ausente")
    (compressao,) = struct.unpack_from(">H", buf, i)
    if compressao == 0:
        bits = max(1, prof // 8)
        tam_dados = canais * altura * largura * bits
        if tam_dados > MAX_CARV:
            return ResultadoCarving(None, "psd", "psd", truncado=True,
                                    motivo="tamanho de dados raw implausível")
        return ResultadoCarving(i + 2 + tam_dados, "psd", "psd",
                                motivo="tamanho calculado pela seção de dados raw")
    if compressao == 1:
        return ResultadoCarving(None, "psd", "psd", truncado=True,
                                motivo="PSD com dados RLE: comprimento não "
                                       "determinável com segurança (não salvo "
                                       "como íntegro)")
    return ResultadoCarving(None, "psd", "psd", truncado=True,
                            motivo=f"compressão desconhecida ({compressao})")


# ---------------------------------------------------------------------------
# RIFF: WEBP / AVI
# ---------------------------------------------------------------------------


def analisar_riff(leitor, off: int, contexto: bytes | None = None) -> ResultadoCarving | None:
    buf = _ctx(leitor, off, contexto, 32)
    if buf[:4] != b"RIFF" or len(buf) < 16:
        return None
    (tam,) = struct.unpack_from("<I", buf, 4)
    forma = buf[8:12]
    if forma == b"WEBP":
        if tam < 4 or tam > MAX_CARV:
            return None
        if buf[12:16] not in (b"VP8 ", b"VP8L", b"VP8X"):
            return None
        return ResultadoCarving(tam + 8 + (tam % 2), "webp", "webp")
    if forma == b"AVI ":
        if 32 <= tam <= MAX_CARV:
            if buf[12:16] not in (b"LIST", b"JUNK", b"AVIX", b"idx1"):
                return ResultadoCarving(None, "avi", "avi", truncado=True,
                                        motivo="estrutura RIFF/AVI inválida")
            return ResultadoCarving(tam + 8 + (tam % 2), "avi", "avi")
        return _riff_varrer(leitor, off)
    return None


def _riff_varrer(leitor, off: int) -> ResultadoCarving | None:
    """AVI com tamanho declarado inválido: fim por varredura de chunks."""
    pos = off + 12
    ultimo_fim = None
    for _ in range(200000):
        cab = _ler(leitor, pos, 8)
        if len(cab) < 8:
            break
        fourcc = cab[:4]
        if not all(32 <= b <= 126 for b in fourcc):
            break
        (comp,) = struct.unpack_from("<I", cab, 4)
        if comp == 0 or comp > MAX_CARV:
            break
        ultimo_fim = pos + 8 + comp + (comp % 2)
        pos = ultimo_fim
    if ultimo_fim:
        return ResultadoCarving(ultimo_fim - off, "avi", "avi", truncado=True,
                                motivo="tamanho RIFF inválido; fim determinado "
                                       "por varredura de chunks")
    return None


# ---------------------------------------------------------------------------
# ISO BMFF: MP4 / MOV / M4V / 3GP / HEIC / AVIF / CR3
# ---------------------------------------------------------------------------

MARCAS_MP4 = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41",
              b"mp42", b"avc1", b"dash", b"MSNV", b"M4V ", b"M4A ", b"f4v ",
              b"qt  ", b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"3ge6", b"3gg6",
              b"mmp4", b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1",
              b"avif", b"avis", b"crx ", b"CAEP", b"CAQT", b"XAVC", b"mp71",
              b"M4VP"}

EXTENSAO_POR_MARCA = {
    b"qt  ": "mov", b"heic": "heic", b"heix": "heic", b"heim": "heic",
    b"heis": "heic", b"mif1": "heic", b"msf1": "heic", b"avif": "avif",
    b"avis": "avif", b"crx ": "cr3", b"M4V ": "m4v", b"M4VP": "m4v",
    b"3gp4": "3gp", b"3gp5": "3gp", b"3gp6": "3gp", b"3g2a": "3g2",
    b"mp42": "mp4", b"mp41": "mp4", b"isom": "mp4", b"iso2": "mp4",
    b"dash": "mp4", b"f4v ": "mp4", b"MSNV": "mp4",
}


def analisar_mp4(leitor, off: int, contexto: bytes | None = None,
                 fim_regiao: int | None = None) -> ResultadoCarving | None:
    buf = _ctx(leitor, off, contexto, 64)
    if len(buf) < 16 or buf[4:8] != b"ftyp":
        return None
    (tam0,) = struct.unpack_from(">I", buf, 0)
    if tam0 < 16 or tam0 > 4096 or tam0 > len(buf):
        # tam0 pode exceder o contexto; relê o necessário
        if tam0 < 16 or tam0 > 4096:
            return None
        buf = _ler(leitor, off, tam0 + 16)
        if len(buf) < tam0:
            return None
    marca = buf[8:12]
    todas = [marca] + [buf[j:j + 4] for j in range(16, tam0 - 3, 4)]
    if not any(m in MARCAS_MP4 for m in todas):
        return None
    ext = "mp4"
    for m in todas:
        if m in EXTENSAO_POR_MARCA:
            ext = EXTENSAO_POR_MARCA[m]
            break
    pos = off
    n_caixas = 0
    tem_mdat = False
    while n_caixas < 20000:
        if fim_regiao and pos >= fim_regiao:
            break
        cab = _ler(leitor, pos, 16)
        if len(cab) < 8:
            break
        (tam,) = struct.unpack_from(">I", cab, 0)
        tipo = cab[4:8]
        cabeca = 8
        if tam == 1:
            if len(cab) < 16:
                break
            (tam,) = struct.unpack_from(">Q", cab, 8)
            cabeca = 16
        elif tam == 0:
            if fim_regiao:
                return ResultadoCarving(fim_regiao - off, "mp4", ext, truncado=True,
                                        motivo="caixa com tamanho 0 (até o fim da região)")
            break
        if tam < cabeca or tam > MAX_CARV or not _tipo_caixa_valido(tipo):
            break
        if fim_regiao and pos + tam > fim_regiao:
            # caixa declarada além do fim da região: comprimento indeterminado
            # a partir daqui — para em segurança no que já foi validado
            if tem_mdat and pos > off:
                return ResultadoCarving(pos - off, "mp4", ext, truncado=True,
                                        motivo="última caixa excede o fim da região")
            return ResultadoCarving(max(pos - off, 0), "mp4", ext, truncado=True,
                                    motivo="caixa excede o fim da região")
        if tipo == b"mdat":
            tem_mdat = True
        pos += tam
        n_caixas += 1
    if n_caixas < 1:
        return None
    if not tem_mdat:
        return ResultadoCarving(pos - off, "mp4", ext, truncado=True,
                                motivo="nenhuma caixa mdat encontrada")
    return ResultadoCarving(pos - off, "mp4", ext)


def _tipo_caixa_valido(t: bytes) -> bool:
    return len(t) == 4 and all(32 <= b <= 126 for b in t)


# ---------------------------------------------------------------------------
# MPEG-TS (MTS / M2TS)
# ---------------------------------------------------------------------------


def analisar_mpegts(leitor, off: int, contexto: bytes | None = None,
                    fim_regiao: int | None = None) -> ResultadoCarving | None:
    buf = _ctx(leitor, off, contexto, 192 * 8)
    if len(buf) < 188 * 6:
        return None
    pacote = None
    if buf[0] == 0x47 and buf[188] == 0x47 and buf[376] == 0x47 and buf[564] == 0x47:
        pacote = 188
    elif (len(buf) >= 192 * 6 and buf[4] == 0x47 and buf[196] == 0x47
          and buf[388] == 0x47 and buf[580] == 0x47):
        pacote = 192
    else:
        return None
    # confirma sincronização no buffer inicial
    n = 0
    pos = 0
    while pos + pacote <= len(buf):
        idx = pos + (4 if pacote == 192 else 0)
        if buf[idx] != 0x47:
            break
        n += 1
        pos += pacote
    if n < 5:
        return None
    cursor = off + n * pacote
    limite = fim_regiao if fim_regiao else off + MAX_CARV
    truncado_na_regiao = False
    while cursor < limite:
        bloco = _ler(leitor, cursor, pacote * 1024)
        if len(bloco) < pacote:
            break
        p = 0
        quebra = False
        while p + pacote <= len(bloco):
            idx = p + (4 if pacote == 192 else 0)
            if bloco[idx] != 0x47:
                quebra = True
                break
            p += pacote
            n += 1
        cursor += p
        if quebra or len(bloco) < pacote * 1024:
            break
    else:
        truncado_na_regiao = True
    if fim_regiao and cursor >= fim_regiao:
        truncado_na_regiao = True
    tam = n * pacote
    if tam < MIN_TAMANHOS["mpegts"]:
        return None
    ext = "m2ts" if pacote == 192 else "mts"
    return ResultadoCarving(tam, "mpegts", ext, truncado=truncado_na_regiao,
                            motivo="fim da região alcançado" if truncado_na_regiao else "")


# ---------------------------------------------------------------------------
# Matroska / MKV (EBML)
# ---------------------------------------------------------------------------


def _vint(buf: bytes, i: int):
    """EBML vint. Retorna (valor, comprimento, desconhecido)."""
    if i >= len(buf):
        return None
    b0 = buf[i]
    if b0 == 0:
        return None
    comp = 1
    while comp <= 8 and not (b0 & (0x80 >> (comp - 1))):
        comp += 1
    if comp > 8 or i + comp > len(buf):
        return None
    val = b0 & (0xFF >> comp)
    for k in range(1, comp):
        val = (val << 8) | buf[i + k]
    desconhecido = val == (1 << (7 * comp)) - 1
    return (None if desconhecido else val, comp, desconhecido)


def analisar_mkv(leitor, off: int, contexto: bytes | None = None,
                 fim_regiao: int | None = None) -> ResultadoCarving | None:
    buf = bytearray(_ctx(leitor, off, contexto, 1 << 12))
    if bytes(buf[:4]) != b"\x1a\x45\xdf\xa3":
        return None
    v = _vint(buf, 4)
    if v is None or v[2]:
        return ResultadoCarving(None, "mkv", "mkv", truncado=True,
                                motivo="tamanho do cabeçalho EBML desconhecido")
    tam_hdr, comp_hdr = v[0], v[1]
    if tam_hdr is None or tam_hdr < 8 or tam_hdr > (1 << 20):
        return None
    i = 4 + comp_hdr + tam_hdr
    while len(buf) < i + 12:
        extra = _ler(leitor, off + len(buf), 1 << 12)
        if not extra:
            break
        buf += extra
    if len(buf) < i + 8 or bytes(buf[i:i + 4]) != b"\x18\x53\x80\x67":
        return ResultadoCarving(None, "mkv", "mkv", truncado=True,
                                motivo="cabeçalho EBML sem Segmento")
    v = _vint(buf, i + 4)
    if v is None:
        return None
    tam_seg, comp_seg, desconhecido = v
    if desconhecido or tam_seg is None:
        if fim_regiao:
            return ResultadoCarving(fim_regiao - off, "mkv", "mkv", truncado=True,
                                    motivo="Segmento com tamanho desconhecido; "
                                           "limitado ao fim da região")
        return ResultadoCarving(None, "mkv", "mkv", truncado=True,
                                motivo="Segmento com tamanho desconhecido")
    if tam_seg > MAX_CARV:
        return None
    total = i + 4 + comp_seg + tam_seg
    if fim_regiao and off + total > fim_regiao:
        return ResultadoCarving(None, "mkv", "mkv", truncado=True,
                                motivo="Segmento excede a região disponível")
    return ResultadoCarving(total, "mkv", "mkv")


# ---------------------------------------------------------------------------
# Varredura sequencial com sobreposição
# ---------------------------------------------------------------------------

ASSINATURAS = [
    (b"\xFF\xD8\xFF", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"IIRO", "tiff"),
    (b"IIRS", "tiff"),
    (b"IIU\x00", "tiff"),
    (b"FUJIFILMCCD-RAW", "raf"),
    (b"8BPS", "psd"),
    (b"RIFF", "riff"),
    (b"\x1a\x45\xdf\xa3", "mkv"),
]

SOBREPOSIÇÃO = 64 * 1024
# limite de memória da varredura: nenhuma leitura única pode passar de 8 MiB,
# portanto o bloco pesquisado é 8 MiB já incluindo a sobreposição
LIMITE_LEITURA = 8 * 1024 * 1024
BLOCO_VARREDURA = LIMITE_LEITURA - SOBREPOSIÇÃO

ANALISADORES = {
    "jpeg": analisar_jpeg,
    "png": analisar_png,
    "gif": analisar_gif,
    "bmp": analisar_bmp,
    "tiff": analisar_tiff,
    "raf": analisar_raf,
    "psd": analisar_psd,
    "riff": analisar_riff,
    "mp4": analisar_mp4,
    "mkv": analisar_mkv,
    "mpegts": analisar_mpegts,
}


def analisar_em(leitor, off: int, formato: str, contexto: bytes | None = None,
                fim_regiao: int | None = None) -> ResultadoCarving | None:
    fn = ANALISADORES.get(formato)
    if fn is None:
        return None
    if formato in ("jpeg", "mp4", "mkv", "mpegts"):
        return fn(leitor, off, fim_regiao=fim_regiao, contexto=contexto)
    return fn(leitor, off, contexto=contexto)


def varrer_regiao(leitor, inicio: int, fim: int, protegido=None,
                  cancelar=None, progresso=None, formatos=None,
                  min_bytes: int = 8 * 1024, detectar_ts: bool = True):
    """Gera candidatos de carving em [inicio, fim).

    * protegido: objeto com .contem(off) — regiões já recuperadas na etapa 1.
    * formatos: famílias de detectores aceitas (None = todas). As famílias são:
      jpeg, png, gif, bmp, tiff (inclui dng/cr2/nef/arw/orf/rw2/nrw/pef/srw),
      raf, psd, riff (webp/avi), mp4 (mov/m4v/3gp/heic/avif/cr3), mkv, mpegts.
    * min_bytes: ignora resultados menores (filtro 'menores de 8 KB').
    """
    tam_leitor = getattr(leitor, "tamanho", None)
    if tam_leitor:
        fim = min(fim, tam_leitor)
    pos = inicio
    total = max(1, fim - inicio)
    while pos < fim:
        if cancelar is not None and cancelar.is_set():
            return
        tam_leitura = min(LIMITE_LEITURA, fim - pos)
        buf = _ler(leitor, pos, tam_leitura)
        if not buf:
            break
        limite_busca = min(len(buf), BLOCO_VARREDURA)

        for assinatura, formato in ASSINATURAS:
            if formatos is not None and formato not in formatos:
                continue
            i = buf.find(assinatura, 0, limite_busca)
            while 0 <= i < limite_busca:
                off_abs = pos + i
                if protegido is not None and protegido.contem(off_abs):
                    i = buf.find(assinatura, i + 1, limite_busca)
                    continue
                resultado = analisar_em(leitor, off_abs, formato,
                                        contexto=buf[i:], fim_regiao=fim)
                if (resultado and resultado.comprimento
                        and resultado.comprimento >= min_bytes
                        and off_abs + resultado.comprimento <= fim + (1 << 20)):
                    yield (off_abs, resultado)
                    if protegido is not None:
                        protegido.adicionar(off_abs, off_abs + resultado.comprimento)
                i = buf.find(assinatura, i + 1, limite_busca)

        if formatos is None or "mp4" in formatos:
            i = buf.find(b"ftyp", 4, limite_busca)
            while 0 <= i < limite_busca:
                off_abs = pos + i - 4
                if (off_abs >= inicio
                        and (protegido is None or not protegido.contem(off_abs))):
                    resultado = analisar_mp4(leitor, off_abs,
                                             contexto=buf[max(0, i - 4):],
                                             fim_regiao=fim)
                    if (resultado and resultado.comprimento
                            and resultado.comprimento >= min_bytes):
                        yield (off_abs, resultado)
                        if protegido is not None:
                            protegido.adicionar(off_abs, off_abs + resultado.comprimento)
                i = buf.find(b"ftyp", i + 1, limite_busca)

        if detectar_ts and (formatos is None or "mpegts" in formatos):
            i = buf.find(b"\x47", 0, limite_busca)
            while 0 <= i < limite_busca - 192 * 6:
                achou = False
                # o arquivo pode começar no próprio 0x47 (pacotes de 188) ou
                # 4 bytes antes (M2TS/AVCHD: timecode de 4 bytes + sync)
                for off_rel in (i, i - 4):
                    if off_rel < 0 or off_rel + 192 * 6 > len(buf):
                        continue
                    sync_188 = (buf[off_rel] == 0x47
                                and buf[off_rel + 188] == 0x47
                                and buf[off_rel + 376] == 0x47)
                    sync_192 = (buf[off_rel + 4] == 0x47
                                and buf[off_rel + 196] == 0x47
                                and buf[off_rel + 388] == 0x47
                                and buf[off_rel + 580] == 0x47)
                    if not (sync_188 or sync_192):
                        continue
                    off_abs = pos + off_rel
                    if protegido is not None and protegido.contem(off_abs):
                        continue
                    resultado = analisar_mpegts(leitor, off_abs,
                                                contexto=buf[off_rel:],
                                                fim_regiao=fim)
                    if (resultado and resultado.comprimento
                            and resultado.comprimento >= min_bytes):
                        yield (off_abs, resultado)
                        if protegido is not None:
                            protegido.adicionar(off_abs,
                                                off_abs + resultado.comprimento)
                        i = off_rel + max(188, resultado.comprimento)
                        achou = True
                        break
                if achou:
                    i = buf.find(b"\x47", i, limite_busca)
                    continue
                i = buf.find(b"\x47", i + 1, limite_busca)

        pos += BLOCO_VARREDURA
        if progresso:
            progresso(min(1.0, (pos - inicio) / total), 1.0, "carving")
