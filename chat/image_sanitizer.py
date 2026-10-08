"""Metadata-stripping rewrite of raster images for the sanitized download.

A prompt-injected agent can hide data in the parts of an image file that a
viewer never shows: EXIF and XMP blocks, text chunks, comments, an embedded
colour profile, bytes appended after the end-of-image marker. The sanitized
download (``GET .../files/download-sanitized``) hands the user a copy that
keeps the picture exactly as it is and nothing else.

The rewrite is STRUCTURAL, not a decode / re-encode: each container is walked
segment by segment and only the parts that carry the pixels or are needed to
decode them are copied through (``_PNG_KEEP``, ``_JPEG_KEEP``, the GIF /
WebP block rules below). Everything the allow-list does not name is dropped,
including unknown and private blocks, and the walk stops at the format's
end marker so trailing bytes never make it out. Lossless by construction:
a JPEG's entropy-coded scans are copied verbatim, no quality is lost.

What survives, per format:

* PNG: IHDR, PLTE, tRNS, IDAT, IEND, the APNG triple acTL / fcTL / fdAT, and
  the fixed-size colour-interpretation chunks gAMA / cHRM / sRGB, each
  length-checked (a wrong-sized one is dropped). Chunk CRCs are recomputed.
  Dropped: tEXt / zTXt / iTXt, eXIf, tIME, iCCP, pHYs, bKGD, sBIT, hIST,
  sPLT and every other chunk.
* JPEG: the frame header, quantisation / Huffman / arithmetic tables, restart
  interval, scans and the hierarchical DNL / DHP / EXP segments. The APP0
  JFIF segment is regenerated in its generic form (version 1.01, no density,
  no thumbnail) when the original had one, and an APP14 Adobe segment is
  regenerated carrying only its colour-transform byte -- decoders rely on
  both to pick YCbCr / RGB / YCCK, so they cannot simply go. Dropped: every
  other APPn (EXIF, XMP, ICC, ...), COM, JPGn, bytes between segments and
  after EOI.
* GIF: header, logical screen descriptor, colour tables, image descriptors
  with their LZW data, graphic control extensions and the NETSCAPE2.0 loop
  extension (regenerated from its loop count). Dropped: comment, plain-text
  and every other application extension (XMP, ICC), bytes after the
  trailer. The output is always GIF89a so the Quest comment can be added.
* WebP: VP8 / VP8L / VP8X / ALPH / ANIM / ANMF (an ANMF frame keeps only its
  ALPH / VP8 / VP8L sub-chunks). Dropped: EXIF, XMP, ICCP and unknown
  chunks, with the matching VP8X flag bits cleared; the RIFF size is
  recomputed. WebP has no plain comment chunk, so it gets no Quest tag.

The generic tag the user asked for is a PNG tEXt pair (``Software`` /
``Comment``), a JPEG COM segment and a GIF comment extension, all reading
``Generated with Quest``.

Not addressed, by design: data hidden IN the pixels (steganography) and in
the decode tables / compressed streams the picture needs -- the pixel data is
kept intact, which is the whole point of this copy versus a re-encode.

Pure stdlib (``struct`` / ``zlib``); the format is picked by magic bytes,
never by the file name.
"""

from __future__ import annotations

import struct
import zlib
from typing import Callable

QUEST_TAG = "Generated with Quest"
QUEST_SOFTWARE = "Quest"


class ImageSanitizeError(ValueError):
    """The bytes are not a supported raster image, or the container is
    malformed in a way the walk cannot get past. The message is safe to show
    to the user."""


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Chunk type -> required data length (None = any). Everything else is dropped.
_PNG_KEEP: dict[bytes, int | None] = {
    b"IHDR": 13,
    b"PLTE": None,
    b"tRNS": None,
    b"IDAT": None,
    b"IEND": 0,
    # APNG: frame control + frame data are pixel data.
    b"acTL": 8,
    b"fcTL": 26,
    b"fdAT": None,
    # Colour interpretation, fixed-size so there is no room for a payload.
    b"gAMA": 4,
    b"cHRM": 32,
    b"sRGB": 1,
}


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _png_text_chunk(keyword: str, text: str) -> bytes:
    return _png_chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + text.encode("latin-1"))


