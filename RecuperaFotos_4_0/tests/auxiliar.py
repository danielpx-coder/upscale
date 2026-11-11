"""
auxiliar.py — utilidades e construtores de arquivos sintéticos para testes
de carving/verificação. Independentes dos parsers do aplicativo.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pasta_temporaria(prefixo="rf4_") -> str:
    return tempfile.mkdtemp(prefix=prefixo)


def gravar(pasta: str, nome: str, dados: bytes) -> str:
    caminho = os.path.join(pasta, nome)
    with open(caminho, "wb") as f:
        f.write(dados)
    return caminho


# ---------------------------------------------------------------- PNG manual

def _chunk(tipo: bytes, dados: bytes) -> bytes:
    comp = struct.pack(">I", len(dados))
    crc = struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)
    return comp + tipo + dados + crc


def png_sintetico(largura=8, altura=6, cor=(255, 0, 0)) -> bytes:
    """PNG válido construído manualmente (IHDR + IDAT + IEND)."""
    ihdr = struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0)
    bruto = bytearray()
    for _y in range(altura):
        bruto.append(0)
        for _x in range(largura):
            bruto += bytes(cor)
    idat = zlib.compress(bytes(bruto))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def png_crc_corrompido() -> bytes:
    dados = bytearray(png_sintetico())
    # corrompe o CRC do IDAT (último chunk antes do IEND)
    iend = dados.rfind(b"IEND")
    pos_crc = iend - 4 - 12  # CRC do IDAT: antes do comp+tipo+dados+crc do IEND
    # localiza o CRC do IDAT com precisão
    i = 8
    while i < len(dados):
        (comp,) = struct.unpack_from(">I", dados, i)
        tipo = dados[i + 4:i + 8]
        if tipo == b"IDAT":
            dados[i + 8 + comp + 1] ^= 0xFF
            break
        i += 12 + comp
    del pos_crc
    return bytes(dados)


# ---------------------------------------------------------------- GIF manual

def _lzw_gif(min_code: int, pixels: bytes) -> bytes:
    """Codificador LZW do GIF (com crescimento de dicionário), conforme a
    especificação: CLEAR reinicia o dicionário; o tamanho do código cresce
    quando o dicionário atinge 2**tamanho; máximo 12 bits."""
    clear = 1 << min_code
    end = clear + 1
    tamanho = min_code + 1
    dic = {}
    proximo_codigo = end + 1

    bits = 0
    n_bits = 0
    saida = bytearray()

    def emitir(codigo: int):
        nonlocal bits, n_bits
        bits |= codigo << n_bits
        n_bits += tamanho
        while n_bits >= 8:
            saida.append(bits & 0xFF)
            bits >>= 8
            n_bits -= 8

    def reiniciar():
        nonlocal dic, proximo_codigo, tamanho
        dic = {}
        proximo_codigo = end + 1
        tamanho = min_code + 1

    emitir(clear)
    reiniciar()
    atual = b""
    for byte in pixels:
        b = bytes([byte])
        candidato = atual + b
        if candidato in dic or len(candidato) == 1:
            atual = candidato
            if len(atual) > 1 and atual not in dic:
                dic[atual] = proximo_codigo
                proximo_codigo += 1
                if proximo_codigo - 1 == (1 << tamanho) and tamanho < 12:
                    tamanho += 1
                if proximo_codigo > 4095:
                    emitir(clear)
                    reiniciar()
            continue
        if len(atual) == 1:
            emitir(atual[0])
        else:
            emitir(dic[atual])
        dic[candidato] = proximo_codigo
        proximo_codigo += 1
        if proximo_codigo - 1 == (1 << tamanho) and tamanho < 12:
            tamanho += 1
        if proximo_codigo > 4095:
            emitir(clear)
            reiniciar()
        atual = b
    if atual:
        if len(atual) == 1:
            emitir(atual[0])
        else:
            emitir(dic.get(atual, atual[0]))
    emitir(end)
    if n_bits:
        saida.append(bits & 0xFF)
    return bytes(saida)


def gif_sintetico(largura=8, altura=6, cor_indices=None) -> bytes:
    """GIF89a válido: tela 4x4 mínima até imagens maiores, com paleta de 4
    cores e dados LZW codificados corretamente."""
    if cor_indices is None:
        cor_indices = [(x + y) % 4 for y in range(altura) for x in range(largura)]
    paleta = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    out = bytearray()
    out += b"GIF89a"
    out += struct.pack("<HH", largura, altura)
    # GCT flag=1, resolução de cor=1 (4 bits/px), size=1 => 2**(1+1)=4 entradas
    out += bytes([0x80 | (1 << 4) | 1, 0x00, 0x00])
    for r, g, b in paleta:
        out += bytes([r, g, b])
    out += b"\x2C"
    out += struct.pack("<HHHH", 0, 0, largura, altura)
    out += bytes([0x00])                     # sem tabela local
    min_code = 2
    dados = _lzw_gif(min_code, bytes(cor_indices[:largura * altura]))
    out += bytes([min_code])
    k = 0
    while k < len(dados):
        bloco = dados[k:k + 255]
        out += bytes([len(bloco)]) + bloco
        k += 255
    out += b"\x00"                          # fim dos sub-blocos
    out += b"\x3B"                          # trailer
    return bytes(out)


# ---------------------------------------------------------------- BMP manual

def bmp_sintetico(largura=4, altura=3) -> bytes:
    linha = ((largura * 3) + 3) // 4 * 4
    pixels = b"\x33\x66\x99" * largura
    pixels += b"\x00" * (linha - largura * 3)
    dados_img = pixels * altura
    cab = bytearray(54)
    cab[0:2] = b"BM"
    struct.pack_into("<I", cab, 2, 54 + len(dados_img))
    struct.pack_into("<I", cab, 10, 54)
    struct.pack_into("<I", cab, 14, 40)
    struct.pack_into("<i", cab, 18, largura)
    struct.pack_into("<i", cab, 22, altura)
    struct.pack_into("<H", cab, 26, 1)
    struct.pack_into("<H", cab, 28, 24)
    struct.pack_into("<I", cab, 30, 0)
    struct.pack_into("<I", cab, 34, len(dados_img))
    return bytes(cab) + dados_img


# ---------------------------------------------------------------- WEBP / RIFF

def webp_real(largura=24, altura=18) -> bytes:
    """WebP real (codificado pelo Pillow), decodificável."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow indisponível") from exc
    from io import BytesIO
    img = Image.new("RGB", (largura, altura), (10, 200, 120))
    bio = BytesIO()
    img.save(bio, "WEBP")
    return bio.getvalue()


