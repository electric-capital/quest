"""chat/image_sanitizer.py: the metadata-stripping rewrite behind the
sanitized image download.

Fixtures are built with Pillow carrying every metadata channel the format
has (EXIF, XMP, ICC, text chunks, comments, trailing bytes); the sanitized
output is then decoded with Pillow again -- an independent decoder -- and
must yield the SAME pixels with NONE of the metadata, plus the Quest tag
where the format has a comment field.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image, ImageCms

from chat.image_sanitizer import (
    QUEST_TAG,
    ImageSanitizeError,
    sanitize_gif,
    sanitize_image,
    sanitize_jpeg,
    sanitize_png,
    sanitize_webp,
)

SECRET = b"api_key=sk-live-0123456789"
SECRET_TEXT = SECRET.decode()


def _picture(mode: str = "RGB") -> Image.Image:
    img = Image.new(mode, (6, 4))
    px = img.load()
    for y in range(4):
        for x in range(6):
            v = (x * 40 + y * 60) % 256
            px[x, y] = (v, 255 - v, (v * 3) % 256) if mode == "RGB" else (v, 255 - v, (v * 3) % 256, 200)
    return img


def _exif_bytes() -> bytes:
    exif = Image.Exif()
    exif[0x010E] = SECRET_TEXT  # ImageDescription
    exif[0x0131] = "Evil Software"
    return exif.tobytes()


def _icc_bytes() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _xmp_bytes() -> bytes:
    return (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b"<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF>" + SECRET + b"</rdf:RDF></x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )


def _pixels(data: bytes) -> tuple[str, tuple[int, int], bytes]:
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return img.mode, img.size, img.tobytes()


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------


def _png_with_metadata() -> bytes:
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", SECRET_TEXT)
    info.add_text("Secret", SECRET_TEXT, zip=True)  # zTXt
    info.add_itxt("Hidden", SECRET_TEXT)  # iTXt
    buf = io.BytesIO()
    _picture("RGBA").save(
        buf, "PNG", pnginfo=info, exif=_exif_bytes(), icc_profile=_icc_bytes(), dpi=(300, 300),
    )
    data = buf.getvalue()
    # A private chunk a sanitizer must not know about, spliced in before IEND.
    iend = data.rfind(b"IEND") - 4
    private = struct.pack(">I", len(SECRET)) + b"prVt" + SECRET
    private += struct.pack(">I", zlib.crc32(b"prVt" + SECRET) & 0xFFFFFFFF)
    data = data[:iend] + private + data[iend:]
    # Trailing bytes after IEND.
    return data + b"\n" + SECRET


class TestPng:
    def test_pixels_kept_metadata_gone(self):
        original = _png_with_metadata()
        assert SECRET in original

        out, mime = sanitize_image(original)

        assert mime == "image/png"
        assert SECRET not in out
        assert zlib.compress(SECRET) not in out
        assert _pixels(out) == _pixels(original)
        with Image.open(io.BytesIO(out)) as img:
            assert img.info.get("exif") is None
            assert img.info.get("icc_profile") is None
            assert "dpi" not in img.info
            assert img.text == {"Software": "Quest", "Comment": QUEST_TAG}
        assert out.endswith(b"IEND\xaeB`\x82")

    def test_chunk_allow_list_is_exact(self):
        out = sanitize_png(_png_with_metadata())
        types = []
        pos = 8
        while pos < len(out):
            (length,) = struct.unpack(">I", out[pos:pos + 4])
            types.append(out[pos + 4:pos + 8])
            pos += 12 + length
        assert types[0] == b"IHDR" and types[-1] == b"IEND"
        assert set(types) <= {b"IHDR", b"tEXt", b"IDAT", b"IEND", b"gAMA", b"cHRM", b"sRGB", b"PLTE", b"tRNS"}
        assert b"prVt" not in types and b"eXIf" not in types and b"iCCP" not in types

    def test_wrong_sized_colour_chunk_is_dropped(self):
        # A gAMA chunk padded with a payload is not a gAMA chunk.
        buf = io.BytesIO()
        _picture().save(buf, "PNG")
        data = buf.getvalue()
        fake = b"gAMA" + struct.pack(">I", 45455) + SECRET
        chunk = struct.pack(">I", len(fake) - 4) + fake + struct.pack(">I", zlib.crc32(fake) & 0xFFFFFFFF)
        data = data[:33] + chunk + data[33:]  # right after IHDR
        out = sanitize_png(data)
        assert SECRET not in out
        assert _pixels(out) == _pixels(buf.getvalue())

    def test_palette_png(self):
        buf = io.BytesIO()
        _picture().convert("P", palette=Image.ADAPTIVE, colors=8).save(buf, "PNG", transparency=0)
        out = sanitize_png(buf.getvalue())
        assert _pixels(out) == _pixels(buf.getvalue())

    def test_apng_frames_survive(self):
        frames = [_picture(), _picture().transpose(Image.FLIP_LEFT_RIGHT)]
        buf = io.BytesIO()
        frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
        out = sanitize_png(buf.getvalue())
        with Image.open(io.BytesIO(out)) as img:
            assert getattr(img, "n_frames", 1) == 2
            img.seek(1)
            assert img.convert("RGB").tobytes() == frames[1].tobytes()

    @pytest.mark.parametrize("data", [
        b"\x89PNG\r\n\x1a\n",
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + b"\0" * 5,
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 1) + b"tEXt" + b"x" + b"\0\0\0\0",
    ])
    def test_truncated_or_malformed(self, data):
        with pytest.raises(ImageSanitizeError):
            sanitize_png(data)


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------


def _jpeg_with_metadata(**save_kwargs) -> bytes:
    buf = io.BytesIO()
    _picture().save(
        buf, "JPEG", quality=90, exif=_exif_bytes(), icc_profile=_icc_bytes(),
        comment=SECRET, xmp=_xmp_bytes(), dpi=(300, 300), **save_kwargs,
    )
    data = buf.getvalue()
    # An APP9 segment nobody knows and bytes after EOI.
    app9 = b"\xff\xe9" + struct.pack(">H", len(SECRET) + 2) + SECRET
    return data[:2] + app9 + data[2:] + SECRET


class TestJpeg:
    @pytest.mark.parametrize("kwargs", [{}, {"progressive": True}, {"optimize": True}])
    def test_pixels_kept_metadata_gone(self, kwargs):
        original = _jpeg_with_metadata(**kwargs)
        assert original.count(SECRET) >= 4

        out, mime = sanitize_image(original)

        assert mime == "image/jpeg"
        assert SECRET not in out
        # Lossless: the scans are copied, so the decode is byte-identical.
        assert _pixels(out) == _pixels(original)
        with Image.open(io.BytesIO(out)) as img:
            assert img.info.get("exif") is None
            assert img.info.get("icc_profile") is None
            assert img.info.get("xmp") is None
            assert "dpi" not in img.info
            assert img.info.get("comment") == QUEST_TAG.encode()
            # The regenerated generic JFIF header.
            assert img.info.get("jfif_version") == (1, 1)
        assert out.endswith(b"\xff\xd9")

    def test_segment_allow_list(self):
        out = sanitize_jpeg(_jpeg_with_metadata())
        markers = []
        pos = 2
        while pos < len(out) - 2:
            assert out[pos] == 0xFF
            marker = out[pos + 1]
            markers.append(marker)
            (length,) = struct.unpack(">H", out[pos + 2:pos + 4])
            pos += 2 + length
            if marker == 0xDA:
                break
        assert markers[:2] == [0xE0, 0xFE]  # JFIF, then the Quest comment
        assert 0xE1 not in markers and 0xE2 not in markers and 0xE9 not in markers

    def test_adobe_transform_regenerated(self):
        buf = io.BytesIO()
        _picture().convert("CMYK").save(buf, "JPEG", quality=90)
        original = buf.getvalue()
        assert b"Adobe" in original
        out = sanitize_jpeg(original)
        idx = out.find(b"\xff\xee")
        assert idx > 0
        (length,) = struct.unpack(">H", out[idx + 2:idx + 4])
        assert length == 14
        assert out[idx + 4:idx + 9] == b"Adobe"
        assert _pixels(out) == _pixels(original)

    def test_garbage_between_segments_is_dropped(self):
        buf = io.BytesIO()
        _picture().save(buf, "JPEG")
        data = buf.getvalue()
        # Junk between the APP0 segment and the next one, as decoders tolerate.
        assert data[2:4] == b"\xff\xe0"
        (app0_len,) = struct.unpack(">H", data[4:6])
        cut = 4 + app0_len
        junk = bytes(b for b in SECRET if b != 0xFF)
        data = data[:cut] + junk + data[cut:]
        out = sanitize_jpeg(data)
        assert junk not in out
        assert _pixels(out) == _pixels(buf.getvalue())

    @pytest.mark.parametrize("data", [
        b"\xff\xd8\xff",
        b"\xff\xd8\xff\xdb\x00\x10abc",
        _jpeg_with_metadata()[:-len(SECRET) - 2],  # EOI cut off
    ])
    def test_truncated(self, data):
        with pytest.raises(ImageSanitizeError):
            sanitize_jpeg(data)


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------


def _gif_with_metadata(animated: bool = False) -> bytes:
    frames = [_picture().convert("P", palette=Image.ADAPTIVE, colors=16)]
    if animated:
        frames.append(frames[0].transpose(Image.FLIP_LEFT_RIGHT))
    buf = io.BytesIO()
    frames[0].save(
        buf, "GIF", save_all=animated, append_images=frames[1:], comment=SECRET,
        loop=0, duration=80, transparency=0,
    )
    data = buf.getvalue()
    assert data.endswith(b"\x3b")
    # An XMP application extension and bytes after the trailer.
    xmp = b"\x21\xff\x0bXMP DataXMP" + bytes([len(SECRET)]) + SECRET + b"\0"
    return data[:-1] + xmp + b"\x3b" + SECRET


class TestGif:
    @pytest.mark.parametrize("animated", [False, True])
    def test_pixels_kept_metadata_gone(self, animated):
        original = _gif_with_metadata(animated)
        assert SECRET in original

        out, mime = sanitize_image(original)

        assert mime == "image/gif"
        assert SECRET not in out
        assert out.startswith(b"GIF89a") and out.endswith(b"\x3b")
        with Image.open(io.BytesIO(out)) as img, Image.open(io.BytesIO(original)) as src:
            assert img.info.get("comment") == QUEST_TAG.encode()
            assert getattr(img, "n_frames", 1) == getattr(src, "n_frames", 1)
            for i in range(getattr(src, "n_frames", 1)):
                img.seek(i)
                src.seek(i)
                assert img.convert("RGBA").tobytes() == src.convert("RGBA").tobytes()
            if animated:
                assert img.info.get("loop") == 0
                assert img.info.get("duration") == 80

    def test_gif87a_is_upgraded(self):
        data = _gif_with_metadata()
        data = b"GIF87a" + data[6:]
        out = sanitize_gif(data)
        assert out.startswith(b"GIF89a")
        assert _pixels(out) == _pixels(_gif_with_metadata())

    def test_plain_text_extension_is_dropped(self):
        data = _gif_with_metadata()
        pos = 13 + 3 * 16  # after the global colour table
        plain = b"\x21\x01\x0c" + b"\0" * 12 + bytes([len(SECRET)]) + SECRET + b"\0"
        out = sanitize_gif(data[:pos] + plain + data[pos:])
        assert SECRET not in out
        assert _pixels(out) == _pixels(_gif_with_metadata())

    @pytest.mark.parametrize("data", [
        b"GIF89a",
        b"GIF89a" + b"\0" * 7,  # no trailer
        b"GIF89a" + b"\0" * 7 + b"\x2c" + b"\0" * 4,
    ])
    def test_truncated(self, data):
        with pytest.raises(ImageSanitizeError):
            sanitize_gif(data)


# ---------------------------------------------------------------------------
# WebP
# ---------------------------------------------------------------------------


def _webp_with_metadata(lossless: bool) -> bytes:
    buf = io.BytesIO()
    _picture("RGBA").save(
        buf, "WEBP", lossless=lossless, quality=100, exif=_exif_bytes(),
        icc_profile=_icc_bytes(), xmp=_xmp_bytes(),
    )
    data = buf.getvalue()
    # An unknown chunk and bytes past the RIFF size.
    junk = b"JUNK" + struct.pack("<I", len(SECRET)) + SECRET + (b"\0" if len(SECRET) & 1 else b"")
    body = data[8:] + junk
    return b"RIFF" + struct.pack("<I", len(body)) + body + SECRET


def _riff_fourccs(data: bytes) -> list[bytes]:
    out = []
    pos = 12
    (size,) = struct.unpack("<I", data[4:8])
    while pos < 8 + size:
        fourcc = data[pos:pos + 4]
        (length,) = struct.unpack("<I", data[pos + 4:pos + 8])
        out.append(fourcc)
        pos += 8 + length + (length & 1)
    return out


class TestWebp:
    @pytest.mark.parametrize("lossless", [True, False])
    def test_pixels_kept_metadata_gone(self, lossless):
        original = _webp_with_metadata(lossless)
        assert SECRET in original

        out, mime = sanitize_image(original)

        assert mime == "image/webp"
        assert SECRET not in out
        assert _pixels(out) == _pixels(original)
        assert struct.unpack("<I", out[4:8])[0] == len(out) - 8
        with Image.open(io.BytesIO(out)) as img:
            assert img.info.get("exif") is None
            assert img.info.get("icc_profile") is None
            assert img.info.get("xmp") is None
        fourccs = _riff_fourccs(out)
        assert b"EXIF" not in fourccs and b"XMP " not in fourccs
        assert b"ICCP" not in fourccs and b"JUNK" not in fourccs

    def test_vp8x_flags_cleared(self):
        original = _webp_with_metadata(lossless=True)
        idx = original.find(b"VP8X")
        assert original[idx + 8] & 0x2C  # ICC / EXIF / XMP set on the original
        out = sanitize_webp(original)
        idx = out.find(b"VP8X")
        assert idx > 0
        assert out[idx + 8] & 0x2C == 0
        assert out[idx + 8] & 0x10  # alpha flag kept

    def test_animated_frames_survive(self):
        frames = [_picture("RGBA"), _picture("RGBA").transpose(Image.FLIP_LEFT_RIGHT)]
        buf = io.BytesIO()
        frames[0].save(
            buf, "WEBP", save_all=True, append_images=frames[1:], lossless=True,
            duration=50, loop=0, exif=_exif_bytes(),
        )
        original = buf.getvalue()
        out = sanitize_webp(original)
        assert b"EXIF" not in _riff_fourccs(out)
        with Image.open(io.BytesIO(out)) as img:
            assert img.n_frames == 2
            img.seek(1)
            assert img.convert("RGBA").tobytes() == frames[1].tobytes()

    @pytest.mark.parametrize("data", [
        b"RIFF\x04\x00\x00\x00WEBP",
        b"RIFF\x10\x00\x00\x00WEBPVP8 \xff\x00\x00\x00abc",
        b"RIFF\x14\x00\x00\x00WEBPEXIF\x04\x00\x00\x00abcd",  # nothing left
    ])
    def test_truncated_or_empty(self, data):
        with pytest.raises(ImageSanitizeError):
            sanitize_webp(data)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.parametrize("data", [
        b"", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"BM" + b"\0" * 50,
        b"%PDF-1.4", b"\x89PNx",
    ])
    def test_unknown_magic_is_refused(self, data):
        with pytest.raises(ImageSanitizeError, match="Only PNG, JPEG, GIF and WebP"):
            sanitize_image(data)

    def test_format_by_magic_not_name(self):
        # The caller passes bytes only; a PNG is a PNG whatever it is called.
        out, mime = sanitize_image(_png_with_metadata())
        assert mime == "image/png" and out.startswith(b"\x89PNG")