def sanitize_png(data: bytes) -> bytes:
    if not data.startswith(_PNG_SIGNATURE):
        raise ImageSanitizeError("Not a PNG file")
    out = [_PNG_SIGNATURE]
    pos = len(_PNG_SIGNATURE)
    first = True
    while True:
        if pos + 8 > len(data):
            raise ImageSanitizeError("Truncated PNG file")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        chunk_type = data[pos + 4:pos + 8]
        if length > 0x7FFFFFFF:
            raise ImageSanitizeError("Malformed PNG chunk")
        chunk_data = data[pos + 8:pos + 8 + length]
        if len(chunk_data) != length or pos + 12 + length > len(data):
            raise ImageSanitizeError("Truncated PNG file")
        pos += 12 + length  # length + type + data + crc
        if first:
            if chunk_type != b"IHDR":
                raise ImageSanitizeError("Malformed PNG file: IHDR is not the first chunk")
            first = False
        expected = _PNG_KEEP.get(chunk_type, -1)
        if expected == -1 or (expected is not None and expected != length):
            continue
        out.append(_png_chunk(chunk_type, chunk_data))
        if chunk_type == b"IHDR":
            out.append(_png_text_chunk("Software", QUEST_SOFTWARE))
            out.append(_png_text_chunk("Comment", QUEST_TAG))
        if chunk_type == b"IEND":
            return b"".join(out)


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

_JPEG_SOI = 0xD8
_JPEG_EOI = 0xD9
_JPEG_SOS = 0xDA
_JPEG_APP0 = 0xE0
_JPEG_APP14 = 0xEE
_JPEG_COM = 0xFE
_JPEG_RST_FIRST, _JPEG_RST_LAST = 0xD0, 0xD7

# Length-prefixed segments copied through verbatim: SOFn (minus the table
# markers sharing the C0-CF range), DHT, DAC, DQT, DRI, SOS, DNL, DHP, EXP.
_JPEG_KEEP = frozenset(
    [m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC)]
    + [0xC4, 0xCC, 0xDB, 0xDD, 0xDA, 0xDC, 0xDE, 0xDF]
)

_JFIF_GENERIC = b"JFIF\0" + bytes([1, 1, 0]) + struct.pack(">HH", 1, 1) + bytes([0, 0])


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def sanitize_jpeg(data: bytes) -> bytes:
    if not (len(data) >= 3 and data[0] == 0xFF and data[1] == _JPEG_SOI and data[2] == 0xFF):
        raise ImageSanitizeError("Not a JPEG file")
    out = [bytes([0xFF, _JPEG_SOI])]
    pos = 2
    saw_jfif = False
    adobe_transform: int | None = None
    header_done = False  # the regenerated APP0 / COM / APP14 go before the first kept segment
    n = len(data)

    def emit_header() -> None:
        nonlocal header_done
        if header_done:
            return
        header_done = True
        if saw_jfif:
            out.append(_jpeg_segment(_JPEG_APP0, _JFIF_GENERIC))
        out.append(_jpeg_segment(_JPEG_COM, QUEST_TAG.encode("ascii")))
        if adobe_transform is not None:
            out.append(_jpeg_segment(
                _JPEG_APP14,
                b"Adobe" + struct.pack(">HHHB", 100, 0, 0, adobe_transform),
            ))

    while True:
        # Expect a marker; anything else between segments is garbage the
        # decoder would skip, so skip (drop) it too.
        while pos < n and data[pos] != 0xFF:
            pos += 1
        while pos < n and data[pos] == 0xFF:
            pos += 1  # fill bytes
        if pos >= n:
            raise ImageSanitizeError("Truncated JPEG file: no EOI marker")
        marker = data[pos]
        pos += 1
        if marker == _JPEG_EOI:
            emit_header()
            out.append(bytes([0xFF, _JPEG_EOI]))
            return b"".join(out)
        if marker == 0x00 or marker == _JPEG_SOI or _JPEG_RST_FIRST <= marker <= _JPEG_RST_LAST or marker == 0x01:
            # Stuffed byte / stray RST / TEM outside a scan: nothing to copy.
            continue
        if pos + 2 > n:
            raise ImageSanitizeError("Truncated JPEG file")
        (length,) = struct.unpack(">H", data[pos:pos + 2])
        if length < 2 or pos + length > n:
            raise ImageSanitizeError("Malformed JPEG segment")
        payload = data[pos + 2:pos + length]
        pos += length

        if marker == _JPEG_APP0 and payload.startswith(b"JFIF\0"):
            saw_jfif = True
            continue
        if marker == _JPEG_APP14 and payload.startswith(b"Adobe") and len(payload) >= 12:
            adobe_transform = payload[11]
            continue
        if marker not in _JPEG_KEEP:
            continue  # APPn, COM, JPGn, unknown

        emit_header()
        out.append(_jpeg_segment(marker, payload))

        if marker == _JPEG_SOS:
            # Entropy-coded data up to the next real marker. 0xFF00 is a
            # stuffed data byte and RSTn markers are part of the scan; a run
            # of 0xFF before a marker is fill, dropped.
            start = pos
            while True:
                idx = data.find(b"\xff", pos)
                if idx < 0 or idx + 1 >= n:
                    raise ImageSanitizeError("Truncated JPEG file: scan has no end")
                nxt = data[idx + 1]
                if nxt == 0x00 or _JPEG_RST_FIRST <= nxt <= _JPEG_RST_LAST:
                    pos = idx + 2
                    continue
                if nxt == 0xFF:
                    out.append(data[start:idx])
                    pos = idx + 1
                    start = pos
                    continue
                out.append(data[start:idx])
                pos = idx
                break


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