def webp_sintetico() -> bytes:
    """WEBP com chunk VP8X mínimo."""
    vp8x = bytearray(b"VP8X")
    payload = bytes([0x08, 0, 0, 0, 3, 0, 0, 2, 0, 0])  # flags + 4x3 dims
    chunk = vp8x + struct.pack("<I", len(payload)) + payload
    # VP8 lossless simples
    vp8l = b"VP8L" + struct.pack("<I", 6) + bytes([0x2F, 0, 0, 0, 0, 0])
    corpo = b"WEBP" + chunk + vp8l
    return b"RIFF" + struct.pack("<I", len(corpo)) + corpo


def avi_sintetico(n_listas: int = 2) -> bytes:
    hdrl = b"LIST" + struct.pack("<I", 4 + 8 + 8) + b"hdrl" + \
        b"avih" + struct.pack("<I", 8) + bytes(8)
    movi = b"LIST" + struct.pack("<I", 4 + 12) + b"movi" + \
        b"00db" + struct.pack("<I", 4) + bytes(4)
    corpo = b"AVI " + hdrl + movi * n_listas
    return b"RIFF" + struct.pack("<I", len(corpo)) + corpo


# ---------------------------------------------------------------- TIFF / RAW

def tiff_sintetico(endian="<", magic=42, dados_extra=b"", tag_dng=False,
                   largura=8, altura=6) -> bytes:
    """TIFF minimal válido: IFD0 com ImageWidth/Length, StripOffsets e
    StripByteCounts apontando para os dados anexados ao final."""
    strips = dados_extra if dados_extra else bytes(largura * altura * 3)
    entradas = [
        (256, 3, 1, largura),
        (257, 3, 1, altura),
    ]
    if tag_dng:
        entradas.append((0xC612, 3, 1, 0x0103))   # DNGVersion 1.3 (SHORT inline)
    # StripOffsets/StripByteCounts são acrescentados depois de saber o layout
    off_ifd = 8
    off_dados = off_ifd + 2 + (len(entradas) + 2) * 12 + 4
    entradas.append((273, 4, 1, off_dados))
    entradas.append((279, 4, 1, len(strips)))
    corpo = bytearray()
    corpo += (b"II" if endian == "<" else b"MM")
    corpo += struct.pack(endian + "H", magic)
    corpo += struct.pack(endian + "I", off_ifd)
    assert len(corpo) == off_ifd
    corpo += struct.pack(endian + "H", len(entradas))
    for tag, tipo, cnt, val in entradas:
        corpo += struct.pack(endian + "HHI", tag, tipo, cnt)
        if tipo == 3:
            corpo += struct.pack(endian + "H", val) + b"\x00\x00"
        else:
            corpo += struct.pack(endian + "I", val)
    corpo += struct.pack(endian + "I", 0)      # próximo IFD = 0
    corpo += strips
    return bytes(corpo)


