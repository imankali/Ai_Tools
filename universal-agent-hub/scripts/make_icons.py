#!/usr/bin/env python3
"""ساخت آیکون‌های PNG برای PWA (و به‌طور جانبی launcher های دسکتاپ).

چرا اسکریپت؟ تا فایل‌های باینری UI در repo «تولیدشده» باشند و نه ادیت‌دستی:
بدون وابستگی خارجی (فقط کتابخانه‌ی استاندارد) و کاملاً قطعی (deterministic).

    python scripts/make_icons.py            # src/server/web/icons/ را می‌سازد
    python scripts/make_icons.py --out DIR  # جای دیگر

طرح: پس‌زمینه‌ی سرمه‌ای با یک نوار مورب روشن‌تر، حلقه‌ی مرکزی و چهار گره —
نماد «هاب»: یک مرکز که ابزارها/دستگاه‌ها به آن وصل‌اند.
"""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# بوم RGBA ساده
# ---------------------------------------------------------------------------


def _channel(value: float) -> int:
    """مقدار ۰..۱ → بایت ۰..۲۵۵ با کلَمپ."""
    return max(0, min(255, round(value * 255)))


class Canvas:
    """بوم پیکسل‌محور با سوپرسمپلینگ ۲× برای لبه‌های نرم."""

    def __init__(self, size: int, scale: int = 2) -> None:
        self.size = size
        self.scale = scale
        self.side = size * scale
        self.data = [0.0, 0.0, 0.0, 0.0] * (self.side * self.side)

    def _put(self, x: int, y: int, color: tuple[float, float, float, float]) -> None:
        if 0 <= x < self.side and 0 <= y < self.side:
            i = (y * self.side + x) * 4
            for k in range(4):
                self.data[i + k] = color[k]

    def fill(self, color: tuple[int, int, int]) -> None:
        rgb = (color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
        for y in range(self.side):
            for x in range(self.side):
                self._put(x, y, rgb)

    def band(self, offset: float, width: float, color: tuple[int, int, int]) -> None:
        """نوار مورب: |x - y - offset| < width/2 (برای بافت پس‌زمینه)."""
        rgb = (color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
        side = self.side
        span = width * side
        for y in range(side):
            base = -offset * side + y
            lo = int(base - span / 2)
            hi = int(base + span / 2)
            for x in range(max(0, lo), min(side, hi)):
                self._put(x, y, rgb)

    def disc(self, cx: float, cy: float, radius: float, color: tuple[int, int, int]) -> None:
        rgb = (color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
        r = radius * self.side
        px, py = cx * self.side, cy * self.side
        x0, x1 = int(px - r) - 1, int(px + r) + 2
        y0, y1 = int(py - r) - 1, int(py + r) + 2
        edge = max(1.0, 0.9 / self.size * self.side)
        for y in range(max(0, y0), min(self.side, y1)):
            for x in range(max(0, x0), min(self.side, x1)):
                d = ((x + 0.5 - px) ** 2 + (y + 0.5 - py) ** 2) ** 0.5
                alpha = min(1.0, max(0.0, (r - d) / edge + 0.5))
                if alpha <= 0:
                    continue
                i = (y * self.side + x) * 4
                bg = self.data[i + 3]
                if bg <= 0:
                    for k in range(4):
                        self.data[i + k] = rgb[k] if k < 3 else 1.0
                    continue
                for k in range(3):
                    a = rgb[k] * alpha
                    self.data[i + k] = self.data[i + k] * (1 - a) + a
                self.data[i + 3] = 1.0

    def ring(self, cx: float, cy: float, radius: float, thickness: float, color: tuple[int, int, int]) -> None:
        """حلقه: قرصِ شعاع r با ضخامت مشخص (حلقه‌ی باریک، نه دیسک)."""
        outer, inner = radius + thickness / 2, radius - thickness / 2
        rgb = (color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
        ro, ri = outer * self.side, inner * self.side
        px, py = cx * self.side, cy * self.side
        edge = max(1.0, 0.9 / self.size * self.side)
        span = int(ro) + 2
        for y in range(max(0, int(py - span)), min(self.side, int(py + span))):
            for x in range(max(0, int(px - span)), min(self.side, int(px + span))):
                d = ((x + 0.5 - px) ** 2 + (y + 0.5 - py) ** 2) ** 0.5
                alpha = min(1.0, max(0.0, (ro - d) / edge + 0.5)) * min(1.0, max(0.0, (d - ri) / edge + 0.5))
                if alpha <= 0.01:
                    continue
                i = (y * self.side + x) * 4
                for k in range(3):
                    a = rgb[k] * alpha
                    self.data[i + k] = self.data[i + k] * (1 - a) + a
                self.data[i + 3] = 1.0

    def downsample(self) -> bytes:
        """میانگین ۴ نمونه‌ی ۲×۲ → RGBA نهایی."""
        side, scale = self.side, self.scale
        out = bytearray()
        data = self.data
        for y in range(0, side, scale):
            for x in range(0, side, scale):
                acc = [0.0, 0.0, 0.0, 0.0]
                for dy in range(scale):
                    for dx in range(scale):
                        i = ((y + dy) * side + (x + dx)) * 4
                        for k in range(4):
                            acc[k] += data[i + k]
                n = float(scale * scale)
                for k in range(4):
                    out.append(_channel(acc[k] / n))
        return bytes(out)


def png_bytes(canvas: Canvas) -> bytes:
    """ساخت PNG واقعی (8-bit RGBA) بدون هیچ کتابخانه‌ای."""
    raw = canvas.downsample()
    side = canvas.size
    stride = side * 4
    scanlines = b"".join(b"\x00" + raw[y * stride : (y + 1) * stride] for y in range(side))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def draw(size: int, *, maskable: bool = False) -> bytes:
    """آیکون هاب با ابعاد ``size``؛ در حالت maskable گلیف کوچک‌تر و حاشیه‌ی ایمن."""
    canvas = Canvas(size)
    canvas.fill((11, 16, 32))  # همان --bg تیره‌ی UI: #0b1020
    canvas.band(-0.15, 0.42, (18, 27, 54))  # نوار مورب روشن‌تر
    zoom = 0.78 if maskable else 1.0  # zone‌ی ایمن ماسکابل ≈ ۸۰٪
    canvas.ring(0.5, 0.5, 0.175 * zoom, 0.075 * zoom, (122, 162, 255))
    canvas.disc(0.5, 0.5, 0.062 * zoom, (232, 240, 255))
    for cx, cy in ((0.5, 0.165), (0.835, 0.5), (0.5, 0.835), (0.165, 0.5)):
        canvas.disc(cx, cy, 0.055 * zoom, (122, 162, 255))
        canvas.disc(cx, cy, 0.022 * zoom, (232, 240, 255))
    # اتصال گره‌ها به مرکز
    for cx, cy in ((0.5, 0.30), (0.70, 0.5), (0.5, 0.70), (0.30, 0.5)):
        canvas.disc(cx, cy, 0.014 * zoom, (77, 128, 224))
    return png_bytes(canvas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the PWA PNG icons with no external deps.")
    parser.add_argument("--out", default=None, help="output directory (default: src/server/web/icons)")
    parser.add_argument("--check", action="store_true", help="only verify the icons exist and are valid PNGs")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "src" / "server" / "web" / "icons"
    targets = {"icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512}

    if args.check:
        missing = [name for name in targets if not (out / name).is_file()]
        for name in list(missing):
            print(f"missing {out / name}")
        return 1 if missing else 0

    out.mkdir(parents=True, exist_ok=True)
    for name, size in targets.items():
        data = draw(size, maskable="maskable" in name)
        path = out / name
        path.write_bytes(data)
        print(f"write {path.name} · {size}x{size} · {len(data) // 1024} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