_GIF_IMAGE = 0x2C
_GIF_EXTENSION = 0x21
_GIF_TRAILER = 0x3B
_GIF_EXT_GRAPHIC_CONTROL = 0xF9
_GIF_EXT_COMMENT = 0xFE
_GIF_EXT_APPLICATION = 0xFF
_GIF_NETSCAPE = b"NETSCAPE2.0"


def _gif_sub_blocks(data: bytes, pos: int) -> tuple[bytes, int]:
    """Collect the data sub-blocks starting at ``pos`` (incl. the terminator).
    Returns the raw bytes and the position after the terminator."""
    start = pos
    n = len(data)
    while True:
        if pos >= n:
            raise ImageSanitizeError("Truncated GIF file")
        size = data[pos]
        pos += 1 + size
        if pos > n:
            raise ImageSanitizeError("Truncated GIF file")
        if size == 0:
            return data[start:pos], pos


def _gif_sub_blocks_payload(raw: bytes) -> bytes:
    """The concatenated payload of a sub-block sequence."""
    out = []
    pos = 0
    while True:
        size = raw[pos]
        pos += 1
        if size == 0:
            return b"".join(out)
        out.append(raw[pos:pos + size])
        pos += size


def _gif_encode_sub_blocks(payload: bytes) -> bytes:
    out = []
    for i in range(0, len(payload), 255):
        piece = payload[i:i + 255]
        out.append(bytes([len(piece)]) + piece)
    out.append(b"\0")
    return b"".join(out)


def sanitize_gif(data: bytes) -> bytes:
    if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        raise ImageSanitizeError("Not a GIF file")
    n = len(data)
    if n < 13:
        raise ImageSanitizeError("Truncated GIF file")
    out = [b"GIF89a", data[6:13]]  # logical screen descriptor
    packed = data[10]
    pos = 13
    if packed & 0x80:
        size = 3 * (2 << (packed & 0x07))
        if pos + size > n:
            raise ImageSanitizeError("Truncated GIF file")
        out.append(data[pos:pos + size])
        pos += size
    out.append(bytes([_GIF_EXTENSION, _GIF_EXT_COMMENT]) + _gif_encode_sub_blocks(QUEST_TAG.encode("ascii")))

    while True:
        if pos >= n:
            raise ImageSanitizeError("Truncated GIF file: no trailer")
        block = data[pos]
        pos += 1
        if block == _GIF_TRAILER:
            out.append(bytes([_GIF_TRAILER]))
            return b"".join(out)
        if block == _GIF_IMAGE:
            if pos + 9 > n:
                raise ImageSanitizeError("Truncated GIF file")
            descriptor = data[pos:pos + 9]
            pos += 9
            local_packed = descriptor[8]
            table = b""
            if local_packed & 0x80:
                size = 3 * (2 << (local_packed & 0x07))
                table = data[pos:pos + size]
                if len(table) != size:
                    raise ImageSanitizeError("Truncated GIF file")
                pos += size
            if pos >= n:
                raise ImageSanitizeError("Truncated GIF file")
            lzw_min = data[pos:pos + 1]
            pos += 1
            blocks, pos = _gif_sub_blocks(data, pos)
            out.append(bytes([_GIF_IMAGE]) + descriptor + table + lzw_min + blocks)
            continue
        if block == _GIF_EXTENSION:
            if pos >= n:
                raise ImageSanitizeError("Truncated GIF file")
            label = data[pos]
            pos += 1
            blocks, pos = _gif_sub_blocks(data, pos)
            if label == _GIF_EXT_GRAPHIC_CONTROL:
                payload = _gif_sub_blocks_payload(blocks)
                if len(payload) == 4:
                    out.append(bytes([_GIF_EXTENSION, label]) + _gif_encode_sub_blocks(payload))
                continue
            if label == _GIF_EXT_APPLICATION:
                payload = _gif_sub_blocks_payload(blocks)
                # NETSCAPE2.0 loop count: identifier (11) + sub-block id 1 + loops (2).
                if payload[:11] == _GIF_NETSCAPE and len(payload) >= 14 and payload[11] == 1:
                    out.append(
                        bytes([_GIF_EXTENSION, label])
                        + _gif_encode_sub_blocks(_GIF_NETSCAPE)[:-1]  # 11-byte block, no terminator
                        + bytes([3, 1]) + payload[12:14] + b"\0"
                    )
                continue
            continue  # comment, plain text, unknown extension
        raise ImageSanitizeError("Malformed GIF file: unknown block")