def tiff_cr2_sintetico(largura=8, altura=6) -> bytes:
    """CR2: cabeçalho TIFF (8) + 'CR' + versão 2.0 + offset do RAW (16 no
    total), IFD0 em 16 e dados de imagem ao final."""
    strips = bytes(largura * altura * 3)
    off_ifd = 16
    entradas = [(256, 3, 1, largura), (257, 3, 1, altura)]
    off_dados = off_ifd + 2 + (len(entradas) + 2) * 12 + 4
    entradas.append((273, 4, 1, off_dados))
    entradas.append((279, 4, 1, len(strips)))
    corpo = bytearray()
    corpo += b"II*\x00"
    corpo += struct.pack("<I", off_ifd)
    corpo += b"CR" + bytes([2, 0])
    corpo += struct.pack("<I", off_dados)     # offset do cabeçalho RAW
    assert len(corpo) == off_ifd, len(corpo)
    corpo += struct.pack("<H", len(entradas))
    for tag, tipo, cnt, val in entradas:
        corpo += struct.pack("<HHI", tag, tipo, cnt)
        if tipo == 3:
            corpo += struct.pack("<H", val) + b"\x00\x00"
        else:
            corpo += struct.pack("<I", val)
    corpo += struct.pack("<I", 0)
    corpo += strips
    return bytes(corpo)


def orf_sintetico(magic=b"IIRO") -> bytes:
    dados = bytearray(tiff_sintetico(endian="<"))
    dados[0:4] = magic
    return bytes(dados)


def rw2_sintetico() -> bytes:
    dados = bytearray(tiff_sintetico(endian="<"))
    dados[0:4] = b"IIU\x00"
    return bytes(dados)


def raf_sintetico() -> bytes:
    """Estrutura Fujifilm RAF com offsets/tamanhos coerentes."""
    jpeg = bytes(64)
    cfa_hdr = bytes(32)
    cfa = bytes(2048)
    off_jpeg = 0x100
    off_cfa_hdr = off_jpeg + len(jpeg)
    off_cfa = off_cfa_hdr + len(cfa_hdr)
    total = off_cfa + len(cfa)
    cab = bytearray(off_jpeg)
    cab[0:16] = b"FUJIFILMCCD-RAW " + b"\x00" * 0
    cab[0:15] = b"FUJIFILMCCD-RAW"
    cab[15] = 0x20
    struct.pack_into(">I", cab, 0x54, 0)
    struct.pack_into(">I", cab, 0x58, 0)
    struct.pack_into(">I", cab, 0x5C, off_jpeg)
    struct.pack_into(">I", cab, 0x60, len(jpeg))
    struct.pack_into(">I", cab, 0x64, off_cfa_hdr)
    struct.pack_into(">I", cab, 0x68, len(cfa_hdr))
    struct.pack_into(">I", cab, 0x6C, off_cfa)
    struct.pack_into(">I", cab, 0x70, len(cfa))
    img = bytearray(total)
    img[0:len(cab)] = cab
    img[off_jpeg:off_jpeg + len(jpeg)] = jpeg
    img[off_cfa_hdr:off_cfa_hdr + len(cfa_hdr)] = cfa_hdr
    img[off_cfa:off_cfa + len(cfa)] = cfa
    return bytes(img)


