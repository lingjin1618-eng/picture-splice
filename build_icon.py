"""生成 Windows 桌面版的程序图标。

用法：python build_icon.py [输出路径.ico]

图案与界面标题栏左侧的小图标一致：两张叠放的圆角照片卡片，
上面是太阳和山峦。生成 16–256 像素的多尺寸 ICO。
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw

SIZE = 512


def _gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    strip = Image.new("RGB", (1, 256))
    pixels = strip.load()
    for y in range(256):
        t = y / 255
        pixels[0, y] = tuple(
            round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
        )
    return strip.resize((SIZE, SIZE), Image.Resampling.BILINEAR)


def _rounded_mask(radius: int) -> Image.Image:
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, SIZE - 1, SIZE - 1), radius=radius, fill=255
    )
    return mask


def build_icon() -> Image.Image:
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gradient = _gradient((0xA8, 0x8C, 0xBA), (0x7B, 0x60, 0x90)).convert("RGBA")
    base.paste(gradient, (0, 0), _rounded_mask(118))

    draw = ImageDraw.Draw(base)
    # 柔和的高光。
    draw.rounded_rectangle(
        (34, 26, SIZE - 34, 200),
        radius=100,
        fill=(255, 255, 255, 26),
    )

    # 后面那张浅紫色照片。
    draw.rounded_rectangle(
        (64, 136, 330, 418),
        radius=52,
        fill=(0xEC, 0xE2, 0xF1, 255),
    )
    # 前面那张深紫色照片。
    draw.rounded_rectangle(
        (152, 62, 430, 352),
        radius=58,
        fill=(0x9A, 0x7F, 0xAE, 255),
    )
    draw.ellipse((300, 102, 356, 158), fill=(0xF6, 0xE6, 0xEB, 255))
    draw.polygon(
        [(180, 300), (252, 200), (310, 262), (354, 220), (418, 300)],
        fill=(0xF3, 0xEA, 0xF6, 255),
    )
    draw.rounded_rectangle(
        (152, 62, 430, 352), radius=58, outline=(255, 255, 255, 70), width=5
    )

    # 左下角的一块小粉色点缀，和主界面的藕粉色呼应。
    draw.ellipse((58, 350, 138, 430), fill=(0xD9, 0xA6, 0xB7, 255))
    draw.ellipse((82, 374, 114, 406), fill=(0xF6, 0xEA, 0xF0, 255))
    return base


def main() -> None:
    output = sys.argv[1] if len(sys.argv) > 1 else "app_icon.ico"
    icon = build_icon()
    icon.save(
        output,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"icon written: {output}")


if __name__ == "__main__":
    main()
