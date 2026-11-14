"""Minimal PNG decoder/encoder (standard library only).

Used to compare pixels with a reference .cap, to tell whether a collision mask
is just the image's alpha channel, and to write custom collision masks.  Input
is untrusted: image dimensions are capped and inflation is bounded by the size
the header announces, so a crafted file cannot exhaust memory.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 64 * 1024 * 1024       # 64 Mpx, far above anything Construct exports
MAX_DIMENSION = 32768


class PNGError(ValueError):
    pass


@dataclass
class Image:
    width: int
    height: int
    rgba: bytes          # top-down, 4 bytes per pixel, non-premultiplied

    def alpha_mask(self) -> list[bytes]:
        """One bytes row per scanline: 1 where alpha > 0 (the editor's collision rule)."""
        w = self.width
        return [bytes(1 if self.rgba[(y * w + x) * 4 + 3] else 0 for x in range(w)) for y in range(self.height)]


_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _unfilter(data: memoryview, width: int, height: int, bpp_bits: int, pos: int) -> tuple[list[bytearray], int]:
    stride = (width * bpp_bits + 7) // 8
    bpp = max(1, bpp_bits // 8)
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for _ in range(height):
        if pos + 1 + stride > len(data):
            raise PNGError("image data truncated")
        ftype = data[pos]
        row = bytearray(data[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        elif ftype != 0:
            raise PNGError(f"bad filter type {ftype}")
        rows.append(row)
        prev = row
    return rows, pos


def _samples(row: bytearray, count: int, depth: int) -> list[int]:
    if depth == 8:
        return list(row[:count])
    if depth == 16:
        return [row[2 * i] for i in range(count)]          # keep the high byte
    per = 8 // depth
    mask = (1 << depth) - 1
    return [(row[i // per] >> (8 - depth * (i % per + 1))) & mask for i in range(count)]


def decode(data: bytes) -> Image:
    """Decode a PNG file to 8-bit RGBA.  Any malformation raises :class:`PNGError`."""
    try:
        return _decode(data)
    except PNGError:
        raise
    except (zlib.error, struct.error, IndexError, ValueError) as e:
        raise PNGError(f"corrupt PNG: {e}") from None


def _decode(data: bytes) -> Image:
    if not data.startswith(SIGNATURE):
        raise PNGError("not a PNG file")
    pos, idat, palette, trns, ihdr = 8, [], None, None, None
    while pos + 8 <= len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise PNGError("chunk truncated")
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise PNGError("missing IHDR")
    width, height, depth, color, _, _, interlace = ihdr
    if color not in _CHANNELS or depth not in (1, 2, 4, 8, 16):
        raise PNGError(f"unsupported PNG format (color type {color}, depth {depth})")
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION) or width * height > MAX_PIXELS:
        raise PNGError(f"implausible PNG size {width}x{height}")
    channels = _CHANNELS[color]
    bits = channels * depth

    passes = [(0, 0, 1, 1)] if interlace == 0 else list(_ADAM7)
    sizes = []
    for x0, y0, dx, dy in passes:
        pw, ph = (width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy
        sizes.append((pw, ph))
    expected = sum(ph * (1 + (pw * bits + 7) // 8) for pw, ph in sizes if pw and ph)
    d = zlib.decompressobj()
    raw = d.decompress(b"".join(idat), expected + 1)
    if len(raw) < expected:
        raise PNGError("image data truncated")

    out = bytearray(width * height * 4)
    view, pos = memoryview(raw), 0
    for (x0, y0, dx, dy), (pw, ph) in zip(passes, sizes):
        if not pw or not ph:
            continue
        rows, pos = _unfilter(view, pw, ph, bits, pos)
        for j, row in enumerate(rows):
            s = _samples(row, pw * channels, depth)
            y = y0 + j * dy
            for i in range(pw):
                px = s[i * channels:(i + 1) * channels]
                if color == 0:
                    g = px[0] * 255 // ((1 << min(depth, 8)) - 1)
                    a = 0 if trns and len(trns) >= 2 and px[0] == struct.unpack(">H", trns[:2])[0] else 255
                    rgba = (g, g, g, a)
                elif color == 2:
                    rgba = (px[0], px[1], px[2], 255)
                elif color == 3:
                    k = px[0]
                    if palette is None or 3 * k + 3 > len(palette):
                        raise PNGError("palette index out of range")
                    a = trns[k] if trns and k < len(trns) else 255
                    rgba = (palette[3 * k], palette[3 * k + 1], palette[3 * k + 2], a)
                elif color == 4:
                    rgba = (px[0], px[0], px[0], px[1])
                else:
                    rgba = tuple(px)
                o = (y * width + x0 + i * dx) * 4
                out[o:o + 4] = bytes(rgba)
    return Image(width, height, bytes(out))


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def encode_bilevel(rows: list[bytes], width: int) -> bytes:
    """Write a 1-bit grayscale PNG: 0 = black/transparent area, 1 = white/solid."""
    raw = bytearray()
    for row in rows:
        raw.append(0)
        packed = bytearray((width + 7) // 8)
        for x in range(width):
            if row[x]:
                packed[x // 8] |= 0x80 >> (x % 8)
        raw += packed
    ihdr = struct.pack(">IIBBBBB", width, len(rows), 1, 0, 0, 0, 0)
    return SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def encode_rgba(width: int, height: int, rgba: bytes) -> bytes:
    """Write an 8-bit RGBA PNG (top-down pixel data)."""
    stride = width * 4
    raw = b"".join(b"\0" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def encode_bmp24(width: int, height: int, rgba: bytes, background: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    """Write a 24-bit BMP, compositing RGBA over ``background`` (what CxImage BMP icons look like)."""
    pad = (4 - width * 3 % 4) % 4
    rows = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b, a = rgba[(y * width + x) * 4:(y * width + x) * 4 + 4]
            rows += bytes(((b * a + background[2] * (255 - a)) // 255, (g * a + background[1] * (255 - a)) // 255,
                           (r * a + background[0] * (255 - a)) // 255))
        rows += b"\0" * pad
    header = struct.pack("<2sIHHI", b"BM", 54 + len(rows), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(rows), 0, 0, 0, 0)
    return header + info + bytes(rows)


def thumbnail(img: Image, size: int) -> bytes:
    """Nearest-neighbour RGBA thumbnail, aspect preserved, centred on a transparent square."""
    out = bytearray(size * size * 4)
    scale = max(img.width, img.height) / size
    w, h = max(1, round(img.width / scale)), max(1, round(img.height / scale))
    ox, oy = (size - w) // 2, (size - h) // 2
    for y in range(h):
        sy = min(img.height - 1, int((y + 0.5) * scale))
        for x in range(w):
            sx = min(img.width - 1, int((x + 0.5) * scale))
            o = ((oy + y) * size + ox + x) * 4
            out[o:o + 4] = img.rgba[(sy * img.width + sx) * 4:(sy * img.width + sx) * 4 + 4]
    return bytes(out)


def decode_icon_dib(data: bytes) -> Image:
    """Decode the DIB of an RT_ICON resource (24/32 bpp, uncompressed) to RGBA."""
    if len(data) < 40:
        raise PNGError("icon too small")
    size, width, height2, planes, bpp, compression = struct.unpack_from("<IiiHHI", data, 0)
    height = height2 // 2
    if size != 40 or compression != 0 or bpp not in (24, 32) or not (0 < width <= 256 and 0 < height <= 256):
        raise PNGError(f"unsupported icon format ({bpp} bpp)")
    stride = (width * bpp // 8 + 3) & ~3
    mask_stride = ((width + 31) // 32) * 4
    if 40 + stride * height > len(data):
        raise PNGError("icon data truncated")
    mask_at = 40 + stride * height
    has_mask = mask_at + mask_stride * height <= len(data)
    out = bytearray(width * height * 4)
    for y in range(height):
        row = 40 + (height - 1 - y) * stride
        for x in range(width):
            p = row + x * (bpp // 8)
            b, g, r = data[p], data[p + 1], data[p + 2]
            if bpp == 32:
                a = data[p + 3]
            elif has_mask:
                a = 0 if data[mask_at + (height - 1 - y) * mask_stride + x // 8] & (0x80 >> (x % 8)) else 255
            else:
                a = 255
            out[(y * width + x) * 4:(y * width + x) * 4 + 4] = bytes((r, g, b, a))
    return Image(width, height, bytes(out))


def mask_rows(bits: bytes, width: int, height: int, pitch: int) -> list[bytes]:
    """Unpack an exported collision mask (1 bpp, MSB first, top-down) to 0/1 rows."""
    return [bytes((bits[y * pitch + x // 8] >> (7 - x % 8)) & 1 for x in range(width)) for y in range(height)]


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(SIGNATURE) or len(data) < 24:
        raise PNGError("not a PNG file")
    return struct.unpack_from(">II", data, 16)