def psd_sintetico(compressao=0, largura=4, altura=3, canais=3, prof=8) -> bytes:
    """PSD com 3 seções vazias + dados de imagem raw (compressão 0)."""
    cab = bytearray(26)
    cab[0:4] = b"8BPS"
    struct.pack_into(">H", cab, 4, 1)
    cab[6:12] = b"\x00" * 6
    struct.pack_into(">H", cab, 12, canais)
    struct.pack_into(">I", cab, 14, altura)
    struct.pack_into(">I", cab, 18, largura)
    struct.pack_into(">H", cab, 22, prof)
    struct.pack_into(">H", cab, 24, 3)  # RGB
    secoes = b""
    for dados_sec in (b"", b"\x01\x02", b""):
        secoes += struct.pack(">I", len(dados_sec)) + dados_sec
        if len(dados_sec) % 2:
            secoes += b"\x00"
    tam_dados = canais * altura * largura * (prof // 8)
    dados_img = struct.pack(">H", compressao) + bytes(tam_dados)
    return bytes(cab) + secoes + dados_img


# ---------------------------------------------------------------- ISO BMFF

def _caixa(tipo: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + tipo + payload


def _mvhd(timescale=1000, duracao=1000) -> bytes:
    """Movie Header Box (versão 0): 100 bytes de payload."""
    p = bytearray()
    p += bytes([0, 0, 0, 0])                     # versão + flags
    p += struct.pack(">I", 0)                    # creation_time
    p += struct.pack(">I", 0)                    # modification_time
    p += struct.pack(">I", timescale)
    p += struct.pack(">I", duracao)
    p += struct.pack(">I", 0x00010000)           # rate 1.0
    p += struct.pack(">H", 0x0100)               # volume 1.0
    p += b"\x00" * 10                           # reserved
    p += struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    p += b"\x00" * 24                           # pre_defined
    p += struct.pack(">I", 2)                    # next_track_ID
    return bytes(p)


def _tkhd(largura=320, altura=240, duracao=1000) -> bytes:
    p = bytearray()
    p += bytes([0, 0, 0, 0x07])                  # versão + flags (enabled...)
    p += struct.pack(">III", 0, 0, 1)            # creation, modification, track_ID
    p += struct.pack(">I", 0)                    # reserved
    p += struct.pack(">I", duracao)
    p += b"\x00" * 8                            # reserved
    p += struct.pack(">HH", 0, 0)                # layer, alternate_group
    p += struct.pack(">H", 0)                    # volume
    p += b"\x00" * 2                            # reserved
    p += struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    p += struct.pack(">II", largura << 16, altura << 16)
    return bytes(p)


def _mdhd(timescale=1000, duracao=1000) -> bytes:
    p = bytearray()
    p += bytes([0, 0, 0, 0])
    p += struct.pack(">III", 0, 0, timescale)
    p += struct.pack(">I", duracao)
    p += struct.pack(">HH", 0x55C4, 0)           # language 'und', pre_defined
    return bytes(p)


def _hdlr_video() -> bytes:
    p = bytearray()
    p += bytes([0, 0, 0, 0])
    p += b"\x00\x00\x00\x00"                 # pre_defined
    p += b"vide"
    p += b"\x00" * 12                           # reserved
    p += b"VideoHandler\x00"
    return bytes(p)


def mp4_sintetico(marca=b"mp42", marcas_comp=(b"isom",), tam_mdat=256,
                  com_moov=True, largura=320, altura=240,
                  caixa_extra: bytes = b"") -> bytes:
    """Arquivo ISO-BMFF com cadeia de caixas coerente:
    ftyp [+ extra] moov(mvhd, trak(tkhd, mdia(mdhd, hdlr, minf))) mdat.
    O comprimento total é a soma exata das caixas."""
    ftyp = _caixa(b"ftyp", marca + struct.pack(">I", 0x200) + b"".join(marcas_comp))
    caixas = ftyp
    if caixa_extra:
        caixas += caixa_extra
    if com_moov:
        minf = _caixa(b"minf", b"\x00" * 8)
        mdia = (_caixa(b"mdhd", _mdhd()) + _caixa(b"hdlr", _hdlr_video())
                + minf)
        trak = _caixa(b"tkhd", _tkhd(largura, altura)) + _caixa(b"mdia", mdia)
        moov = _caixa(b"mvhd", _mvhd()) + _caixa(b"trak", trak)
        caixas += _caixa(b"moov", moov)
    corpo = bytes((i * 31 + 7) & 0xFF for i in range(tam_mdat))
    caixas += _caixa(b"mdat", corpo)
    return caixas


def heic_sintetico(tam_mdat=128) -> bytes:
    """HEIC: ftyp 'heic'/'mif1' + meta-like moov + mdat. Sem decodificador
    HEVC, o conteúdo não pode ser validado (espera-se nao_verificado)."""
    return mp4_sintetico(marca=b"heic", marcas_comp=(b"mif1", b"heix"),
                         tam_mdat=tam_mdat)


def avif_sintetico(tam_mdat=128) -> bytes:
    return mp4_sintetico(marca=b"avif", marcas_comp=(b"mif1",), tam_mdat=tam_mdat)


def mov_sintetico(tam_mdat=128) -> bytes:
    return mp4_sintetico(marca=b"qt  ", marcas_comp=(), tam_mdat=tam_mdat)


def m4v_sintetico(tam_mdat=128) -> bytes:
    return mp4_sintetico(marca=b"M4V ", marcas_comp=(b"isom",), tam_mdat=tam_mdat)


def mp4_3gp_sintetico(tam_mdat=128) -> bytes:
    return mp4_sintetico(marca=b"3gp4", marcas_comp=(b"3gp4", b"isom"),
                         tam_mdat=tam_mdat)


# ---------------------------------------------------------------- MPEG-TS

def mpegts_sintetico(n_pacotes=40, pacote=188) -> bytes:
    out = bytearray()
    for k in range(n_pacotes):
        if pacote == 192:
            out += struct.pack(">I", k)  # timecode
        pid = 0x100 + (k % 4)
        out += bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10])
        out += bytes((k * 7 + j) & 0xFF for j in range(pacote - 4 - (4 if pacote == 192 else 0)))
    return bytes(out)