# ---------------------------------------------------------------------------
# WebP
# ---------------------------------------------------------------------------

_WEBP_KEEP = frozenset([b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"])
_WEBP_FRAME_KEEP = frozenset([b"ALPH", b"VP8 ", b"VP8L"])
# VP8X flag bits of the chunks that are dropped.
_WEBP_VP8X_DROPPED_FLAGS = 0x20 | 0x08 | 0x04  # ICC, EXIF, XMP


def _riff_chunks(data: bytes, pos: int, end: int):
    while pos < end:
        if pos + 8 > end:
            raise ImageSanitizeError("Truncated WebP file")
        fourcc = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        payload = data[pos + 8:pos + 8 + size]
        if len(payload) != size:
            raise ImageSanitizeError("Truncated WebP file")
        yield fourcc, payload
        pos += 8 + size + (size & 1)


def _riff_chunk(fourcc: bytes, payload: bytes) -> bytes:
    return fourcc + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def sanitize_webp(data: bytes) -> bytes:
    if not (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        raise ImageSanitizeError("Not a WebP file")
    (riff_size,) = struct.unpack("<I", data[4:8])
    end = min(8 + riff_size, len(data))
    if end < 12:
        raise ImageSanitizeError("Truncated WebP file")
    chunks: list[bytes] = []
    for fourcc, payload in _riff_chunks(data, 12, end):
        if fourcc not in _WEBP_KEEP:
            continue
        if fourcc == b"VP8X":
            if len(payload) != 10:
                raise ImageSanitizeError("Malformed WebP file: bad VP8X chunk")
            payload = bytes([payload[0] & ~_WEBP_VP8X_DROPPED_FLAGS]) + payload[1:]
        elif fourcc == b"ANMF":
            if len(payload) < 16:
                raise ImageSanitizeError("Malformed WebP file: bad ANMF chunk")
            frame = [
                _riff_chunk(sub, sub_payload)
                for sub, sub_payload in _riff_chunks(payload, 16, len(payload))
                if sub in _WEBP_FRAME_KEEP
            ]
            payload = payload[:16] + b"".join(frame)
        chunks.append(_riff_chunk(fourcc, payload))
    if not chunks:
        raise ImageSanitizeError("Malformed WebP file: no image data")
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_FORMATS: tuple[tuple[Callable[[bytes], bool], Callable[[bytes], bytes], str], ...] = (
    (lambda d: d.startswith(_PNG_SIGNATURE), sanitize_png, "image/png"),
    (lambda d: d.startswith(b"\xff\xd8\xff"), sanitize_jpeg, "image/jpeg"),
    (lambda d: d.startswith(b"GIF87a") or d.startswith(b"GIF89a"), sanitize_gif, "image/gif"),
    (lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP", sanitize_webp, "image/webp"),
)


def sanitize_image(data: bytes) -> tuple[bytes, str]:
    """Return ``(sanitized_bytes, mime_type)`` for a PNG / JPEG / GIF / WebP,
    picked by magic bytes. ``ImageSanitizeError`` for anything else."""
    for matches, sanitize, mime in _FORMATS:
        if matches(data):
            return sanitize(data), mime
    raise ImageSanitizeError(
        "Only PNG, JPEG, GIF and WebP images can be sanitized; this file is not one of them"
    )