# ---------------------------------------------------------------- MKV

def _vint_valor(v: int, comp: int) -> bytes:
    marcador = 1 << (7 * comp)
    x = marcador | v
    return x.to_bytes(comp, "big")


def mkv_sintetico(tam_conhecido=True, corpo_seg=64) -> bytes:
    # EBML header
    doc = b"matroska"
    elem_doc = b"\x42" + b"\x82"  # DocType id 0x4282
    elem_doc += _vint_valor(len(doc), 1) + doc
    hdr_corpo = elem_doc
    ebml_id = b"\x1a\x45\xdf\xa3"
    ebml = ebml_id + _vint_valor(len(hdr_corpo), 1) + hdr_corpo
    seg_id = b"\x18\x53\x80\x67"
    dados_seg = bytes(corpo_seg)
    if tam_conhecido:
        seg = seg_id + _vint_valor(len(dados_seg), 4) + dados_seg
    else:
        seg = seg_id + bytes([0x0F, 0xFF, 0xFF, 0xFF]) + dados_seg
    return ebml + seg


# ---------------------------------------------------------------- HEIC sem decodificador

def heic_sem_decodificador() -> bytes:
    """HEIC estruturalmente válido (ftyp heic + moov + mdat) cujo conteúdo
    HEVC não é decodificável pelo Pillow — deve classificar como
    nao_verificado, nunca como ok."""
    return heic_sintetico()


# ---------------------------------------------------------------- JPEG especiais

def jpeg_motion_photo(jpeg: bytes, mp4: bytes) -> bytes:
    """JPEG + MP4 anexo + XMP APP1 com MicroVideoOffset=len(mp4)."""
    xmp = (b'http://ns.google.com/photos/1.0/camera/\x00'
           b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
           b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           b'<rdf:Description rdf:about="" '
           b'xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
           b'GCamera:MotionPhoto="1" MicroVideoOffset="'
           + str(len(mp4)).encode() + b'"/></rdf:RDF></x:xmpmeta>')
    seg = b"\xFF\xE1" + struct.pack(">H", len(xmp) + 2) + xmp
    # insere APP1 logo após SOI; MP4 vai ao final
    return jpeg[:2] + seg + jpeg[2:] + mp4


def jpeg_mpf(jpeg1: bytes, jpeg2: bytes, valido: bool = True) -> bytes:
    """JPEG MPF: primário com APP2 'MPF\\0' + índice TIFF (tags B000/B001/B002)
    e jpeg2 anexado ao final. Offsets relativos ao início do cabeçalho MPF.

    Layout: SOI(2) + APP2(90) + resto_de_jpeg1 + jpeg2
      posição de 'MPF\\0' = 6;  posição de jpeg2 = len(jpeg1) + 90
    """
    def montar_seg(off2_rel: int) -> bytes:
        off_entrada = 8 + 2 + 3 * 12 + 4          # = 50
        ifd = bytearray()
        ifd += struct.pack("<H", 3)
        ifd += struct.pack("<HHI", 0xB000, 7, 4) + b"0100"
        ifd += struct.pack("<HHI", 0xB001, 4, 1) + struct.pack("<I", 2)
        ifd += struct.pack("<HHI", 0xB002, 4, 1) + struct.pack("<I", off_entrada)
        ifd += struct.pack("<I", 0)
        tam_primario = len(jpeg1) + 90 - len(jpeg2)  # tudo antes de jpeg2
        entrada = struct.pack("<IIIHH", 0x00030000, tam_primario, 0, 0, 0)
        entrada += struct.pack("<IIIHH", 0x00020001, len(jpeg2), off2_rel, 0, 0)
        tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) \
            + bytes(ifd) + entrada
        assert len(tiff) == 82, len(tiff)
        conteudo = b"MPF\x00" + tiff
        seg = b"\xFF\xE2" + struct.pack(">H", len(conteudo) + 2) + conteudo
        assert len(seg) == 90, len(seg)
        return seg

    if valido:
        off2_rel = len(jpeg1) + 90 - 6
    else:
        off2_rel = 0x00ABCDE & 0x7FFFFFFF  # offset incoerente
    seg = montar_seg(off2_rel)
    return jpeg1[:2] + seg + jpeg1[2:] + jpeg2


def jpeg_com_rst_fora_de_ordem(jpeg_grande: bytes) -> bytes:
    """Troca dois marcadores RST consecutivos distintos para quebrar a
    sequência (simula fragmentos de outra fotografia intercalados)."""
    dados = bytearray(jpeg_grande)
    # localiza SOS
    i = dados.find(b"\xFF\xDA")
    if i < 0:
        raise ValueError("JPEG sem SOS")
    # pula o segmento SOS
    (comp,) = struct.unpack_from(">H", dados, i + 2)
    j = i + 2 + comp
    achados = []
    while j < len(dados) - 1 and len(achados) < 4:
        if dados[j] == 0xFF and 0xD0 <= dados[j + 1] <= 0xD7:
            achados.append(j)
            j += 2
            continue
        j += 1
    if len(achados) < 2:
        raise ValueError("JPEG sem RST suficientes")
    # inverte dois RST adjacentes trocando seus valores
    a, b = achados[0], achados[1]
    dados[a + 1], dados[b + 1] = dados[b + 1], dados[a + 1]
    # se ficaram iguais, força valores distintos fora de ordem
    if dados[a + 1] == dados[b + 1]:
        dados[a + 1] = 0xD3
        dados[b + 1] = 0xD0
    return bytes(dados)


def jpeg_truncado(jpeg: bytes) -> bytes:
    """Remove o EOI final (arquivo truncado)."""
    i = jpeg.rfind(b"\xFF\xD9")
    if i > 0:
        return jpeg[:i]
    return jpeg[:-2]


def jpeg_lixo_no_meio(jpeg: bytes) -> bytes:
    """Insere lixo não-JPEG logo após o SOI (estrutura inválida)."""
    return jpeg[:2] + b"\x01\x02\x03\x04\x05\x06\x07\x08" + jpeg[2:]
