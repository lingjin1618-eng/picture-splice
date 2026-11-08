"""Picture Splice · 六寸照片拼接工具。

工作区从左到右是「单张照片」「自动拼接」「自定义拼接」三栏，最右是大预览区，
三栏都可以收起或拖动分栏线调整宽度。自动拼接页由工具按场景/色彩自动生成；
自定义拼接栏可以先建立 2 张或 4 张的空白版面，再把照片拖进格子里自己拼。
点击单张照片只预览这张照片，点击任一拼接缩略图则预览拼好的版面。
横图会按格子方向自动旋转成竖向，避免被裁掉太多；预览里可以用滚轮缩放、
拖动取景（带残影）。
输出为 3:2 的 6 寸照片 (1800 x 1200 px @ 300 dpi)，导入只缓存预览图、
导出时按原文件全分辨率重新拼接。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import random
import re
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Iterable

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageTk

try:  # 可选依赖：Windows 桌面版支持从资源管理器直接拖入照片文件
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # noqa: BLE001 - 没装或 TkDND 不可用时只禁用外部拖入
    DND_FILES = None
    TkinterDnD = None

# WSL/Linux 下用内部拖拽即可；Windows 原生版额外支持从资源管理器拖文件。
OS_FILE_DND = TkinterDnD is not None and sys.platform == "win32"

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}


# ---- 输出规格 -------------------------------------------------------------

OUTPUT_WIDTH = 1800
OUTPUT_HEIGHT = 1200
OUTPUT_DPI = 300

# 导出 JPEG 时使用接近无损的设置，并保留完整色度信息。
JPEG_QUALITY = 98

# 界面预览只缓存缩小后的副本，导出时才从原文件按全分辨率读取。
# 这样一次导入很多张照片也不会占满内存，导出质量仍与原图一致。
PREVIEW_MAX_SIZE = 1600
IMPORT_WORKERS = max(2, min(6, (os.cpu_count() or 4)))

# 中间「自动拼接」和右侧「自定义拼接」列表里的成品缩略图尺寸。
PAGE_THUMB_WIDTH = 150
PAGE_THUMB_HEIGHT = 100

# Moldiv 里的 12 档间距/边距，按约 0.25 mm 一档换算，即默认约 3 mm。
UNIT_MM = 0.25
DEFAULT_GAP = 12
DEFAULT_MARGIN = 12
MAX_SPACING = 100

# 自动混排时，每一页成为 4 张田字格的概率；其余为 2 张左右。
MIXED_FOUR_CHANCE = 0.3

# 单张照片的最大放大倍率。
MAX_ZOOM = 3.0

IMAGE_FILETYPES = [
    ("图片文件", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
    ("所有文件", "*.*"),
]

# 柔和的淡紫 + 藕粉浅色系（面板都带一点颜色，不使用纯白）。
BG = "#efe8f1"
PANEL_BG = "#f7f2f8"
TITLEBAR_BG = "#f4eef6"
TEXT = "#46404e"
MUTED = "#948b9c"
BORDER = "#e3d9e7"
ACCENT = "#9a7fae"
ACCENT_HOVER = "#8a6e9e"
ACCENT_PRESS = "#7b6090"
ACCENT_SOFT = "#ece2f1"
ACCENT_BLUSH = "#d9a6b7"
PREVIEW_BG = "#e9e1ed"
CARD_BG = "#f2ecf5"
CARD_HOVER = "#ece4f0"
CARD_SELECTED = "#e7dcef"
DANGER = "#c0888e"
DANGER_SOFT = "#f3dfe2"


# ---- 照片分析 -------------------------------------------------------------

HUE_BINS = 12
ANALYSIS_SIZE = 64


@dataclass(frozen=True)
class PhotoProfile:
    """自动分组用的小图特征：人物/建筑倾向、色彩分布和感知哈希。"""

    is_person: bool
    is_building: bool
    skin_ratio: float
    largest_skin: float
    dark_ratio: float
    structure: float
    sky_ratio: float
    neutral_ratio: float
    hue_hist: tuple[float, ...]
    saturation: float
    brightness: float
    dhash: int


def _is_skin(r: int, g: int, b: int) -> bool:
    """用 YCbCr + RGB 双重条件判断一个像素是否接近肤色。"""
    y = 0.299 * r + 0.587 * g + 0.114 * b
    if not 60 <= y <= 240:
        return False
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    if not (77 <= cb <= 127 and 133 <= cr <= 173):
        return False
    mx = max(r, g, b)
    mn = min(r, g, b)
    return (
        r > 60
        and g > 30
        and b > 15
        and r >= g
        and r >= b
        and mx - mn > 10
    )


def _largest_skin_component(mask: bytearray, size: int) -> tuple[int, float]:
    """返回最大肤色连通块的面积，以及它外接矩形的填充率。

    人的脸部/手臂肤色通常是紧凑的连通块；沙地、木纹这类大片同色区域
    虽然颜色接近肤色，但会铺满画面、外接框填充率很高，用这两个指标可以
    大致区分开。
    """
    visited = bytearray(size * size)
    largest = 0
    largest_fill = 0.0
    for start in range(size * size):
        if not mask[start] or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        area = 0
        min_x = min_y = size
        max_x = max_y = 0
        while stack:
            index = stack.pop()
            area += 1
            y, x = divmod(index, size)
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < size and 0 <= ny < size:
                        nindex = ny * size + nx
                        if mask[nindex] and not visited[nindex]:
                            visited[nindex] = 1
                            stack.append(nindex)
        if area > largest:
            largest = area
            bbox = max(1, (max_x - min_x + 1) * (max_y - min_y + 1))
            largest_fill = area / bbox
    return largest, largest_fill


def _structure_features(
    gray_pixels, size: int
) -> tuple[float, float]:
    """估算画面的「建筑感」：边缘密度和贯穿画面的长直线比例。

    建筑照片通常有成组的水平/竖直线（窗框、屋檐、墙面），自然风景的
    线条更零碎。这里在 64×64 小图上统计梯度，做到轻量且不依赖模型。
    """
    threshold = 26
    vertical_cols = [0] * size
    horizontal_rows = [0] * size
    edges = 0
    for y in range(size):
        row = [gray_pixels[x, y] for x in range(size)]
        for x in range(size - 1):
            if abs(row[x + 1] - row[x]) > threshold:
                vertical_cols[x] += 1
                edges += 1
        if y + 1 < size:
            for x in range(size):
                if abs(gray_pixels[x, y + 1] - row[x]) > threshold:
                    horizontal_rows[y] += 1
                    edges += 1

    strong = size * 0.42
    long_lines = sum(1 for value in vertical_cols if value >= strong)
    long_lines += sum(1 for value in horizontal_rows if value >= strong)
    line_score = long_lines / (2 * size)
    structure = edges / (2 * size * (size - 1))
    return structure, line_score


def _dhash(image: Image.Image) -> int:
    """8×8 差值哈希，用来衡量两张照片整体观感是否相似。"""
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = small.load()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if pixels[x, y] > pixels[x + 1, y] else 0)
    return bits


def analyze_image(image: Image.Image) -> PhotoProfile:
    """从缩略图上提取人物/建筑倾向、色相分布和整体相似度特征。"""
    size = ANALYSIS_SIZE
    rgb = image.convert("RGB").resize((size, size), Image.Resampling.BILINEAR)
    pixels = rgb.load()

    gray_pixels = rgb.convert("L").load()
    structure, line_score = _structure_features(gray_pixels, size)
    mask = bytearray(size * size)
    skin = 0
    dark = 0
    for y in range(size):
        for x in range(size):
            if gray_pixels[x, y] < 80:
                dark += 1
            r, g, b = pixels[x, y]
            if _is_skin(r, g, b):
                mask[y * size + x] = 1
                skin += 1

    total = size * size
    skin_ratio = skin / total
    dark_ratio = dark / total
    largest, largest_fill = _largest_skin_component(mask, size)
    largest_ratio = largest / total
    is_person = (
        # 常见人像：肤色是画面里一块紧凑、有边界的区域。
        0.015 <= skin_ratio <= 0.55
        and largest_ratio >= 0.006
        and largest_fill >= 0.28
    ) or (
        # 肤色背景/沙滩上的人像：整张图都接近肤色，但仍有头发、
        # 眼睛、衣服等深色结构；纯沙地、沙漠渐变通常没有。
        # 大面积的米色墙面、格窗也可能接近肤色，长直线很多时排除。
        skin_ratio > 0.55
        and dark_ratio >= 0.04
        and line_score < 0.08
    )

    hsv = rgb.convert("HSV")
    hsv_pixels = hsv.load()
    histogram = [0.0] * HUE_BINS
    saturation = 0.0
    brightness = 0.0
    neutral = 0
    sky = 0
    sky_area = max(1, size * (size // 3))
    for y in range(size):
        for x in range(size):
            h, s, v = hsv_pixels[x, y]
            weight = (s / 255.0) * (0.25 + 0.75 * v / 255.0) + 0.02
            histogram[min(HUE_BINS - 1, h * HUE_BINS // 256)] += weight
            saturation += s
            brightness += v
            if s < 45:
                neutral += 1
            if y < size // 3 and v >= 150 and (
                s <= 55 or (135 <= h <= 205 and s >= 25)
            ):
                sky += 1

    hist_total = sum(histogram) or 1.0
    saturation_ratio = saturation / total / 255.0
    neutral_ratio = neutral / total
    sky_ratio = sky / sky_area
    # 建筑感：画面里有较多长直线，且整体偏中性色/带天空。
    building_like = (
        line_score >= 0.085
        and structure >= 0.085
        and (
            sky_ratio >= 0.10
            or neutral_ratio >= 0.30
            or saturation_ratio <= 0.34
        )
    )
    # 米色墙面在统计上很像肤色；结构非常明确时优先归为建筑。
    if (
        building_like
        and line_score >= 0.11
        and structure >= 0.11
        and neutral_ratio >= 0.40
    ):
        is_person = False
    is_building = building_like and not is_person

    return PhotoProfile(
        is_person=is_person,
        is_building=is_building,
        skin_ratio=skin_ratio,
        largest_skin=largest_ratio,
        dark_ratio=dark_ratio,
        structure=structure,
        sky_ratio=sky_ratio,
        neutral_ratio=neutral_ratio,
        hue_hist=tuple(value / hist_total for value in histogram),
        saturation=saturation_ratio,
        brightness=brightness / total / 255.0,
        dhash=_dhash(image),
    )


def _hue_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """色相直方图的环形距离，0 表示色调一致，1 表示完全不同。"""
    total = 0.0
    half = HUE_BINS / 2
    for i, ai in enumerate(a):
        if ai <= 0:
            continue
        for j, bj in enumerate(b):
            if bj <= 0:
                continue
            diff = abs(i - j)
            diff = min(diff, HUE_BINS - diff) / half
            total += ai * bj * diff
    return min(1.0, total)


def photo_kind(photo: Photo) -> str:
    """照片的近似类别：人物 / 建筑 / 风景。"""
    profile = photo.profile
    if profile is None:
        return "风景"
    if profile.is_person:
        return "人物"
    if profile.is_building:
        return "建筑"
    return "风景"


# 感知哈希几乎重合，视为同一场景（同一机位、同一组连拍）。
SAME_SCENE_HAMMING = 0.18


def _group_similarity(a: Photo, b: Photo, kind: str) -> float:
    """同一类别内部的相似度（0 最相似），用于把照片串成连续分组。

    同场景的照片（感知哈希几乎重合）会被额外拉近；风景组更看重同色系，
    建筑组更看重构图和明暗，人物组则整体观感优先。
    """
    if a.profile is None or b.profile is None:
        return 0.5
    pa, pb = a.profile, b.profile
    hue = _hue_distance(pa.hue_hist, pb.hue_hist)
    saturation = abs(pa.saturation - pb.saturation)
    brightness = abs(pa.brightness - pb.brightness)
    hamming = (pa.dhash ^ pb.dhash).bit_count() / 64.0
    if kind == "人物":
        score = (
            0.40 * hamming
            + 0.30 * hue
            + 0.20 * saturation
            + 0.10 * brightness
        )
    elif kind == "风景":
        score = (
            0.55 * hue
            + 0.25 * hamming
            + 0.12 * saturation
            + 0.08 * brightness
        )
    else:  # 建筑
        score = 0.45 * hamming + 0.35 * hue + 0.20 * saturation
    if hamming <= SAME_SCENE_HAMMING:
        score *= 0.45
    return score


def _order_group(
    photos: list[Photo], kind: str, rng: random.Random
) -> list[Photo]:
    """在一个类别内部用最近邻排序，保留一点随机性。"""
    if len(photos) <= 1:
        return list(photos)
    remaining = list(photos)
    current = remaining.pop(rng.randrange(len(remaining)))
    ordered = [current]
    while remaining:
        scored = sorted(
            (
                (_group_similarity(current, photo, kind), photo)
                for photo in remaining
            ),
            key=lambda item: item[0],
        )
        best = scored[0][0]
        # 只在非常接近的候选之间随机，既保留变化，又不会打散分组。
        limit = best * 1.25 + 0.02
        pool = [photo for distance, photo in scored[:3] if distance <= limit]
        current = rng.choice(pool)
        remaining.remove(current)
        ordered.append(current)
    return ordered


def order_similar(photos: list[Photo], rng: random.Random) -> list[Photo]:
    """按优先级分组：同场景/人物/风景 → 同建筑 → 同色系。

    先按类别分桶（人物 → 风景 → 建筑），再在桶内用最近邻把观感相近、
    同场景或同色系的照片串在一起。这样同一类别的照片不会被其他类别拆开，
    建筑组也稳定排在人物/风景之后。
    """
    buckets: dict[str, list[Photo]] = {"人物": [], "风景": [], "建筑": []}
    for photo in photos:
        buckets[photo_kind(photo)].append(photo)
    ordered: list[Photo] = []
    for kind in ("人物", "风景", "建筑"):
        ordered.extend(_order_group(buckets[kind], kind, rng))
    return ordered


# ---- 拼接核心 -------------------------------------------------------------


@dataclass(eq=False)
class Photo:
    """一张已导入的照片，以及用户对它的手动裁剪设置。

    内存里只保留用于界面显示的缩小预览图（``preview``），以及原图的
    像素尺寸。导出拼接页时才会按 ``path`` 重新从原文件读取全分辨率图像，
    因此导入多少张都不会把原图全部塞进内存，导出质量也不受预览影响。
    """

    path: Path
    preview: Image.Image
    width: int  # 原图（已按 EXIF 方向摆正）的宽度
    height: int  # 原图（已按 EXIF 方向摆正）的高度
    zoom: float = 1.0
    offset_x: float = 0.0  # 裁剪中心位置，-1 靠左，1 靠右
    offset_y: float = 0.0  # -1 靠上，1 靠下
    rotation: int = 0  # 用户手动旋转，0/90/180/270
    profile: PhotoProfile | None = field(default=None, repr=False)
    _rotated_angle: int | None = field(default=None, init=False, repr=False)
    _rotated_image: Image.Image | None = field(default=None, init=False, repr=False)
    _full_image: Image.Image | None = field(default=None, init=False, repr=False)
    _full_angle: int | None = field(default=None, init=False, repr=False)
    _full_rotated: Image.Image | None = field(default=None, init=False, repr=False)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def image(self) -> Image.Image:
        """界面用的预览图（不是原图本身）。"""
        return self.preview

    def full_image(self) -> Image.Image:
        """从原文件读取全分辨率图像；重复调用会复用同一份缓存。"""
        if self._full_image is None:
            self._full_image = load_full_image(self.path)
        return self._full_image

    def release_full_image(self) -> None:
        """导出完一页后释放全分辨率副本，避免占用大量内存。"""
        self._full_image = None
        self._full_angle = None
        self._full_rotated = None

    def _orientation_mismatch(self, cell_w: int, cell_h: int) -> bool:
        """原图方向（横/竖）是否和格子方向不一致。"""
        image_portrait = self.height > self.width
        cell_portrait = cell_h > cell_w
        return image_portrait != cell_portrait

    def rotation_for_cell(
        self, cell_w: int, cell_h: int, auto_rotate: bool
    ) -> int:
        """返回这张照片在该格子里应使用的旋转角度。"""
        angle = self.rotation % 360
        if auto_rotate and self._orientation_mismatch(cell_w, cell_h):
            angle = (angle + 90) % 360
        return angle

    def oriented_image(
        self, angle: int, cache: bool = True, full: bool = False
    ) -> Image.Image:
        """按角度旋转后的图像。

        full=False 时使用界面预览图；full=True 时使用原图全分辨率副本。
        cache=False 时不保留旋转副本（导出时用于及时释放内存）。
        """
        angle %= 360
        base = self.full_image() if full else self.preview
        if angle == 0:
            return base
        transpose = {
            90: Image.Transpose.ROTATE_90,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_270,
        }[angle]
        if not cache:
            return base.transpose(transpose)
        if full:
            if self._full_angle != angle or self._full_rotated is None:
                self._full_rotated = base.transpose(transpose)
                self._full_angle = angle
            return self._full_rotated
        if self._rotated_angle != angle or self._rotated_image is None:
            self._rotated_image = base.transpose(transpose)
            self._rotated_angle = angle
        return self._rotated_image

    def clear_rotation_cache(self) -> None:
        self._rotated_angle = None
        self._rotated_image = None

    def rotate(self, delta: int = 90) -> None:
        self.rotation = (self.rotation + delta) % 360

    def clone_for_export(self) -> Photo:
        """复制一份轻量快照供导出线程使用，避免和界面操作互相影响。"""
        return Photo(
            path=self.path,
            preview=self.preview,
            width=self.width,
            height=self.height,
            zoom=self.zoom,
            offset_x=self.offset_x,
            offset_y=self.offset_y,
            rotation=self.rotation,
            profile=self.profile,
        )


@dataclass
class Page:
    """一页拼版：版式（2 或 4 格）和每个槽位的照片。"""

    mode: int
    photos: list[Photo | None]

    def __post_init__(self) -> None:
        if self.mode not in (2, 4):
            raise ValueError("mode must be 2 or 4")
        while len(self.photos) < self.mode:
            self.photos.append(None)
        del self.photos[self.mode :]

    def used_count(self) -> int:
        return sum(1 for photo in self.photos if photo is not None)


@dataclass(frozen=True)
class Cell:
    x: int
    y: int
    width: int
    height: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """把透明通道铺成白底，并统一成 RGB（不做任何缩放）。"""
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba)
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def load_full_image(path: str | Path) -> Image.Image:
    """按原文件的全分辨率读取，供导出使用。"""
    with Image.open(Path(path)) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
        return _flatten_to_rgb(image)


def load_photo(path: str | Path) -> Photo:
    """快速导入一张照片：只解码出界面预览图，原图留到导出时再读。

    JPEG 会先用 ``draft`` 让解码器按 1/2、1/4、1/8 缩小解码，再缩到
    预览尺寸，这样导入大图时速度快、占用内存少，也不会改动原文件。
    """
    path = Path(path)
    with Image.open(path) as opened:
        width, height = opened.size
        try:
            orientation = opened.getexif().get(0x0112, 1)
        except (AttributeError, OSError, ValueError):
            orientation = 1
        if orientation in (5, 6, 7, 8):
            width, height = height, width

        if opened.format == "JPEG":
            # 先让解码器按较小尺寸解码，再在预览图上应用 EXIF 旋转。
            opened.draft("RGB", (PREVIEW_MAX_SIZE * 2, PREVIEW_MAX_SIZE * 2))

        preview = ImageOps.exif_transpose(opened)
        preview.load()
        preview = _flatten_to_rgb(preview)
        if max(preview.size) > PREVIEW_MAX_SIZE:
            preview.thumbnail(
                (PREVIEW_MAX_SIZE, PREVIEW_MAX_SIZE),
                Image.Resampling.LANCZOS,
            )
        preview.load()

    return Photo(
        path=path,
        preview=preview,
        width=width,
        height=height,
        profile=analyze_image(preview),
    )


def unit_to_px(value: int | float, scale: float = 1.0) -> float:
    """把 Moldiv 风格的档位换算为输出像素。scale 用于预览缩放。"""
    return value * UNIT_MM / 25.4 * OUTPUT_DPI * scale


def layout_cells(
    width: int,
    height: int,
    mode: int,
    gap_px: float,
    margin_px: float,
) -> list[Cell]:
    """计算 2 张 / 4 张版式中每个照片格子的位置。"""
    if mode not in (2, 4):
        raise ValueError("mode must be 2 or 4")

    left = int(round(margin_px))
    top = int(round(margin_px))
    right = max(left + 1, width - int(round(margin_px)))
    bottom = max(top + 1, height - int(round(margin_px)))
    gap = max(0, int(round(gap_px)))

    def split(start: int, end: int) -> list[tuple[int, int]]:
        total = max(2, end - start - gap)
        first = max(1, total // 2)
        second = max(1, total - first)
        return [
            (start, start + first),
            (min(end - 1, start + first + gap), min(end, start + first + gap + second)),
        ]

    x_spans = split(left, right)
    y_spans = split(top, bottom) if mode == 4 else [(top, bottom)]

    cells: list[Cell] = []
    for y0, y1 in y_spans:
        for x0, x1 in x_spans:
            cells.append(Cell(x0, y0, max(1, x1 - x0), max(1, y1 - y0)))
    return cells


@dataclass(frozen=True)
class CropGeometry:
    """一张照片在某个格子里当前的裁剪窗口（原图像素坐标）。"""

    image: Image.Image
    angle: int
    crop_w: float
    crop_h: float
    center_x: float
    center_y: float
    max_center_x: float
    max_center_y: float

    @property
    def img_w(self) -> int:
        return self.image.width

    @property
    def img_h(self) -> int:
        return self.image.height

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (
            self.center_x - self.crop_w / 2,
            self.center_y - self.crop_h / 2,
            self.center_x + self.crop_w / 2,
            self.center_y + self.crop_h / 2,
        )


def crop_geometry(
    photo: Photo,
    cell_w: int,
    cell_h: int,
    auto_rotate: bool = True,
    cache: bool = True,
    full: bool = False,
) -> CropGeometry:
    """计算 cover 裁剪窗口，拖动和滚轮缩放都以它为基准。

    full=True 时基于原图全分辨率计算（导出用）；否则基于界面预览图。
    """
    cell_w = max(1, cell_w)
    cell_h = max(1, cell_h)
    angle = photo.rotation_for_cell(cell_w, cell_h, auto_rotate)
    image = photo.oriented_image(angle, cache=cache, full=full)
    img_w, img_h = image.size

    target_ratio = cell_w / cell_h
    image_ratio = img_w / img_h if img_h else 0.0

    # 先得到能完整覆盖格子的最大裁剪区域。
    if image_ratio > target_ratio:
        base_h = float(img_h)
        base_w = img_h * target_ratio
    else:
        base_w = float(img_w)
        base_h = img_w / target_ratio

    zoom = max(1.0, float(photo.zoom))
    crop_w = max(1.0, base_w / zoom)
    crop_h = max(1.0, base_h / zoom)
    max_center_x = max(0.0, (img_w - crop_w) / 2)
    max_center_y = max(0.0, (img_h - crop_h) / 2)
    center_x = img_w / 2 + max(-1.0, min(1.0, photo.offset_x)) * max_center_x
    center_y = img_h / 2 + max(-1.0, min(1.0, photo.offset_y)) * max_center_y
    return CropGeometry(
        image=image,
        angle=angle,
        crop_w=crop_w,
        crop_h=crop_h,
        center_x=center_x,
        center_y=center_y,
        max_center_x=max_center_x,
        max_center_y=max_center_y,
    )


def render_cell(
    photo: Photo,
    cell_w: int,
    cell_h: int,
    preview: bool = False,
    auto_rotate: bool = True,
    full: bool = False,
) -> Image.Image:
    """按 cover 方式裁剪并缩放一张照片到格子尺寸。

    导出（full=True）时使用原图全分辨率和完整的 LANCZOS 重采样，不做
    降低质量的快速缩放；预览时使用预览图，以速度优先。
    """
    cell_w = max(1, cell_w)
    cell_h = max(1, cell_h)
    geometry = crop_geometry(
        photo, cell_w, cell_h, auto_rotate=auto_rotate, cache=preview, full=full
    )
    if geometry.img_w <= 0 or geometry.img_h <= 0:
        return Image.new("RGB", (cell_w, cell_h), "white")
    if preview:
        return geometry.image.resize(
            (cell_w, cell_h),
            Image.Resampling.BILINEAR,
            box=geometry.box,
            reducing_gap=2.0,
        )
    return geometry.image.resize(
        (cell_w, cell_h),
        Image.Resampling.LANCZOS,
        box=geometry.box,
        reducing_gap=None,
    )


def zoom_photo_at(
    photo: Photo,
    cell_w: int,
    cell_h: int,
    auto_rotate: bool,
    fraction_x: float,
    fraction_y: float,
    new_zoom: float,
) -> None:
    """以格子里某个相对位置为锚点缩放，光标下的画面尽量保持不动。"""
    old = crop_geometry(photo, cell_w, cell_h, auto_rotate=auto_rotate)
    if old.img_w <= 0 or old.img_h <= 0:
        return
    fraction_x = max(0.0, min(1.0, fraction_x))
    fraction_y = max(0.0, min(1.0, fraction_y))
    point_x = old.center_x + (fraction_x - 0.5) * old.crop_w
    point_y = old.center_y + (fraction_y - 0.5) * old.crop_h

    photo.zoom = max(1.0, min(MAX_ZOOM, new_zoom))
    new = crop_geometry(photo, cell_w, cell_h, auto_rotate=auto_rotate)
    center_x = point_x - (fraction_x - 0.5) * new.crop_w
    center_y = point_y - (fraction_y - 0.5) * new.crop_h
    photo.offset_x = (
        0.0
        if new.max_center_x <= 0
        else max(-1.0, min(1.0, (center_x - new.img_w / 2) / new.max_center_x))
    )
    photo.offset_y = (
        0.0
        if new.max_center_y <= 0
        else max(-1.0, min(1.0, (center_y - new.img_h / 2) / new.max_center_y))
    )


def compose_page(
    photos: Iterable[Photo | None],
    mode: int,
    gap_units: int,
    margin_units: int,
    width: int = OUTPUT_WIDTH,
    height: int = OUTPUT_HEIGHT,
    background: str = "white",
    preview: bool = False,
    auto_rotate: bool = True,
    full: bool | None = None,
) -> Image.Image:
    """把一页的照片拼成一张完整图片，空槽位保留背景色。

    preview=False（导出）时默认使用原图全分辨率；preview=True 时使用
    内存里的预览图。
    """
    if full is None:
        full = not preview
    scale = width / OUTPUT_WIDTH
    gap_px = unit_to_px(gap_units, scale)
    margin_px = unit_to_px(margin_units, scale)
    cells = layout_cells(width, height, mode, gap_px, margin_px)
    canvas = Image.new("RGB", (width, height), background)

    for photo, cell in zip(list(photos), cells):
        if photo is None:
            continue
        rendered = render_cell(
            photo,
            cell.width,
            cell.height,
            preview=preview,
            auto_rotate=auto_rotate,
            full=full,
        )
        canvas.paste(rendered, (cell.x, cell.y))
    return canvas


def slot_label(index: int, mode: int) -> str:
    slot = index % mode
    if mode == 2:
        return ("左", "右")[slot]
    return ("左上", "右上", "左下", "右下")[slot]


# ---- 文件选择 -------------------------------------------------------------
#
# 主窗口用 overrideredirect(True) 自绘边框，在 WSLg 下 WM 管不到这个窗口，
# Tk 自带的文件对话框会被主窗口盖住（虽然拿得到焦点）。这里在 WSL 里改为
# 调用 Windows 原生的文件对话框：它是独立的 Windows 窗口，永远显示在最前面，
# 也带缩略图和「图片/桌面/下载」等常用位置。Windows 和普通 Linux 桌面继续
# 使用 Tk 自带的原生对话框。

WSL_PICK_MARKER = "__PIC_SPLICE_PICK__"
WSL_CANCEL_MARKER = "__PIC_SPLICE_CANCEL__"
WSL_ERROR_MARKER = "__PIC_SPLICE_ERROR__"

SAVE_FILETYPES = [("JPEG 图片", "*.jpg"), ("PNG 图片", "*.png")]


def running_in_wsl() -> bool:
    """当前进程是否运行在 WSL（WSLg）里。"""
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def _ps_literal(value: str) -> str:
    """生成 PowerShell 单引号字符串字面量。"""
    return "'" + value.replace("'", "''") + "'"


def _ps_filter(filetypes: list[tuple[str, str]]) -> str:
    """把 Tk 的 filetypes 转成 PowerShell 对话框的 Filter 格式。"""
    parts: list[str] = []
    for label, patterns in filetypes:
        parts.append(label.replace("|", ""))
        parts.append(patterns.replace(" ", ";"))
    return "|".join(parts)


def _windows_path_to_wsl(path: str) -> str:
    """把 Windows 对话框返回的路径转换成 WSL 里可用的 POSIX 路径。"""
    path = path.strip().strip('"')
    if not path or path.startswith("/"):
        return path
    try:
        result = subprocess.run(
            ["wslpath", "-u", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        converted = result.stdout.strip()
        if result.returncode == 0 and converted:
            return converted
    except (OSError, subprocess.SubprocessError):
        pass
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if match:
        return f"/mnt/{match.group(1).lower()}/" + match.group(2).replace("\\", "/")
    return path


def _run_windows_dialog(body: str) -> list[str] | None:
    """在 WSL 里运行一段 Windows 文件对话框脚本。

    返回值：
    - 选中路径列表（已转换成 WSL 路径）；
    - 用户取消时返回空列表；
    - PowerShell 无法启动或出错时返回 None（调用方回退到 Tk 对话框）。
    """
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "try {\n"
        f"{body}\n"
        "} catch {\n"
        f"  Write-Output ('{WSL_ERROR_MARKER}' + $_.Exception.Message)\n"
        "  exit 1\n"
        "}\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    output = completed.stdout.decode("utf-8", errors="replace")
    picks = [
        line[len(WSL_PICK_MARKER):].strip()
        for line in output.splitlines()
        if line.startswith(WSL_PICK_MARKER)
    ]
    if picks:
        return [_windows_path_to_wsl(path) for path in picks]
    if completed.returncode == 0 and WSL_CANCEL_MARKER in output:
        return []
    return None


def _windows_pick_files(
    title: str, filetypes: list[tuple[str, str]]
) -> list[str] | None:
    body = f"""
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = {_ps_literal(title)}
$dlg.Filter = {_ps_literal(_ps_filter(filetypes))}
$dlg.Multiselect = $true
$dlg.CheckFileExists = $true
$pictures = [Environment]::GetFolderPath('MyPictures')
if ($pictures -and (Test-Path $pictures)) {{ $dlg.InitialDirectory = $pictures }}
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
  foreach ($file in $dlg.FileNames) {{
    Write-Output ('{WSL_PICK_MARKER}' + $file)
  }}
}} else {{
  Write-Output '{WSL_CANCEL_MARKER}'
}}
"""
    return _run_windows_dialog(body)


def _windows_save_file(
    title: str,
    default_name: str,
    filetypes: list[tuple[str, str]],
    default_ext: str,
) -> str | None:
    body = f"""
$dlg = New-Object System.Windows.Forms.SaveFileDialog
$dlg.Title = {_ps_literal(title)}
$dlg.Filter = {_ps_literal(_ps_filter(filetypes))}
$dlg.FileName = {_ps_literal(default_name)}
$dlg.DefaultExt = {_ps_literal(default_ext)}
$dlg.AddExtension = $true
$dlg.OverwritePrompt = $true
$pictures = [Environment]::GetFolderPath('MyPictures')
if ($pictures -and (Test-Path $pictures)) {{ $dlg.InitialDirectory = $pictures }}
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
  Write-Output ('{WSL_PICK_MARKER}' + $dlg.FileName)
}} else {{
  Write-Output '{WSL_CANCEL_MARKER}'
}}
"""
    result = _run_windows_dialog(body)
    if result is None:
        return None
    return result[0] if result else ""


def _windows_pick_folder(title: str) -> str | None:
    body = f"""
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = {_ps_literal(title)}
$dlg.ShowNewFolderButton = $true
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
  Write-Output ('{WSL_PICK_MARKER}' + $dlg.SelectedPath)
}} else {{
  Write-Output '{WSL_CANCEL_MARKER}'
}}
"""
    result = _run_windows_dialog(body)
    if result is None:
        return None
    return result[0] if result else ""


# ---- 图形界面 -------------------------------------------------------------


def _round_rect_points(
    x0: float, y0: float, x1: float, y1: float, radius: float
) -> list[float]:
    """返回可用于 Canvas smooth 多边形的圆角矩形点列。"""
    radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    return [
        x0 + radius,
        y0,
        x1 - radius,
        y0,
        x1,
        y0,
        x1,
        y0 + radius,
        x1,
        y1 - radius,
        x1,
        y1,
        x1 - radius,
        y1,
        x0 + radius,
        y1,
        x0,
        y1,
        x0,
        y1 - radius,
        x0,
        y0 + radius,
        x0,
        y0,
    ]


def _canvas_round_rect(
    canvas: tk.Canvas,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radius: float,
    **kwargs,
) -> int:
    return canvas.create_polygon(
        _round_rect_points(x0, y0, x1, y1, radius),
        smooth=True,
        splinesteps=32,
        **kwargs,
    )


def _rounded_image(image: Image.Image, radius: float) -> Image.Image:
    """给 PIL 图片加圆角，保留原有的透明区域。"""
    image = image.convert("RGBA")
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1),
        radius=max(0, int(radius)),
        fill=255,
    )
    alpha = image.getchannel("A")
    alpha = ImageChops.multiply(alpha, mask)
    image.putalpha(alpha)
    return image


def make_list_thumbnail(image: Image.Image) -> Image.Image:
    """生成左侧照片列表用的 88×66 小图（可在后台线程里完成）。"""
    return ImageOps.pad(
        image.convert("RGBA"),
        (88, 66),
        color=(0, 0, 0, 0),
        method=Image.Resampling.LANCZOS,
    )


class SoftSlider(tk.Canvas):
    """圆角滑块：细药丸形轨道 + 圆形手柄。"""

    def __init__(
        self,
        parent: tk.Widget,
        variable: tk.Variable,
        from_: float,
        to: float,
        command=None,
        *,
        width: int = 120,
        height: int = 24,
        integer: bool = True,
        bg: str | None = None,
    ) -> None:
        self._variable = variable
        self._from = float(from_)
        self._to = float(to)
        self._command = command
        self._integer = integer
        self._enabled = True
        self._hover = False
        self._bg = bg or parent.cget("bg")
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<ButtonPress-1>", self._on_drag)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._trace = self._variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _size(self) -> tuple[int, int]:
        width = self.winfo_width()
        if width <= 2:
            width = int(float(self["width"]))
        height = self.winfo_height()
        if height <= 2:
            height = int(float(self["height"]))
        return max(8, width), max(8, height)

    def _value_x(self, width: int, pad: float) -> float:
        try:
            value = float(self._variable.get())
        except (TypeError, ValueError):
            value = self._from
        span = self._to - self._from
        ratio = 0.0 if span == 0 else (value - self._from) / span
        ratio = max(0.0, min(1.0, ratio))
        return pad + ratio * (width - pad * 2)

    def _draw(self) -> None:
        width, height = self._size()
        self.delete("all")
        pad = 10
        center_y = height / 2
        trough = 6
        _canvas_round_rect(
            self,
            pad,
            center_y - trough / 2,
            width - pad,
            center_y + trough / 2,
            trough / 2,
            fill="#e5daeb",
            outline="",
        )
        knob_x = self._value_x(width, pad)
        if knob_x > pad + trough:
            _canvas_round_rect(
                self,
                pad,
                center_y - trough / 2,
                knob_x,
                center_y + trough / 2,
                trough / 2,
                fill="#d9c7e5",
                outline="",
            )
        radius = 8
        knob_fill = ACCENT if self._enabled else "#cfc4d7"
        if self._hover and self._enabled:
            knob_fill = ACCENT_HOVER
        self.create_oval(
            knob_x - radius,
            center_y - radius,
            knob_x + radius,
            center_y + radius,
            fill=knob_fill,
            outline=self._bg,
            width=2,
        )

    def _apply_x(self, x: float) -> None:
        width, _height = self._size()
        pad = 10
        ratio = (x - pad) / max(1, width - pad * 2)
        ratio = max(0.0, min(1.0, ratio))
        value = self._from + ratio * (self._to - self._from)
        value = int(round(value)) if self._integer else round(value, 2)
        if value != self._variable.get():
            self._variable.set(value)
        if self._command is not None:
            self._command(str(value))

    def _on_drag(self, event: tk.Event) -> None:
        if not self._enabled:
            return
        self._apply_x(event.x)

    def _on_enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()

    def set_state(self, state: str) -> None:
        self._enabled = state != "disabled"
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()


class SegmentedControl(tk.Canvas):
    """药丸形的分段选择器，替代系统单选框。"""

    def __init__(
        self,
        parent: tk.Widget,
        options: list[tuple[str, int]],
        variable: tk.IntVar,
        command=None,
        *,
        font: tkfont.Font | None = None,
        height: int = 32,
        bg: str | None = None,
    ) -> None:
        self._font = font or tkfont.nametofont("TkDefaultFont")
        self._bold_font = tkfont.Font(font=self._font)
        self._bold_font.configure(weight="bold")
        self._segments = list(options)
        self._variable = variable
        self._command = command
        self._height = height
        self._widths = [self._font.measure(label) + 28 for label, _ in options]
        self._hover: int | None = None
        self._bg = bg or parent.cget("bg")
        total = sum(self._widths) + 4
        super().__init__(
            parent,
            width=total,
            height=height,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._trace = self._variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _index_at(self, x: float) -> int | None:
        cursor = 2.0
        for index, width in enumerate(self._widths):
            if cursor <= x < cursor + width:
                return index
            cursor += width
        return None

    def _draw(self) -> None:
        width = int(float(self["width"]))
        height = self._height
        self.delete("all")
        _canvas_round_rect(
            self,
            0.5,
            0.5,
            width - 0.5,
            height - 0.5,
            height / 2,
            fill=CARD_BG,
            outline=BORDER,
            width=1,
        )
        selected = self._variable.get()
        cursor = 2.0
        for index, (label, value) in enumerate(self._segments):
            segment = self._widths[index]
            if index == self._hover and value != selected:
                _canvas_round_rect(
                    self,
                    cursor + 2,
                    3,
                    cursor + segment - 2,
                    height - 3,
                    (height - 6) / 2,
                    fill=CARD_HOVER,
                    outline="",
                )
            if value == selected:
                _canvas_round_rect(
                    self,
                    cursor + 2,
                    3,
                    cursor + segment - 2,
                    height - 3,
                    (height - 6) / 2,
                    fill="#e8dcef",
                    outline="#ddcce7",
                    width=1,
                )
            self.create_text(
                cursor + segment / 2,
                height / 2 + 1,
                text=label,
                font=self._bold_font if value == selected else self._font,
                fill=ACCENT_PRESS if value == selected else MUTED,
            )
            cursor += segment

    def _on_motion(self, event: tk.Event) -> None:
        hover = self._index_at(event.x)
        if hover != self._hover:
            self._hover = hover
            self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        if self._hover is not None:
            self._hover = None
            self._draw()

    def _on_click(self, event: tk.Event) -> None:
        index = self._index_at(event.x)
        if index is None:
            return
        _label, value = self._segments[index]
        if self._variable.get() != value:
            self._variable.set(value)
            if self._command is not None:
                self._command()


class TogglePill(tk.Canvas):
    """带小圆点的开关药丸，替代系统复选框。"""

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        variable: tk.BooleanVar,
        command=None,
        *,
        font: tkfont.Font | None = None,
        height: int = 30,
        bg: str | None = None,
    ) -> None:
        self._font = font or tkfont.nametofont("TkDefaultFont")
        self._text = text
        self._variable = variable
        self._command = command
        self._height = height
        self._hover = False
        self._bg = bg or parent.cget("bg")
        width = self._font.measure(text) + 48
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        self._width = width
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._trace = self._variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self) -> None:
        on = bool(self._variable.get())
        width = self._width
        height = self._height
        self.delete("all")
        fill = ACCENT_SOFT if on else CARD_BG
        if self._hover:
            fill = "#e3d6ec" if on else CARD_HOVER
        _canvas_round_rect(
            self,
            0.5,
            0.5,
            width - 0.5,
            height - 0.5,
            height / 2,
            fill=fill,
            outline=BORDER if not on else "#dccde7",
            width=1,
        )
        cx = 15
        cy = height / 2
        radius = 5
        if on:
            self.create_oval(
                cx - radius,
                cy - radius,
                cx + radius,
                cy + radius,
                fill=ACCENT,
                outline="",
            )
        else:
            self.create_oval(
                cx - radius,
                cy - radius,
                cx + radius,
                cy + radius,
                fill=self._bg,
                outline="#cbbfd3",
                width=1,
            )
        self.create_text(
            26,
            cy + 1,
            text=self._text,
            font=self._font,
            fill=ACCENT_PRESS if on else MUTED,
            anchor="w",
        )

    def _on_click(self, _event: tk.Event) -> None:
        self._variable.set(not self._variable.get())
        if self._command is not None:
            self._command()

    def _on_enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()


class PillButton(tk.Canvas):
    """Canvas 绘制的圆角药丸按钮，比 ttk 按钮更柔和。"""

    _STYLES = {
        # kind: (常态, 悬停, 按下, 文字)
        "primary": (ACCENT, ACCENT_HOVER, ACCENT_PRESS, "#fbf7fc"),
        "soft": (ACCENT_SOFT, "#e3d6ec", "#dccde8", ACCENT_PRESS),
        "quiet": (CARD_BG, CARD_HOVER, CARD_SELECTED, TEXT),
        "ghost": (TITLEBAR_BG, CARD_HOVER, CARD_SELECTED, MUTED),
    }

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command,
        *,
        kind: str = "quiet",
        font: tkfont.Font | None = None,
        height: int = 34,
        padx: int = 18,
        parent_bg: str | None = None,
    ) -> None:
        self._font = font or tkfont.nametofont("TkDefaultFont")
        self._command = command
        self._enabled = True
        bg = parent_bg or parent.cget("bg")
        width = self._font.measure(text) + padx * 2
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        self._normal, self._hover, self._press, self._text_color = self._STYLES[
            kind
        ]
        self._height = height
        self._shape = _canvas_round_rect(
            self,
            1,
            1,
            width - 1,
            height - 1,
            height / 2,
            fill=self._normal,
            outline="",
        )
        self._label = self.create_text(
            width / 2,
            height / 2 + 1,
            text=text,
            font=self._font,
            fill=self._text_color,
        )
        self._paint(self._normal, self._text_color)
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda _e: self._paint(self._hover, self._text_color))
        self.bind("<Leave>", lambda _e: self._paint(self._normal, self._text_color))
        self.bind("<ButtonPress-1>", lambda _e: self._paint(self._press, self._text_color))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _paint(self, fill: str, text_color: str) -> None:
        if not self._enabled:
            fill, text_color = "#f0eaf2", "#bdb3c4"
        self.itemconfigure(self._shape, fill=fill)
        self.itemconfigure(self._label, fill=text_color)

    def _on_release(self, event: tk.Event) -> None:
        if not self._enabled:
            return
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._paint(self._hover if inside else self._normal, self._text_color)
        if inside:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint(self._normal, self._text_color)


class ProgressBar(tk.Canvas):
    """细长的圆角进度条，跟随父容器宽度自动重绘。"""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        height: int = 10,
        bg: str | None = None,
    ) -> None:
        self._bg = bg or parent.cget("bg")
        super().__init__(
            parent,
            height=height,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        self._fraction = 0.0
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def set_fraction(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        if abs(fraction - self._fraction) < 0.001:
            return
        self._fraction = fraction
        self._draw()

    def _draw(self) -> None:
        width = self.winfo_width()
        if width <= 2:
            width = int(float(self["width"]))
        height = self.winfo_height()
        if height <= 2:
            height = int(float(self["height"]))
        width = max(8, width)
        height = max(6, height)
        self.delete("all")
        radius = height / 2
        _canvas_round_rect(
            self,
            0,
            0,
            width,
            height,
            radius,
            fill="#e8dfee",
            outline="",
        )
        fill_width = width * self._fraction
        if fill_width >= 2:
            _canvas_round_rect(
                self,
                0,
                0,
                max(radius * 2, fill_width),
                height,
                radius,
                fill=ACCENT,
                outline="",
            )


class WindowControls(tk.Canvas):
    """自定义标题栏右侧的最小化 / 最大化 / 关闭按钮。"""

    def __init__(
        self,
        parent: tk.Widget,
        on_minimize,
        on_maximize,
        on_close,
        *,
        bg: str = TITLEBAR_BG,
    ) -> None:
        super().__init__(
            parent, width=108, height=36, bg=bg, highlightthickness=0, bd=0
        )
        self._bg = bg
        self._items: list[tuple[str, int, int]] = []
        self._maximized = False
        buttons = [
            ("–", on_minimize, "#7e7488", ACCENT_SOFT, ACCENT_PRESS),
            ("□", on_maximize, "#7e7488", ACCENT_SOFT, ACCENT_PRESS),
            ("✕", on_close, "#9c7f89", DANGER_SOFT, DANGER),
        ]
        for index, (glyph, action, color, hover_fill, hover_text) in enumerate(
            buttons
        ):
            cx = 18 + index * 36
            tag = f"wc{index}"
            oval = self.create_oval(
                cx - 12,
                6,
                cx + 12,
                30,
                fill=bg,
                outline="",
                tags=(tag,),
            )
            label = self.create_text(
                cx,
                18,
                text=glyph,
                font=(self._font_family(), 12),
                fill=color,
                tags=(tag,),
            )

            def enter(
                _event,
                oval=oval,
                label=label,
                fill=hover_fill,
                text=hover_text,
            ) -> None:
                self.itemconfigure(oval, fill=fill)
                self.itemconfigure(label, fill=text)

            def leave(_event, oval=oval, label=label, color=color) -> None:
                self.itemconfigure(oval, fill=bg)
                self.itemconfigure(label, fill=color)

            self.tag_bind(tag, "<Enter>", enter)
            self.tag_bind(tag, "<Leave>", leave)
            self.tag_bind(tag, "<Button-1>", lambda _e, action=action: action())
            self._items.append((tag, label, oval))
        self.configure(cursor="hand2")

    @staticmethod
    def _font_family() -> str:
        available = set(tkfont.families())
        for family in ("Noto Sans CJK SC", "Microsoft YaHei UI", "PingFang SC",
                       "Source Han Sans SC", "WenQuanYi Micro Hei", "DejaVu Sans"):
            if family in available:
                return family
        return "TkDefaultFont"

    def set_maximized(self, maximized: bool) -> None:
        self._maximized = maximized
        self.itemconfigure(self._items[1][1], text="❐" if maximized else "□")


class ResizeGrip(tk.Canvas):
    """无系统边框窗口右下角的拖动缩放手柄。"""

    def __init__(
        self,
        parent: tk.Widget,
        target: tk.Tk,
        min_width: int,
        min_height: int,
        *,
        bg: str = BG,
    ) -> None:
        super().__init__(
            parent, width=22, height=22, bg=bg, highlightthickness=0, bd=0
        )
        self._target = target
        self._min_width = min_width
        self._min_height = min_height
        self._start: tuple[int, int, int, int] | None = None
        for offset in (7, 12, 17):
            self.create_line(
                20 - offset,
                20,
                20,
                20 - offset,
                fill="#c9bcd2",
                width=1,
                capstyle="round",
            )
        try:
            self.configure(cursor="bottom_right_corner")
        except tk.TclError:
            self.configure(cursor="sizing")
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event: tk.Event) -> None:
        self._start = (
            event.x_root,
            event.y_root,
            self._target.winfo_width(),
            self._target.winfo_height(),
        )

    def _on_motion(self, event: tk.Event) -> None:
        if self._start is None:
            return
        start_x, start_y, start_w, start_h = self._start
        width = max(self._min_width, start_w + event.x_root - start_x)
        height = max(self._min_height, start_h + event.y_root - start_y)
        self._target.geometry(f"{width}x{height}")

    def _on_release(self, _event: tk.Event) -> None:
        self._start = None


class RestoreBubble:
    """最小化后停留在右下角的小胶囊，点击即可恢复主窗口。

    WSLg 里让 override-redirect 窗口走系统 iconify 会让 X 连接断开，
    所以这里用「隐藏主窗口 + 悬浮恢复按钮」的方式实现最小化。
    """

    WIDTH = 212
    HEIGHT = 48

    def __init__(
        self,
        root: tk.Tk,
        on_restore,
        title_font: tkfont.Font,
        tiny_font: tkfont.Font,
    ) -> None:
        self._on_restore = on_restore
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        try:
            self.window.attributes("-topmost", True)
            self.window.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(
            self.window,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=PANEL_BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.canvas.pack()
        _canvas_round_rect(
            self.canvas,
            1,
            1,
            self.WIDTH - 1,
            self.HEIGHT - 1,
            self.HEIGHT / 2,
            fill=PANEL_BG,
            outline=BORDER,
            width=1,
        )
        _canvas_round_rect(
            self.canvas, 12, 11, 37, 36, 9, fill=ACCENT, outline=""
        )
        self.canvas.create_oval(25, 14, 30, 19, fill="#f6e6eb", outline="")
        self.canvas.create_polygon(
            [15, 31, 21, 23, 25, 28, 28, 25, 33, 31],
            fill="#f3eaf6",
            outline="",
        )
        self.canvas.create_text(
            47,
            17,
            anchor="w",
            text="Picture Splice",
            font=title_font,
            fill=TEXT,
        )
        self.canvas.create_text(
            47,
            33,
            anchor="w",
            text="已最小化 · 点此恢复",
            font=tiny_font,
            fill=MUTED,
        )
        for widget in (self.window, self.canvas):
            widget.bind("<Button-1>", lambda _e: self._restore())

    def show(self, x: float, y: float) -> None:
        self.window.geometry(
            f"{self.WIDTH}x{self.HEIGHT}+{int(x)}+{int(y)}"
        )
        self.window.deiconify()
        try:
            self.window.lift()
        except tk.TclError:
            pass

    def hide(self) -> None:
        self.window.withdraw()

    def _restore(self) -> None:
        self.hide()
        self._on_restore()


class FloatingGhost:
    """拖动左侧照片时跟随鼠标的半透明缩略图。"""

    def __init__(self, root: tk.Tk) -> None:
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        try:
            self.window.attributes("-topmost", True)
            self.window.attributes("-alpha", 0.78)
        except tk.TclError:
            pass
        self.frame = tk.Frame(
            self.window,
            bg=PANEL_BG,
            highlightthickness=1,
            highlightbackground="#ddd1e5",
        )
        self.frame.pack()
        self.label = tk.Label(self.frame, bg=PANEL_BG, bd=0)
        self.label.pack(padx=4, pady=4)
        self._image: ImageTk.PhotoImage | None = None
        self._offset = (18, 18)

    def show(self, image: Image.Image, x_root: int, y_root: int) -> None:
        thumb = ImageOps.pad(
            image.convert("RGBA"),
            (150, 112),
            color=(0, 0, 0, 0),
            method=Image.Resampling.LANCZOS,
        )
        self._image = ImageTk.PhotoImage(_rounded_image(thumb, 10))
        self.label.configure(image=self._image)
        self.move(x_root, y_root)
        self.window.deiconify()
        try:
            self.window.lift()
        except tk.TclError:
            pass

    def move(self, x_root: int, y_root: int) -> None:
        if self._image is None:
            return
        self.window.geometry(
            f"+{x_root + self._offset[0]}+{y_root + self._offset[1]}"
        )

    def hide(self) -> None:
        self.window.withdraw()
        self.label.configure(image="")
        self._image = None


class PhotoSplicerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._min_width = 1080
        self._min_height = 740
        self._maximized = False
        self._restore_geometry = ""
        self._window_drag: tuple[int, int] | None = None

        # 使用自绘标题栏：去掉系统边框，窗口更柔和的浅色系才能完整呈现。
        self.root.withdraw()
        self.root.title("Picture Splice")
        self.root.configure(bg=BORDER)
        self.root.overrideredirect(True)
        self.root.minsize(self._min_width, self._min_height)
        self._center_window(1320, 880)
        self.shell = tk.Frame(root, bg=BG, bd=0, highlightthickness=0)
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)

        self.photos: list[Photo] = []
        # 自动拼接页（工具按场景随机生成）和用户自己建立的空白拼接页。
        self.pages: list[Page] = [Page(mode=2, photos=[None, None])]
        self.custom_pages: list[Page] = []
        self.selected_index: int | None = None
        self.active_slot: tuple[int, int] | None = None
        self.active_custom_slot: int | None = None
        self.current_page = 0
        self.current_custom_page = 0
        # 大型预览区当前显示的内容：单张照片 / 自动拼接页 / 自定义拼接页。
        self.preview_kind = "auto"
        self.preview_photo_index: int | None = None
        self.mode = tk.IntVar(value=0)  # 2 / 4 / 0（自动混排）
        self.auto_rotate = tk.BooleanVar(value=True)
        self.gap_units = tk.IntVar(value=DEFAULT_GAP)
        self.margin_units = tk.IntVar(value=DEFAULT_MARGIN)
        self.zoom_value = tk.IntVar(value=100)
        self._rng = random.Random()

        self._preview_image: ImageTk.PhotoImage | None = None
        self._preview_composite: Image.Image | None = None
        self._preview_origin = (0, 0)
        self._preview_scale = 1.0
        self._preview_cells: list[Cell] = []
        self._preview_page_photos: list[Photo | None] = []
        self._render_job: str | None = None
        self._resize_job: str | None = None
        self._syncing_zoom = False
        self._drag: dict | None = None
        self._drag_moved = False
        self._drag_trail: list[tuple[float, float]] = []
        self._drag_ghost_images: list[ImageTk.PhotoImage] = []
        self._drop_hover: int | None = None
        self._card_drag: dict | None = None
        self._thumb_refs: list[ImageTk.PhotoImage] = []
        self._thumb_cache: dict[tuple[str, int], ImageTk.PhotoImage] = {}
        self._list_building = False
        self._list_queue: list[int] = []
        self._list_revision = 0
        self._card_job: str | None = None
        self._pending_list_status: str | None = None

        # 中间 / 右侧两个拼接页列表的缩略图与分批生成状态。
        self._page_thumb_cache: dict[tuple, ImageTk.PhotoImage] = {}
        self._auto_thumb_refs: list[ImageTk.PhotoImage] = []
        self._custom_thumb_refs: list[ImageTk.PhotoImage] = []
        self._page_list_job: str | None = None
        self._page_list_revision = 0
        self._page_build_queue: list[tuple[str, int]] = []
        self._page_build_job: str | None = None

        # 三栏的折叠状态，以及从资源管理器拖入文件时的目标拼接页。
        self._panels: dict[str, dict] = {}
        self._paned: tk.PanedWindow | None = None
        self._import_drop_page: int | None = None
        self._import_drop_slot = 0
        self._import_drop_mode = 2
        self._import_drop_active = False
        self._import_drop_last_page = 0
        self._import_drop_last_slot = 0

        # 后台导入 / 导出的进度状态。
        self._importing = False
        self._import_thread: threading.Thread | None = None
        self._import_queue: queue.Queue | None = None
        self._import_cancel: threading.Event | None = None
        self._import_poll_job: str | None = None
        self._import_total = 0
        self._import_done = 0
        self._import_added: list[Photo] = []
        self._import_errors: list[str] = []
        self._import_ordered: list[Photo] | None = None
        self._exporting = False
        self._export_thread: threading.Thread | None = None
        self._export_queue: queue.Queue | None = None
        self._export_poll_job: str | None = None
        self._export_total = 0
        self._export_done = 0
        self._exported_paths: list[Path] = []
        self._export_error: str | None = None
        self._export_folder: str = ""
        self._export_single = False

        self._configure_styles()
        self._build_ui()
        self._restore_bubble = RestoreBubble(
            self.root,
            self._restore_window,
            self._fonts["section"],
            self._fonts["tiny"],
        )
        self._floating_ghost = FloatingGhost(self.root)
        self._refresh_all()
        self.root.deiconify()
        self.root.lift()
        self.root.after(120, self.root.focus_force)

    # -- 样式与布局 ---------------------------------------------------------

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self.font_family = self._pick_font()
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family=self.font_family, size=10)
        text_font = tkfont.nametofont("TkTextFont")
        text_font.configure(family=self.font_family, size=10)
        heading = tkfont.Font(family=self.font_family, size=18, weight="bold")
        subheading = tkfont.Font(family=self.font_family, size=10)
        section = tkfont.Font(family=self.font_family, size=11, weight="bold")
        title = tkfont.Font(family=self.font_family, size=15, weight="bold")
        button = tkfont.Font(family=self.font_family, size=10, weight="bold")
        tiny = tkfont.Font(family=self.font_family, size=8)
        segment = tkfont.Font(family=self.font_family, size=9, weight="bold")
        toggle = tkfont.Font(family=self.font_family, size=9)
        self._fonts = {
            "heading": heading,
            "subheading": subheading,
            "section": section,
            "title": title,
            "button": button,
            "tiny": tiny,
            "segment": segment,
            "toggle": toggle,
        }

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=PANEL_BG, foreground=TEXT)
        style.configure(
            "Muted.TLabel",
            background=PANEL_BG,
            foreground=MUTED,
            font=(self.font_family, 9),
        )
        style.configure(
            "Accent.TButton",
            font=(self.font_family, 10, "bold"),
            padding=(16, 9),
            background=ACCENT,
            foreground="white",
            borderwidth=0,
            focusthickness=0,
            relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[
                ("active", ACCENT_HOVER),
                ("pressed", ACCENT_PRESS),
                ("disabled", "#d9cfe1"),
            ],
            foreground=[("disabled", "#f7f2f8")],
        )
        style.configure(
            "Quiet.TButton",
            font=(self.font_family, 9),
            padding=(12, 7),
            background=PANEL_BG,
            foreground=TEXT,
            borderwidth=1,
            bordercolor=BORDER,
            lightcolor=PANEL_BG,
            darkcolor=PANEL_BG,
            relief="flat",
            focusthickness=0,
        )
        style.map(
            "Quiet.TButton",
            background=[("active", ACCENT_SOFT), ("pressed", CARD_SELECTED)],
            bordercolor=[("active", "#dccfe6")],
            foreground=[("active", TEXT)],
        )
        style.configure(
            "Small.TButton",
            font=(self.font_family, 9),
            padding=(9, 5),
            background=CARD_BG,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        style.map(
            "Small.TButton",
            background=[
                ("active", CARD_HOVER),
                ("pressed", CARD_SELECTED),
                ("disabled", "#f1ebf3"),
            ],
            foreground=[("active", TEXT), ("disabled", "#b7adbe")],
        )
        style.configure(
            "Tiny.TButton",
            font=(self.font_family, 8),
            padding=(6, 2),
            background=CARD_BG,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        style.map(
            "Tiny.TButton",
            background=[
                ("active", CARD_HOVER),
                ("pressed", CARD_SELECTED),
                ("disabled", "#f1ebf3"),
            ],
            foreground=[("active", TEXT), ("disabled", "#b7adbe")],
        )
        style.configure(
            "TRadiobutton",
            background=PANEL_BG,
            foreground=TEXT,
            font=(self.font_family, 10),
            indicatorbackground=PANEL_BG,
            indicatorforeground=ACCENT,
            indicatorsize=12,
        )
        style.map(
            "TRadiobutton",
            background=[("active", PANEL_BG)],
            foreground=[("active", ACCENT_PRESS)],
            indicatorbackground=[("selected", ACCENT_SOFT)],
        )
        style.configure(
            "TCheckbutton",
            background=PANEL_BG,
            foreground=TEXT,
            font=(self.font_family, 10),
            indicatorbackground=PANEL_BG,
            indicatorforeground=ACCENT,
            indicatorsize=12,
        )
        style.map(
            "TCheckbutton",
            background=[("active", PANEL_BG)],
            indicatorbackground=[("selected", ACCENT_SOFT)],
        )
        style.configure(
            "Soft.Vertical.TScrollbar",
            gripcount=0,
            background=CARD_BG,
            darkcolor=PANEL_BG,
            lightcolor=PANEL_BG,
            troughcolor=PANEL_BG,
            bordercolor=PANEL_BG,
            arrowcolor=MUTED,
            width=10,
        )
        style.map(
            "Soft.Vertical.TScrollbar",
            background=[("active", "#ddd1e5"), ("pressed", ACCENT_SOFT)],
        )

    @staticmethod
    def _pick_font() -> str:
        available = set(tkfont.families())
        for family in (
            "Noto Sans CJK SC",
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "PingFang SC",
            "Source Han Sans SC",
            "WenQuanYi Micro Hei",
            "DejaVu Sans",
        ):
            if family in available:
                return family
        return "TkDefaultFont"

    def _center_window(self, width: int, height: int) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(24, (screen_h - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self) -> None:
        self._build_titlebar(self.shell)

        body = tk.Frame(self.shell, bg=BG)
        body.pack(fill="both", expand=True)

        # 三栏列表 + 预览区。分栏之间可以直接拖动调整宽度，每栏还能向左收起。
        paned = tk.PanedWindow(
            body,
            orient="horizontal",
            bd=0,
            sashwidth=7,
            sashrelief="flat",
            showhandle=True,
            handlesize=8,
            bg=BG,
            opaqueresize=True,
        )
        paned.pack(fill="both", expand=True, padx=(14, 14), pady=(6, 14))
        self._paned = paned

        photos_panel = self._build_sidebar(paned)
        auto_panel = self._build_auto_panel(paned)
        custom_panel = self._build_custom_panel(paned)
        workspace = self._build_workspace(paned)

        paned.add(photos_panel, minsize=180, width=300, stretch="never")
        paned.add(auto_panel, minsize=190, width=225, stretch="never")
        paned.add(custom_panel, minsize=190, width=235, stretch="never")
        paned.add(workspace, minsize=440, stretch="always")

        self._setup_external_dnd()

        self.resize_grip = ResizeGrip(
            self.shell, self.root, self._min_width, self._min_height
        )
        self.resize_grip.place(relx=1.0, rely=1.0, x=-7, y=-7, anchor="se")

    def _make_panel(
        self,
        parent: tk.Widget,
        name: str,
        collapsed_text: str,
        default_width: int,
        min_width: int,
    ) -> tuple[tk.Frame, tk.Frame]:
        """创建一栏带折叠状态的浅色面板，返回 (面板, 正常内容区)。"""
        panel = tk.Frame(
            parent,
            bg=PANEL_BG,
            highlightthickness=1,
            highlightbackground="#e7dcec",
        )
        content = tk.Frame(panel, bg=PANEL_BG)
        content.pack(fill="both", expand=True)

        collapsed = tk.Frame(panel, bg=PANEL_BG, cursor="hand2")
        arrow = tk.Label(
            collapsed,
            text="›",
            bg=PANEL_BG,
            fg=ACCENT,
            font=(self.font_family, 13, "bold"),
            cursor="hand2",
        )
        arrow.pack(side="top", pady=(12, 2))
        label = tk.Label(
            collapsed,
            text=collapsed_text,
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 9, "bold"),
            justify="center",
            cursor="hand2",
        )
        label.pack(side="top")
        for widget in (collapsed, arrow, label):
            widget.bind(
                "<Button-1>", lambda _event, key=name: self._expand_panel(key)
            )

        self._panels[name] = {
            "panel": panel,
            "content": content,
            "collapsed": collapsed,
            "width": default_width,
            "min_width": min_width,
            "expanded": True,
        }
        return panel, content

    def _collapse_button(self, parent: tk.Widget, name: str) -> tk.Label:
        button = tk.Label(
            parent,
            text="‹",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 13, "bold"),
            cursor="hand2",
            padx=4,
        )
        button.bind(
            "<Button-1>", lambda _event, key=name: self._collapse_panel(key)
        )
        button.bind(
            "<Enter>", lambda _event: button.configure(fg=ACCENT)
        )
        button.bind(
            "<Leave>", lambda _event: button.configure(fg=MUTED)
        )
        return button

    def _collapse_panel(self, name: str) -> None:
        meta = self._panels.get(name)
        if meta is None or not meta["expanded"] or self._paned is None:
            return
        width = meta["panel"].winfo_width()
        if width > 60:
            meta["width"] = width
        meta["content"].pack_forget()
        meta["collapsed"].pack(fill="both", expand=True)
        self._paned.paneconfigure(meta["panel"], minsize=38, width=38)
        meta["expanded"] = False

    def _expand_panel(self, name: str) -> None:
        meta = self._panels.get(name)
        if meta is None or meta["expanded"] or self._paned is None:
            return
        meta["collapsed"].pack_forget()
        meta["content"].pack(fill="both", expand=True)
        self._paned.paneconfigure(
            meta["panel"],
            minsize=meta["min_width"],
            width=meta["width"],
        )
        meta["expanded"] = True

    def _build_titlebar(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=TITLEBAR_BG, height=66, cursor="fleur")
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        tk.Frame(parent, bg=BORDER, height=1).pack(side="top", fill="x")

        left = tk.Frame(bar, bg=TITLEBAR_BG, cursor="fleur")
        left.pack(side="left", padx=(18, 0), pady=10)

        logo = tk.Canvas(
            left,
            width=34,
            height=34,
            bg=TITLEBAR_BG,
            highlightthickness=0,
            bd=0,
            cursor="fleur",
        )
        logo.pack(side="left")
        logo.create_polygon(
            _round_rect_points(2, 8, 26, 32, 8),
            smooth=True,
            splinesteps=24,
            fill=ACCENT_SOFT,
            outline="",
        )
        logo.create_polygon(
            _round_rect_points(8, 2, 32, 26, 8),
            smooth=True,
            splinesteps=24,
            fill=ACCENT,
            outline="",
        )
        logo.create_oval(20, 6, 26, 12, fill="#f6e6eb", outline="")
        logo.create_polygon(
            [10, 23, 17, 14, 22, 20, 26, 16, 31, 23],
            fill="#f3eaf6",
            outline="",
        )

        title_box = tk.Frame(left, bg=TITLEBAR_BG, cursor="fleur")
        title_box.pack(side="left", padx=(10, 0))
        title = tk.Label(
            title_box,
            text="Picture Splice",
            font=self._fonts["title"],
            bg=TITLEBAR_BG,
            fg=TEXT,
            cursor="fleur",
        )
        title.pack(anchor="w")
        subtitle = tk.Label(
            title_box,
            text="柔和拼版 · 300 dpi · 同场景优先",
            font=self._fonts["tiny"],
            bg=TITLEBAR_BG,
            fg=MUTED,
            cursor="fleur",
        )
        subtitle.pack(anchor="w")

        self.window_controls = WindowControls(
            bar,
            self._minimize_window,
            self._toggle_maximize,
            self._close_window,
            bg=TITLEBAR_BG,
        )
        self.window_controls.pack(side="right", padx=(12, 10), pady=15)

        self.export_button = PillButton(
            bar,
            "导出全部",
            self.export_all,
            kind="primary",
            font=self._fonts["button"],
            parent_bg=TITLEBAR_BG,
        )
        self.export_button.pack(side="right", pady=16)
        self.export_page_button = PillButton(
            bar,
            "导出本页",
            self.export_current_page,
            kind="quiet",
            font=self._fonts["button"],
            parent_bg=TITLEBAR_BG,
        )
        self.export_page_button.pack(side="right", padx=(0, 8), pady=16)
        self.clear_button = PillButton(
            bar,
            "清空",
            self.clear_photos,
            kind="ghost",
            font=self._fonts["button"],
            parent_bg=TITLEBAR_BG,
        )
        self.clear_button.pack(side="right", padx=(0, 8), pady=16)
        self.add_button = PillButton(
            bar,
            "添加照片",
            self.add_photos,
            kind="soft",
            font=self._fonts["button"],
            parent_bg=TITLEBAR_BG,
        )
        self.add_button.pack(side="right", padx=(0, 8), pady=16)

        for widget in (bar, left, logo, title_box, title, subtitle):
            widget.bind("<ButtonPress-1>", self._start_window_move)
            widget.bind("<B1-Motion>", self._move_window)
            widget.bind("<Double-Button-1>", lambda _e: self._toggle_maximize())

    # -- 窗口控制 -----------------------------------------------------------

    def _start_window_move(self, event: tk.Event) -> None:
        if self._maximized:
            return
        self._window_drag = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _move_window(self, event: tk.Event) -> None:
        if self._window_drag is None or self._maximized:
            return
        offset_x, offset_y = self._window_drag
        self.root.geometry(
            f"+{event.x_root - offset_x}+{event.y_root - offset_y}"
        )

    def _toggle_maximize(self) -> None:
        if self._maximized:
            self.root.geometry(self._restore_geometry)
        else:
            self._restore_geometry = self.root.geometry()
            self.root.geometry(
                f"{self.root.winfo_screenwidth()}x"
                f"{self.root.winfo_screenheight()}+0+0"
            )
        self._maximized = not self._maximized
        self.window_controls.set_maximized(self._maximized)

    def _minimize_window(self) -> None:
        # 主窗口隐藏后，在右下角留一个恢复胶囊（系统 iconify 在 WSLg
        # 上会断开 X 连接，所以不使用）。
        bubble = self._restore_bubble
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = self.root.winfo_x() + self.root.winfo_width() - bubble.WIDTH - 24
        y = self.root.winfo_y() + self.root.winfo_height() - bubble.HEIGHT - 24
        x = max(8, min(x, screen_w - bubble.WIDTH - 8))
        y = max(8, min(y, screen_h - bubble.HEIGHT - 8))
        self.root.withdraw()
        bubble.show(x, y)

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.after(60, self.root.focus_force)

    def _close_window(self) -> None:
        self.root.destroy()

    def _build_sidebar(self, parent: tk.Widget) -> tk.Frame:
        panel, sidebar = self._make_panel(
            parent, "photos", "照\n片", default_width=300, min_width=180
        )

        top = tk.Frame(sidebar, bg=PANEL_BG)
        top.pack(fill="x", padx=14, pady=(14, 8))
        self.count_label = tk.Label(
            top,
            text="单张照片",
            font=self._fonts["section"],
            bg=PANEL_BG,
            fg=TEXT,
        )
        self.count_label.pack(side="left")
        self._collapse_button(top, "photos").pack(side="right")
        self.photo_count_label = tk.Label(
            top,
            text="0 张",
            bg=ACCENT_SOFT,
            fg=ACCENT_PRESS,
            font=(self.font_family, 8),
            padx=8,
            pady=2,
        )
        self.photo_count_label.pack(side="right", padx=(0, 4))

        self.list_holder = tk.Frame(sidebar, bg=PANEL_BG)
        self.list_holder.pack(fill="both", expand=True, padx=(10, 6))
        list_holder = self.list_holder

        self.photo_canvas = tk.Canvas(
            list_holder, bg=PANEL_BG, highlightthickness=0, bd=0
        )
        scrollbar = ttk.Scrollbar(
            list_holder,
            orient="vertical",
            command=self.photo_canvas.yview,
            style="Soft.Vertical.TScrollbar",
        )
        self.photo_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.photo_canvas.pack(side="left", fill="both", expand=True)

        self.photo_list = tk.Frame(self.photo_canvas, bg=PANEL_BG)
        self._photo_window = self.photo_canvas.create_window(
            (0, 0), window=self.photo_list, anchor="nw"
        )
        self.photo_list.bind("<Configure>", self._on_photo_list_configure)
        self.photo_canvas.bind("<Configure>", self._on_photo_canvas_configure)
        self.photo_canvas.bind("<Enter>", self._bind_mousewheel)
        self.photo_canvas.bind("<Leave>", self._unbind_mousewheel)

        self.empty_list_label = tk.Label(
            self.photo_list,
            text="还没有照片\n点击「添加照片」选择多张图片",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 10),
            justify="left",
            anchor="w",
        )
        self.empty_list_label.pack(fill="x", padx=6, pady=18)

        # 导入 / 导出进度条（默认隐藏，开始时插到照片列表上方）。
        self.progress_holder = tk.Frame(sidebar, bg=PANEL_BG)
        progress_row = tk.Frame(self.progress_holder, bg=PANEL_BG)
        progress_row.pack(fill="x", pady=(0, 5))
        self.progress_label = tk.Label(
            progress_row,
            text="",
            bg=PANEL_BG,
            fg=TEXT,
            font=(self.font_family, 9),
            anchor="w",
        )
        self.progress_label.pack(side="left", fill="x", expand=True)
        self.progress_cancel_label = tk.Label(
            progress_row,
            text="取消",
            bg=PANEL_BG,
            fg=ACCENT,
            font=(self.font_family, 9),
            cursor="hand2",
        )
        self.progress_cancel_label.pack(side="right")
        self.progress_cancel_label.bind("<Button-1>", self._cancel_import)
        self.progress_bar = ProgressBar(self.progress_holder, height=10)
        self.progress_bar.pack(fill="x")

        controls = tk.Frame(sidebar, bg=PANEL_BG)
        controls.pack(fill="x", padx=14, pady=(10, 14))
        tk.Frame(controls, bg=BORDER, height=1).pack(fill="x", pady=(0, 12))

        self.selected_label = tk.Label(
            controls,
            text="选中照片缩放",
            bg=PANEL_BG,
            fg=TEXT,
            font=(self.font_family, 10, "bold"),
            anchor="w",
            justify="left",
            wraplength=230,
        )
        self.selected_label.pack(fill="x")

        zoom_row = tk.Frame(controls, bg=PANEL_BG)
        zoom_row.pack(fill="x", pady=(4, 0))
        self.zoom_scale = SoftSlider(
            zoom_row,
            self.zoom_value,
            100,
            int(MAX_ZOOM * 100),
            self._on_zoom_slider,
            width=140,
            height=24,
        )
        self.zoom_scale.pack(side="left", fill="x", expand=True)
        self.zoom_text = tk.Label(
            zoom_row,
            text="100%",
            bg=PANEL_BG,
            fg=MUTED,
            width=6,
            anchor="e",
            font=(self.font_family, 9),
        )
        self.zoom_text.pack(side="right", padx=(8, 0))

        tk.Label(
            controls,
            text=(
                "点这张照片看单张预览；拖到中间自动拼接"
                "或右侧自定义拼接的格子里即可放入。\n"
                "预览区：滚轮缩放、按住拖动取景。"
            ),
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 8),
            anchor="w",
            justify="left",
            wraplength=230,
        ).pack(fill="x", pady=(6, 10))

        compose_row = tk.Frame(controls, bg=PANEL_BG)
        compose_row.pack(fill="x", pady=(0, 6))
        self.put_button = ttk.Button(
            compose_row,
            text="放入选中格",
            style="Small.TButton",
            width=7,
            command=self.put_selected_in_slot,
        )
        self.put_button.pack(side="left")
        self.move_out_button = ttk.Button(
            compose_row,
            text="移出本页",
            style="Small.TButton",
            width=7,
            command=self.move_selected_out,
        )
        self.move_out_button.pack(side="left", padx=(6, 0))

        edit_row = tk.Frame(controls, bg=PANEL_BG)
        edit_row.pack(fill="x")
        self.rotate_button = ttk.Button(
            edit_row,
            text="↻ 旋转 90°",
            style="Small.TButton",
            width=7,
            command=self.rotate_selected,
        )
        self.rotate_button.pack(side="left")
        self.reset_view_button = ttk.Button(
            edit_row,
            text="复位取景",
            style="Small.TButton",
            width=7,
            command=self.reset_selected_view,
        )
        self.reset_view_button.pack(side="left", padx=(6, 0))
        remove_row = tk.Frame(controls, bg=PANEL_BG)
        remove_row.pack(fill="x", pady=(6, 0))
        self.remove_button = ttk.Button(
            remove_row,
            text="移除照片",
            style="Small.TButton",
            width=7,
            command=self.remove_selected,
        )
        self.remove_button.pack(side="left")
        return panel

    def _bind_scroll_wheel(self, widget: tk.Widget, canvas: tk.Canvas) -> None:
        """让鼠标在列表里的任意子控件上滚动时都能滚动对应列表。"""
        def handler(event: tk.Event) -> str:
            num = getattr(event, "num", None)
            if num == 4:
                delta = -1
            elif num == 5:
                delta = 1
            else:
                delta = -1 if getattr(event, "delta", 0) > 0 else 1
            canvas.yview_scroll(delta, "units")
            return "break"

        widget.bind("<MouseWheel>", handler)
        widget.bind("<Button-4>", handler)
        widget.bind("<Button-5>", handler)

    def _make_page_list(self, parent: tk.Widget) -> tuple[tk.Canvas, tk.Frame]:
        holder = tk.Frame(parent, bg=PANEL_BG)
        holder.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 8))
        canvas = tk.Canvas(holder, bg=PANEL_BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(
            holder,
            orient="vertical",
            command=canvas.yview,
            style="Soft.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=PANEL_BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )
        self._bind_scroll_wheel(canvas, canvas)
        return canvas, inner

    def _build_auto_panel(self, parent: tk.Widget) -> tk.Frame:
        panel, content = self._make_panel(
            parent, "auto", "拼\n图", default_width=225, min_width=190
        )
        top = tk.Frame(content, bg=PANEL_BG)
        top.pack(fill="x", padx=14, pady=(14, 4))
        tk.Label(
            top,
            text="自动拼接",
            font=self._fonts["section"],
            bg=PANEL_BG,
            fg=TEXT,
        ).pack(side="left")
        self._collapse_button(top, "auto").pack(side="right")
        self.auto_pages_count_label = tk.Label(
            top,
            text="0 页",
            bg=ACCENT_SOFT,
            fg=ACCENT_PRESS,
            font=(self.font_family, 8),
            padx=7,
            pady=2,
        )
        self.auto_pages_count_label.pack(side="right", padx=(0, 4))
        tk.Label(
            content,
            text="工具随机生成的拼接页\n点击缩略图预览",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 8),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 6))
        self.auto_canvas, self.auto_list = self._make_page_list(content)
        return panel

    def _build_custom_panel(self, parent: tk.Widget) -> tk.Frame:
        panel, content = self._make_panel(
            parent, "custom", "自\n定\n义", default_width=235, min_width=190
        )
        top = tk.Frame(content, bg=PANEL_BG)
        top.pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(
            top,
            text="自定义拼接",
            font=self._fonts["section"],
            bg=PANEL_BG,
            fg=TEXT,
        ).pack(side="left")
        self._collapse_button(top, "custom").pack(side="right")
        self.custom_pages_count_label = tk.Label(
            top,
            text="0 页",
            bg=ACCENT_SOFT,
            fg=ACCENT_PRESS,
            font=(self.font_family, 8),
            padx=7,
            pady=2,
        )
        self.custom_pages_count_label.pack(side="right", padx=(0, 4))

        add_row = tk.Frame(content, bg=PANEL_BG)
        add_row.pack(fill="x", padx=14, pady=(4, 4))
        self.add_custom2_button = ttk.Button(
            add_row,
            text="＋ 2 张",
            style="Tiny.TButton",
            width=6,
            command=lambda: self.add_custom_page(2),
        )
        self.add_custom2_button.pack(side="left")
        self.add_custom4_button = ttk.Button(
            add_row,
            text="＋ 4 张",
            style="Tiny.TButton",
            width=6,
            command=lambda: self.add_custom_page(4),
        )
        self.add_custom4_button.pack(side="left", padx=(6, 0))
        tk.Label(
            content,
            text="先建一张空白拼接图，再把左侧照片\n拖进格子里，或直接拖入图片文件。",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 8),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 6))
        self.custom_canvas, self.custom_list = self._make_page_list(content)
        return panel

    def _build_workspace(self, parent: tk.Widget) -> tk.Frame:
        workspace = tk.Frame(parent, bg=BG)

        preview_card = tk.Frame(
            workspace,
            bg=PANEL_BG,
            highlightthickness=1,
            highlightbackground="#e7dcec",
        )
        preview_card.pack(fill="both", expand=True)

        preview_header = tk.Frame(preview_card, bg=PANEL_BG)
        preview_header.pack(fill="x", padx=18, pady=(12, 10))
        self.preview_title_label = tk.Label(
            preview_header,
            text="自动拼接预览",
            font=self._fonts["section"],
            bg=PANEL_BG,
            fg=TEXT,
        )
        self.preview_title_label.pack(side="left")
        self.page_label = tk.Label(
            preview_header,
            text="",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 9),
        )
        self.page_label.pack(side="left", padx=(10, 0))
        self.next_button = ttk.Button(
            preview_header,
            text="›",
            style="Tiny.TButton",
            width=2,
            command=lambda: self.change_page(1),
        )
        self.next_button.pack(side="right")
        self.page_number_label = tk.Label(
            preview_header,
            text="",
            bg=CARD_BG,
            fg=TEXT,
            font=(self.font_family, 8),
            padx=7,
            pady=2,
        )
        self.page_number_label.pack(side="right", padx=8)
        self.prev_button = ttk.Button(
            preview_header,
            text="‹",
            style="Tiny.TButton",
            width=2,
            command=lambda: self.change_page(-1),
        )
        self.prev_button.pack(side="right")

        tools_row = tk.Frame(preview_card, bg=PANEL_BG)
        tools_row.pack(fill="x", padx=18, pady=(0, 6))
        self.new_page_button = ttk.Button(
            tools_row,
            text="＋ 新页",
            style="Tiny.TButton",
            width=7,
            command=self.add_page,
        )
        self.new_page_button.pack(side="left")
        self.delete_page_button = ttk.Button(
            tools_row,
            text="删除本页",
            style="Tiny.TButton",
            width=7,
            command=self.delete_page,
        )
        self.delete_page_button.pack(side="right")

        self.preview_canvas = tk.Canvas(
            preview_card,
            bg=PREVIEW_BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.preview_canvas.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.preview_canvas.bind("<Configure>", self._on_preview_resize)
        self.preview_canvas.bind("<ButtonPress-1>", self._on_preview_press)
        self.preview_canvas.bind("<B1-Motion>", self._on_preview_drag)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_preview_release)
        self.preview_canvas.bind("<MouseWheel>", self._on_preview_wheel)
        self.preview_canvas.bind("<Button-4>", self._on_preview_wheel)
        self.preview_canvas.bind("<Button-5>", self._on_preview_wheel)

        self._build_settings_bar(preview_card)

        self.status_label = tk.Label(
            workspace,
            text="点击单张照片预览 · 拖动照片到拼接格 · 滚轮缩放 · 按住拖动取景",
            bg=BG,
            fg=MUTED,
            font=(self.font_family, 9),
            anchor="w",
        )
        self.status_label.pack(fill="x", pady=(8, 0))
        return workspace

    def _build_settings_bar(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=PANEL_BG)
        bar.pack(fill="x", padx=14, pady=(10, 14))
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", pady=(0, 10))

        row = tk.Frame(bar, bg=PANEL_BG)
        row.pack(fill="x", padx=4)

        mode_group = tk.Frame(row, bg=PANEL_BG)
        mode_group.pack(side="left")
        tk.Label(
            mode_group,
            text="拼版方式",
            bg=PANEL_BG,
            fg=TEXT,
            font=(self.font_family, 10, "bold"),
        ).pack(side="left", padx=(0, 8))
        self.mode_control = SegmentedControl(
            mode_group,
            [
                ("2 张 · 左右", 2),
                ("4 张 · 田字格", 4),
                ("自动混排", 0),
            ],
            self.mode,
            self._on_mode_changed,
            font=self._fonts["segment"],
        )
        self.mode_control.pack(side="left")

        divider = tk.Frame(row, bg=BORDER, width=1)
        divider.pack(side="left", fill="y", padx=12)

        self.gap_text = self._build_spacing_control(
            row, "间距", self.gap_units, self._on_spacing_changed
        )
        self.margin_text = self._build_spacing_control(
            row, "边距", self.margin_units, self._on_spacing_changed
        )

        tk.Label(
            row,
            text="单位 1 ≈ 0.25 mm",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 8),
        ).pack(side="right", padx=(8, 4))

        extra_row = tk.Frame(bar, bg=PANEL_BG)
        extra_row.pack(fill="x", padx=4, pady=(8, 0))
        self.auto_rotate_toggle = TogglePill(
            extra_row,
            "自动旋转（横图转竖向）",
            self.auto_rotate,
            self._on_auto_rotate_changed,
            font=self._fonts["toggle"],
        )
        self.auto_rotate_toggle.pack(side="left")
        self.reroll_button = ttk.Button(
            extra_row,
            text="重新随机",
            style="Small.TButton",
            command=self._rerandomize,
        )
        self.reroll_button.pack(side="left", padx=(12, 0))
        tk.Label(
            extra_row,
            text="优先级：同场景/人物/风景 › 同建筑 › 同色系；4 张页约占 1/4 ～ 1/3",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 8),
        ).pack(side="right")

    def _build_spacing_control(
        self,
        parent: tk.Widget,
        title: str,
        variable: tk.IntVar,
        callback,
    ) -> tk.Label:
        group = tk.Frame(parent, bg=PANEL_BG)
        group.pack(side="left", padx=(0, 16))
        head = tk.Frame(group, bg=PANEL_BG)
        head.pack(fill="x")
        tk.Label(
            head,
            text=title,
            bg=PANEL_BG,
            fg=TEXT,
            font=(self.font_family, 9, "bold"),
        ).pack(side="left")
        value_label = tk.Label(
            head,
            text="",
            bg=PANEL_BG,
            fg=MUTED,
            font=(self.font_family, 8),
        )
        value_label.pack(side="left", padx=(6, 0))
        scale = SoftSlider(
            group,
            variable,
            0,
            MAX_SPACING,
            callback,
            width=118,
            height=22,
        )
        scale.pack(fill="x", pady=(3, 0))
        return value_label

    # -- 数据与状态 ---------------------------------------------------------

    def _clamp_page(self) -> None:
        if not self.pages:
            self.current_page = 0
            return
        self.current_page = max(0, min(self.current_page, len(self.pages) - 1))

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _selected_photo(self) -> Photo | None:
        if self.selected_index is None:
            return None
        if not (0 <= self.selected_index < len(self.photos)):
            return None
        return self.photos[self.selected_index]

    def _pool_index(self, photo: Photo) -> int | None:
        for index, item in enumerate(self.photos):
            if item is photo:
                return index
        return None

    def _location_of(self, photo: Photo) -> tuple[int, int] | None:
        """返回照片所在的 (页索引, 槽位索引)，未使用时返回 None。"""
        for page_index, page in enumerate(self.pages):
            for slot_index, item in enumerate(page.photos):
                if item is photo:
                    return page_index, slot_index
        return None

    def _assigned_photos(self) -> list[Photo]:
        return [
            photo
            for page in self.pages
            for photo in page.photos
            if photo is not None
        ]

    def _random_page_mode(self, remaining: int | None = None) -> int:
        layout = self.mode.get()
        if layout in (2, 4):
            return layout
        if remaining is not None and remaining <= 2:
            return 2
        return 4 if self._rng.random() < MIXED_FOUR_CHANCE else 2

    def _smart_ordered_photos(self, photos: list[Photo]) -> list[Photo]:
        """按分层优先级串起照片。

        同一场景优先级最高；其次是人物照和风景照；再次是同一建筑；
        最后才用色系做兜底。距离函数里已经把这些层级编码好，这里直接
        做最近邻排序即可。
        """
        return order_similar(photos, self._rng)

    def _category_summary(self, photos: list[Photo]) -> str:
        counts = {"人物": 0, "建筑": 0, "风景": 0}
        for photo in photos:
            counts[photo_kind(photo)] += 1
        return (
            f"人物 {counts['人物']} 张 · 建筑 {counts['建筑']} 张 · "
            f"风景 {counts['风景']} 张"
        )

    def _build_pages(
        self,
        photos: list[Photo],
        layout: int,
        pre_ordered: bool = False,
    ) -> list[Page]:
        if not photos:
            mode = self._random_page_mode()
            return [Page(mode, [None] * mode)]

        ordered = list(photos) if pre_ordered else self._smart_ordered_photos(photos)
        pages: list[Page] = []
        index = 0
        while index < len(ordered):
            if layout in (2, 4):
                mode = layout
            else:
                # 自动混排时尽量在同类照片内部翻页，避免一页里类别混杂。
                category = photo_kind(ordered[index])
                end = index
                while end < len(ordered) and photo_kind(ordered[end]) == category:
                    end += 1
                remaining = end - index
                if remaining >= 4:
                    mode = (
                        4
                        if self._rng.random() < MIXED_FOUR_CHANCE
                        else 2
                    )
                else:
                    mode = 2
            chunk = ordered[index : index + mode]
            pages.append(
                Page(mode, list(chunk) + [None] * (mode - len(chunk)))
            )
            index += mode
        return pages

    def _assign_new_photos(
        self, new_photos: list[Photo], ordered: list[Photo] | None = None
    ) -> None:
        """先填满已有空格，剩余照片按当前版式批量建页。

        ``ordered`` 已由后台线程排好序时直接使用，避免重复做费时的
        最近邻排序。
        """
        if ordered is not None:
            pending = list(ordered)
        else:
            pending = self._smart_ordered_photos(list(new_photos))
        for page in self.pages:
            for index, item in enumerate(page.photos):
                if item is None and pending:
                    page.photos[index] = pending.pop(0)
            if not pending:
                return
        if pending:
            self.pages.extend(
                self._build_pages(pending, self.mode.get(), pre_ordered=True)
            )

    def _reflow_pages(self) -> None:
        photos = self._assigned_photos()
        self.pages = self._build_pages(photos, self.mode.get())
        self.active_slot = None
        selected = self._selected_photo()
        if selected is not None:
            location = self._location_of(selected)
            if location is not None:
                self.current_page = location[0]
                self.active_slot = location
        self._clamp_page()

    def _first_empty_slot(self, page_index: int) -> int | None:
        if not (0 <= page_index < len(self.pages)):
            return None
        for index, photo in enumerate(self.pages[page_index].photos):
            if photo is None:
                return index
        return None

    # -- 文件选择 -----------------------------------------------------------

    def _refocus_after_dialog(self) -> None:
        """Windows 对话框关闭后，把焦点还给主窗口。"""
        if not running_in_wsl():
            return
        self.root.lift()
        self.root.after(60, self.root.focus_force)

    def _pick_photo_paths(self) -> tuple[str, ...]:
        if running_in_wsl():
            self._set_status("正在打开文件选择器…")
            self.root.update_idletasks()
            picked = _windows_pick_files("选择要拼接的照片", IMAGE_FILETYPES)
            if picked is not None:
                self._refocus_after_dialog()
                if not picked:
                    self._set_status("已取消添加照片。")
                return tuple(picked)
        return tuple(
            filedialog.askopenfilenames(
                parent=self.root,
                title="选择要拼接的照片",
                filetypes=IMAGE_FILETYPES,
            )
        )

    def _pick_save_path(self, default_name: str) -> str:
        if running_in_wsl():
            self._set_status("正在打开保存对话框…")
            self.root.update_idletasks()
            picked = _windows_save_file(
                "导出拼接照片", default_name, SAVE_FILETYPES, "jpg"
            )
            if picked is not None:
                self._refocus_after_dialog()
                if not picked:
                    self._set_status("已取消导出。")
                return picked
        return filedialog.asksaveasfilename(
            parent=self.root,
            title="导出拼接照片",
            defaultextension=".jpg",
            initialfile=default_name,
            filetypes=SAVE_FILETYPES,
        )

    def _pick_export_folder(self) -> str:
        if running_in_wsl():
            self._set_status("正在打开文件夹选择器…")
            self.root.update_idletasks()
            picked = _windows_pick_folder("选择导出文件夹")
            if picked is not None:
                self._refocus_after_dialog()
                if not picked:
                    self._set_status("已取消导出。")
                return picked
        return filedialog.askdirectory(
            parent=self.root, title="选择导出文件夹"
        )

    # -- 照片列表 -----------------------------------------------------------

    def add_photos(self) -> None:
        if self._importing:
            self._set_status("正在导入照片，请稍候…")
            return
        if self._exporting:
            self._set_status("正在导出照片，请稍候…")
            return
        paths = self._pick_photo_paths()
        if not paths:
            return
        self._start_import([str(path) for path in paths])

    # -- 后台导入 -----------------------------------------------------------

    def _show_progress(self, text: str, cancellable: bool = False) -> None:
        self.progress_label.configure(text=text)
        self.progress_bar.set_fraction(0.0)
        if cancellable:
            self.progress_cancel_label.configure(
                text="取消", fg=ACCENT, cursor="hand2"
            )
        else:
            self.progress_cancel_label.configure(text="", cursor="")
        self.progress_holder.pack(
            fill="x",
            padx=16,
            pady=(2, 8),
            before=self.list_holder,
        )

    def _update_progress(
        self, done: int, total: int, text: str | None = None
    ) -> None:
        if text is not None:
            self.progress_label.configure(text=text)
        self.progress_bar.set_fraction(done / total if total else 0.0)

    def _hide_progress(self) -> None:
        self.progress_holder.pack_forget()

    def _cancel_import(self, _event: tk.Event | None = None) -> None:
        if not self._importing or self._import_cancel is None:
            return
        self._import_cancel.set()
        self.progress_cancel_label.configure(
            text="正在取消…", fg=MUTED, cursor=""
        )
        self._set_status("正在取消导入，已经读取的照片会保留…")

    def _start_import(
        self,
        paths: list[str],
        drop_target: tuple[int, int] | None = None,
    ) -> None:
        if self._importing or self._exporting:
            self._set_status("当前有任务正在进行，请稍候…")
            return
        total = len(paths)
        self._importing = True
        self._import_total = total
        self._import_done = 0
        self._import_added = []
        self._import_errors = []
        self._import_ordered = None
        self._import_cancel = threading.Event()
        self._import_queue = queue.Queue()
        self._import_drop_active = drop_target is not None
        if drop_target is not None:
            page_index, slot_index = drop_target
            if not self.custom_pages:
                fallback_mode = 4 if slot_index >= 2 else 2
                self.custom_pages.append(
                    Page(fallback_mode, [None] * fallback_mode)
                )
            page_index = max(0, min(page_index, len(self.custom_pages) - 1))
            page = self.custom_pages[page_index]
            self._import_drop_page = page_index
            self._import_drop_slot = max(0, min(slot_index, page.mode - 1))
            self._import_drop_mode = page.mode
            self.preview_kind = "custom"
            self.current_custom_page = page_index
            self.active_custom_slot = self._import_drop_slot
            self.preview_photo_index = None
        else:
            self._import_drop_page = None
            self._import_drop_slot = 0
            self._import_drop_mode = 2

        self.add_button.set_enabled(False)
        self._update_export_buttons()
        self._show_progress(f"正在导入 0 / {total} 张", cancellable=True)
        self._set_status(
            f"正在后台导入 {total} 张照片，导入过程中界面仍可正常使用…"
        )
        if self.empty_list_label is not None:
            self.empty_list_label.configure(
                text="正在导入照片…\n进度见上方进度条"
            )
        self._import_thread = threading.Thread(
            target=self._import_worker,
            args=(list(paths), self._import_queue, self._import_cancel),
            name="photo-import",
            daemon=True,
        )
        self._import_thread.start()
        self._import_poll_job = self.root.after(50, self._poll_import)

    @staticmethod
    def _import_worker(
        paths: list[str], out: queue.Queue, cancel: threading.Event
    ) -> None:
        """后台线程：并行解码预览图，完成后按场景排序。"""
        loaded: list[Photo] = []
        pool = ThreadPoolExecutor(max_workers=IMPORT_WORKERS)
        try:
            futures = {pool.submit(load_photo, path): path for path in paths}
            for future in as_completed(futures):
                if cancel.is_set():
                    break
                path = futures[future]
                try:
                    photo = future.result()
                except Exception as exc:  # noqa: BLE001 - 单张失败不影响其他
                    out.put(("error", Path(path).name, str(exc)))
                    continue
                loaded.append(photo)
                out.put(("photo", photo))
            if loaded and not cancel.is_set():
                ordered = order_similar(list(loaded), random.Random())
                out.put(("ordered", ordered))
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默退出
            out.put(("fatal", "", str(exc)))
        finally:
            # 取消时不要等所有排队图片解码完；未开始的任务直接丢弃。
            pool.shutdown(wait=False, cancel_futures=True)
            out.put(("done",))

    def _poll_import(self) -> None:
        self._import_poll_job = None
        pending = self._import_queue
        if pending is None:
            return
        done = False
        processed = 0
        while processed < 12:
            try:
                item = pending.get_nowait()
            except queue.Empty:
                break
            processed += 1
            kind = item[0]
            if kind == "photo":
                photo = item[1]
                self._import_done += 1
                self._import_added.append(photo)
                self.photos.append(photo)
                if self._import_drop_active:
                    self._place_imported_photo(photo)
            elif kind == "error":
                self._import_done += 1
                self._import_errors.append(f"{item[1]}: {item[2]}")
            elif kind == "ordered":
                self._import_ordered = item[1]
            elif kind == "fatal":
                self._import_errors.append(item[2])
            elif kind == "done":
                done = True
                break

        cancelled = (
            self._import_cancel is not None and self._import_cancel.is_set()
        )
        if not done:
            if (
                not cancelled
                and self._import_ordered is None
                and self._import_done >= self._import_total
            ):
                self._update_progress(
                    self._import_done,
                    self._import_total,
                    f"正在按场景智能分组（{self._import_done} 张）…",
                )
            else:
                self._update_progress(
                    self._import_done,
                    self._import_total,
                    f"正在导入 {self._import_done} / {self._import_total} 张",
                )
            self.photo_count_label.configure(
                text=f"{len(self.photos)} 张 · 导入中"
            )
            self._import_poll_job = self.root.after(40, self._poll_import)
            return

        self._finish_import()

    def _place_imported_photo(self, photo: Photo) -> None:
        """把拖入 / 导入的照片依次放进自定义拼接图的空格。"""
        page_index = self._import_drop_page
        if page_index is None:
            return
        mode = self._import_drop_mode
        while page_index >= len(self.custom_pages):
            self.custom_pages.append(Page(mode, [None] * mode))
        page = self.custom_pages[page_index]
        slot = max(0, min(self._import_drop_slot, page.mode - 1))
        page.photos[slot] = photo.clone_for_export()
        self._import_drop_last_page = page_index
        self._import_drop_last_slot = slot
        self.preview_kind = "custom"
        self.current_custom_page = page_index
        self.active_custom_slot = slot
        self.preview_photo_index = None
        self._import_drop_slot = slot + 1
        if self._import_drop_slot >= page.mode:
            self._import_drop_page = page_index + 1
            self._import_drop_slot = 0
            self._import_drop_mode = page.mode
        self.schedule_preview()

    def _finish_import(self) -> None:
        self._importing = False
        self._hide_progress()
        self.add_button.set_enabled(True)
        self._update_export_buttons()
        added = list(self._import_added)
        if added:
            # 排序已在后台线程完成；取消导入时按到达顺序排列，避免界面再卡住。
            ordered = self._import_ordered or list(added)
            drop_import = self._import_drop_active
            self._assign_new_photos(added, ordered=ordered)
            self.preview_photo_index = None
            first = added[0]
            self.selected_index = self._pool_index(first)
            if drop_import and self.custom_pages:
                self._clamp_custom_page()
                self.current_custom_page = min(
                    self._import_drop_last_page,
                    len(self.custom_pages) - 1,
                )
                self.active_custom_slot = self._import_drop_last_slot
                self.preview_kind = "custom"
                status_text = (
                    f"已把 {len(added)} 张照片放入自定义拼接图。"
                )
            else:
                self.preview_kind = "auto"
                self.active_custom_slot = None
                location = self._location_of(first)
                if location is not None:
                    self.current_page = location[0]
                    self.active_slot = location
                status_text = (
                    f"已添加 {len(added)} 张照片"
                    f"（{self._category_summary(added)}），"
                    "同场景/人物/风景优先相邻，其次是同建筑、同色系。"
                )
            self._clamp_page()
            self._import_drop_active = False
            self._import_drop_page = None
            self._pending_list_status = status_text
            self._refresh_all()
            if (
                self._pending_list_status is not None
                and not self._list_building
            ):
                self._set_status(self._pending_list_status)
                self._pending_list_status = None
        elif self.photos:
            self._refresh_controls()
        else:
            self._update_export_buttons()
            if self.empty_list_label is not None:
                self.empty_list_label.configure(
                    text="还没有照片\n点击「添加照片」选择多张图片"
                )

        if not added:
            self._import_drop_active = False
            self._import_drop_page = None
        errors = self._import_errors
        if errors:
            preview = "\n".join(errors[:8])
            if len(errors) > 8:
                preview += f"\n…另有 {len(errors) - 8} 张无法读取"
            messagebox.showwarning("部分照片无法读取", preview, parent=self.root)
        elif not added:
            cancelled_now = (
                self._import_cancel is not None and self._import_cancel.is_set()
            )
            self._set_status(
                "已取消导入。" if cancelled_now else "没有可导入的照片。"
            )

    def clear_photos(self) -> None:
        if self._importing or self._exporting:
            self._set_status("当前有任务正在进行，请稍候再清空。")
            return
        if not self.photos:
            return
        prompt = f"确定移除当前 {len(self.photos)} 张照片吗？"
        if self.custom_pages:
            prompt += "\n自定义拼接栏里的拼接图也会一并清空。"
        if not messagebox.askyesno("清空照片", prompt, parent=self.root):
            return
        self.photos.clear()
        layout = self.mode.get()
        mode = layout if layout in (2, 4) else 2
        self.pages = [Page(mode, [None] * mode)]
        self.custom_pages.clear()
        self.selected_index = None
        self.active_slot = None
        self.active_custom_slot = None
        self.current_page = 0
        self.current_custom_page = 0
        self.preview_kind = "auto"
        self.preview_photo_index = None
        self._thumb_cache.clear()
        self._page_thumb_cache.clear()
        self._refresh_all()
        self._set_status("照片已清空。")

    def remove_selected(self) -> None:
        if self.preview_kind == "custom":
            page = self._active_page()
            slot = self.active_custom_slot
            if page is not None and slot is not None and 0 <= slot < len(
                page.photos
            ):
                if page.photos[slot] is not None:
                    page.photos[slot] = None
                    self.active_custom_slot = None
                    self._refresh_all()
                    self._set_status("已从自定义拼接图移除这张照片。")
                    return
        photo = self._selected_photo()
        if photo is None:
            return
        index = self._pool_index(photo)
        if index is None:
            return
        location = self._location_of(photo)
        del self.photos[index]
        if location is not None:
            self.pages[location[0]].photos[location[1]] = None

        if not self.photos:
            self.selected_index = None
            self.preview_kind = "auto"
            self.preview_photo_index = None
        else:
            self.selected_index = min(index, len(self.photos) - 1)
            if self.preview_kind == "photo":
                self.preview_photo_index = self.selected_index
        self.active_slot = location
        self._clamp_page()
        self._refresh_all()
        self._set_status(f"已移除 {photo.name}。")

    def select_photo(self, index: int) -> None:
        if index < 0 or index >= len(self.photos):
            return
        self.selected_index = index
        self.preview_kind = "photo"
        self.preview_photo_index = index
        self.active_slot = None
        self.active_custom_slot = None
        self._refresh_all()

    def _pool_index_by_path(self, photo: Photo) -> int | None:
        for index, item in enumerate(self.photos):
            if item.path == photo.path:
                return index
        return None

    def _put_photo_in_slot(self, index: int) -> None:
        if self._importing or self._exporting or self._list_building:
            return
        if index < 0 or index >= len(self.photos):
            return
        self.selected_index = index
        self.put_selected_in_slot()

    # -- 照片与页面的组合 ---------------------------------------------------

    def _assign_photo_to_slot(
        self, photo: Photo, page_index: int, slot_index: int
    ) -> None:
        if self._importing or self._exporting or self._list_building:
            return
        if not self.pages:
            return
        page_index = max(0, min(page_index, len(self.pages) - 1))
        page = self.pages[page_index]
        slot_index = max(0, min(slot_index, page.mode - 1))
        if page.photos[slot_index] is photo:
            index = self._pool_index(photo)
            if index is not None:
                self.selected_index = index
            self.current_page = page_index
            self.active_slot = (page_index, slot_index)
            self.preview_kind = "auto"
            self.preview_photo_index = None
            self.active_custom_slot = None
            self._refresh_all()
            return

        source = self._location_of(photo)
        displaced = page.photos[slot_index]
        if source is not None:
            self.pages[source[0]].photos[source[1]] = None
        page.photos[slot_index] = photo
        if displaced is not None and source is not None:
            self.pages[source[0]].photos[source[1]] = displaced

        self.selected_index = self._pool_index(photo)
        self.current_page = page_index
        self.active_slot = (page_index, slot_index)
        self.preview_kind = "auto"
        self.preview_photo_index = None
        self.active_custom_slot = None
        self._refresh_all()

        slot_name = slot_label(slot_index, page.mode)
        if displaced is not None and source is None:
            self._set_status(
                f"已放入 {photo.name}；{displaced.name} 移到未使用区。"
            )
        else:
            self._set_status(
                f"已把 {photo.name} 放入第 {page_index + 1} 页 · {slot_name}。"
            )

    def _assign_photo_to_custom_slot(
        self, photo: Photo, page_index: int, slot_index: int
    ) -> None:
        """把单张照片复制一份放进自定义拼接页，不改变自动拼接页里的取景。"""
        if self._importing or self._exporting or self._list_building:
            return
        if not self.custom_pages:
            return
        page_index = max(0, min(page_index, len(self.custom_pages) - 1))
        page = self.custom_pages[page_index]
        slot_index = max(0, min(slot_index, page.mode - 1))
        displaced = page.photos[slot_index]
        page.photos[slot_index] = photo.clone_for_export()
        self.preview_kind = "custom"
        self.preview_photo_index = None
        self.current_custom_page = page_index
        self.active_custom_slot = slot_index
        self.active_slot = None
        self.selected_index = self._pool_index(photo)
        self._refresh_all()
        slot_name = slot_label(slot_index, page.mode)
        if displaced is not None:
            self._set_status(
                f"已用 {photo.name} 替换自定义拼接图 "
                f"{page_index + 1} 的{slot_name}。"
            )
        else:
            self._set_status(
                f"已把 {photo.name} 放入自定义拼接图 "
                f"{page_index + 1} · {slot_name}。"
            )

    def _first_empty_custom_slot(self, page_index: int) -> int | None:
        if not (0 <= page_index < len(self.custom_pages)):
            return None
        for index, photo in enumerate(self.custom_pages[page_index].photos):
            if photo is None:
                return index
        return None

    def put_selected_in_slot(self) -> None:
        photo = self._selected_photo()
        if photo is None:
            self._set_status("先选中一张照片。")
            return
        if self.preview_kind == "custom":
            if not self.custom_pages:
                self._set_status("先在右侧新建一张空白拼接图。")
                return
            self._clamp_custom_page()
            page_index = self.current_custom_page
            slot = self.active_custom_slot
            if slot is None:
                slot = self._first_empty_custom_slot(page_index)
                if slot is None:
                    self._set_status("这张自定义拼接图已经放满了。")
                    return
            self._assign_photo_to_custom_slot(photo, page_index, slot)
            return
        slot = self.active_slot
        if slot is None or slot[0] != self.current_page:
            empty = self._first_empty_slot(self.current_page)
            if empty is None:
                self._set_status("当前页没有空格，先点「＋ 新页」或拖到其他格子。")
                return
            slot = (self.current_page, empty)
        self._assign_photo_to_slot(photo, slot[0], slot[1])

    def move_selected_out(self) -> None:
        if self.preview_kind == "custom":
            page = self._active_page()
            slot = self.active_custom_slot
            if page is not None and slot is not None and 0 <= slot < len(
                page.photos
            ):
                page.photos[slot] = None
                self._refresh_all()
                self._set_status("已把这张照片移出自定义拼接图。")
            return
        photo = self._selected_photo()
        if photo is None:
            self._set_status("先选中一张照片。")
            return
        location = self._location_of(photo)
        if location is None:
            self._set_status("选中的照片本来就不在拼接页上。")
            return
        self.pages[location[0]].photos[location[1]] = None
        self.active_slot = location
        self._refresh_all()
        self._set_status(
            f"{photo.name} 已移出拼接页，仍保留在左侧列表（未使用）。"
        )

    def rotate_selected(self) -> None:
        photo = self._control_photo()
        if photo is None:
            self._set_status("先选中一张照片。")
            return
        photo.rotate(90)
        # 旋转后同步交换裁剪方向，尽量保持原来的取景位置。
        photo.offset_x, photo.offset_y = photo.offset_y, -photo.offset_x
        self._refresh_all()
        self._set_status(f"{photo.name} 已手动旋转 90°。")

    def reset_selected_view(self) -> None:
        photo = self._control_photo()
        if photo is None:
            self._set_status("先选中一张照片。")
            return
        photo.zoom = 1.0
        photo.offset_x = 0.0
        photo.offset_y = 0.0
        self._refresh_all()
        self._set_status(f"{photo.name} 的缩放和取景已复位。")

    # -- 分页 ---------------------------------------------------------------

    def change_page(self, delta: int) -> None:
        if self.preview_kind == "photo":
            index = self.preview_photo_index
            if index is None:
                index = self.selected_index
            if index is None:
                return
            target = index + delta
            if 0 <= target < len(self.photos):
                self.select_photo(target)
            return
        if self.preview_kind == "custom":
            new_page = self.current_custom_page + delta
            if 0 <= new_page < len(self.custom_pages):
                self.current_custom_page = new_page
                self.active_custom_slot = None
                self.selected_index = None
                self._refresh_all()
            return
        if not self.pages:
            return
        new_page = self.current_page + delta
        if 0 <= new_page < len(self.pages):
            self.current_page = new_page
            page = self.pages[new_page]
            first = next(
                (index for index, photo in enumerate(page.photos) if photo is not None),
                None,
            )
            if first is not None:
                self.active_slot = (new_page, first)
                self.selected_index = self._pool_index(page.photos[first])
            else:
                self.active_slot = (new_page, 0)
            self._refresh_all()

    def add_page(self) -> None:
        mode = self._random_page_mode()
        self.pages.append(Page(mode, [None] * mode))
        self.preview_kind = "auto"
        self.preview_photo_index = None
        self.current_page = len(self.pages) - 1
        self.active_slot = (self.current_page, 0)
        self.active_custom_slot = None
        self._refresh_all()
        self._set_status(
            f"已新建第 {self.current_page + 1} 页（{mode} 张版式）。"
        )

    def delete_page(self) -> None:
        if self.preview_kind == "custom":
            self.remove_custom_page(self.current_custom_page)
            return
        if self.preview_kind == "photo":
            return
        if len(self.pages) <= 1:
            self._set_status("至少保留一页。")
            return
        page = self.pages[self.current_page]
        used = page.used_count()
        if used and not messagebox.askyesno(
            "删除本页",
            f"本页有 {used} 张照片。删除后这些照片会变成“未使用”，"
            "仍保留在左侧列表。\n确定删除这一页吗？",
            parent=self.root,
        ):
            return
        del self.pages[self.current_page]
        self.current_page = min(self.current_page, len(self.pages) - 1)
        self.active_slot = None
        self._refresh_all()
        self._set_status("已删除当前页。")

    # -- 刷新界面 -----------------------------------------------------------

    def _refresh_all(self) -> None:
        self._refresh_photo_list()
        self._refresh_controls()
        self.schedule_page_lists()
        self.schedule_preview()

    def _thumbnail_angle(self, photo: Photo) -> int:
        location = self._location_of(photo)
        if location is not None:
            portrait_cell = self.pages[location[0]].mode == 2
        else:
            layout = self.mode.get()
            if layout == 2:
                portrait_cell = True
            elif layout == 4:
                portrait_cell = False
            else:
                return photo.rotation % 360
        cell_w, cell_h = (300, 400) if portrait_cell else (400, 300)
        return photo.rotation_for_cell(cell_w, cell_h, self.auto_rotate.get())

    def _thumbnail_image(
        self, photo: Photo, angle: int
    ) -> ImageTk.PhotoImage:
        cache_key = (str(photo.path), angle)
        thumb_image = self._thumb_cache.get(cache_key)
        if thumb_image is None:
            thumb = make_list_thumbnail(
                photo.oriented_image(angle, cache=False)
            )
            thumb_image = ImageTk.PhotoImage(_rounded_image(thumb, 9))
            self._thumb_cache[cache_key] = thumb_image
        return thumb_image

    def _refresh_photo_list(self) -> None:
        # 新的刷新会让还没画完的旧批次失效。
        self._list_revision += 1
        revision = self._list_revision
        if self._card_job is not None:
            try:
                self.root.after_cancel(self._card_job)
            except tk.TclError:
                pass
            self._card_job = None
        self._list_building = False
        self._list_queue = []

        for child in self.photo_list.winfo_children():
            child.destroy()
        self._thumb_refs.clear()
        self.empty_list_label = None

        n = len(self.photos)
        used = len(self._assigned_photos())
        self.photo_count_label.configure(text=f"{n} 张 · 已用 {used}")
        self.count_label.configure(text="照片列表")
        self._update_export_buttons()
        if not n:
            self.empty_list_label = tk.Label(
                self.photo_list,
                text="还没有照片\n点击「添加照片」选择多张图片",
                bg=PANEL_BG,
                fg=MUTED,
                font=(self.font_family, 10),
                justify="left",
                anchor="w",
            )
            self.empty_list_label.pack(fill="x", padx=6, pady=18)
            self._pending_list_status = None
            return

        if n > 80:
            # 照片很多时分批生成卡片，避免界面长时间卡住。
            self._list_building = True
            self._list_queue = list(range(n))
            self._set_status(f"正在显示照片列表… 0 / {n}")
            self._build_cards_batch(revision, n)
            return

        for i, photo in enumerate(self.photos):
            self._make_photo_card(
                i, photo, i == self.selected_index, self._thumbnail_angle(photo)
            )
        if self._pending_list_status is not None:
            self._set_status(self._pending_list_status)
            self._pending_list_status = None

    def _build_cards_batch(self, revision: int, total: int) -> None:
        self._card_job = None
        if revision != self._list_revision:
            return
        built = 0
        while self._list_queue and built < 5:
            index = self._list_queue.pop(0)
            if index >= len(self.photos):
                continue
            self._make_photo_card(
                index,
                self.photos[index],
                index == self.selected_index,
                self._thumbnail_angle(self.photos[index]),
            )
            built += 1
        remaining = len(self._list_queue)
        if remaining:
            self._set_status(
                f"正在显示照片列表… {total - remaining} / {total}"
            )
            self._card_job = self.root.after(
                1, lambda: self._build_cards_batch(revision, total)
            )
            return
        self._list_building = False
        self._refresh_controls()
        if self._pending_list_status is not None:
            self._set_status(self._pending_list_status)
            self._pending_list_status = None

    # -- 自动拼接页 / 自定义拼接页列表 --------------------------------------

    def _page_signature(self, page: Page) -> tuple:
        return (
            page.mode,
            int(self.gap_units.get()),
            int(self.margin_units.get()),
            bool(self.auto_rotate.get()),
            tuple(
                None
                if photo is None
                else (
                    str(photo.path),
                    round(photo.zoom, 3),
                    round(photo.offset_x, 3),
                    round(photo.offset_y, 3),
                    photo.rotation,
                )
                for photo in page.photos
            ),
        )

    def _page_thumbnail(self, page: Page) -> ImageTk.PhotoImage:
        signature = self._page_signature(page)
        cached = self._page_thumb_cache.get(signature)
        if cached is not None:
            return cached
        composite = compose_page(
            page.photos,
            page.mode,
            self.gap_units.get(),
            self.margin_units.get(),
            width=PAGE_THUMB_WIDTH,
            height=PAGE_THUMB_HEIGHT,
            preview=True,
            auto_rotate=self.auto_rotate.get(),
        )
        if any(photo is None for photo in page.photos):
            scale = PAGE_THUMB_WIDTH / OUTPUT_WIDTH
            cells = layout_cells(
                PAGE_THUMB_WIDTH,
                PAGE_THUMB_HEIGHT,
                page.mode,
                unit_to_px(self.gap_units.get(), scale),
                unit_to_px(self.margin_units.get(), scale),
            )
            draw = ImageDraw.Draw(composite)
            for index, cell in enumerate(cells):
                if index >= len(page.photos) or page.photos[index] is not None:
                    continue
                draw.rectangle(
                    cell.box,
                    fill="#f7f2f8",
                    outline="#ddd2e1",
                )
        cached = ImageTk.PhotoImage(_rounded_image(composite, 7))
        if len(self._page_thumb_cache) > 400:
            self._page_thumb_cache.clear()
        self._page_thumb_cache[signature] = cached
        return cached

    def schedule_page_lists(self) -> None:
        if self._page_list_job is not None:
            try:
                self.root.after_cancel(self._page_list_job)
            except tk.TclError:
                pass
        self._page_list_job = self.root.after(80, self._refresh_page_lists)

    def _refresh_page_lists(self) -> None:
        self._page_list_job = None
        if self._page_build_job is not None:
            try:
                self.root.after_cancel(self._page_build_job)
            except tk.TclError:
                pass
            self._page_build_job = None
        self._page_list_revision += 1
        revision = self._page_list_revision

        for child in self.auto_list.winfo_children():
            child.destroy()
        for child in self.custom_list.winfo_children():
            child.destroy()
        self._auto_thumb_refs.clear()
        self._custom_thumb_refs.clear()
        self.auto_pages_count_label.configure(text=f"{len(self.pages)} 页")
        self.custom_pages_count_label.configure(
            text=f"{len(self.custom_pages)} 页"
        )

        if not self.pages:
            tk.Label(
                self.auto_list,
                text="还没有自动拼接页",
                bg=PANEL_BG,
                fg=MUTED,
                font=(self.font_family, 9),
                anchor="w",
            ).pack(fill="x", padx=6, pady=12)
        if not self.custom_pages:
            tk.Label(
                self.custom_list,
                text="还没有自定义拼接页\n点上方按钮新建空白拼接图",
                bg=PANEL_BG,
                fg=MUTED,
                font=(self.font_family, 9),
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=6, pady=12)

        items = [("auto", i) for i in range(len(self.pages))]
        items += [("custom", i) for i in range(len(self.custom_pages))]
        if not items:
            return
        self._page_build_queue = items
        self._build_page_items_batch(revision)

    def _build_page_items_batch(self, revision: int) -> None:
        self._page_build_job = None
        if revision != self._page_list_revision:
            return
        built = 0
        while self._page_build_queue and built < 4:
            kind, index = self._page_build_queue.pop(0)
            self._make_page_item(kind, index)
            built += 1
        if self._page_build_queue:
            self._page_build_job = self.root.after(
                1, lambda: self._build_page_items_batch(revision)
            )

    def _make_page_item(self, kind: str, index: int) -> None:
        pages = self.pages if kind == "auto" else self.custom_pages
        if index < 0 or index >= len(pages):
            return
        page = pages[index]
        if kind == "auto":
            selected = (
                self.preview_kind == "auto" and index == self.current_page
            )
        else:
            selected = (
                self.preview_kind == "custom"
                and index == self.current_custom_page
            )
        card_bg = CARD_SELECTED if selected else CARD_BG
        container = self.auto_list if kind == "auto" else self.custom_list
        card = tk.Frame(
            container,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=ACCENT if selected else BORDER,
            cursor="hand2",
        )
        card.pack(fill="x", padx=4, pady=(0, 8))

        thumb_image = self._page_thumbnail(page)
        if kind == "auto":
            self._auto_thumb_refs.append(thumb_image)
        else:
            self._custom_thumb_refs.append(thumb_image)
        thumb_label = tk.Label(card, image=thumb_image, bg=card_bg, bd=0)
        thumb_label.pack(padx=5, pady=(5, 3))

        if kind == "auto":
            title = f"第 {index + 1} 页 · {page.mode} 张"
        else:
            title = f"自定义 {index + 1} · {page.mode} 张"
        title_label = tk.Label(
            card,
            text=title,
            bg=card_bg,
            fg=TEXT,
            font=(self.font_family, 9, "bold" if selected else "normal"),
            anchor="w",
        )
        title_label.pack(fill="x", padx=7)

        bottom = tk.Frame(card, bg=card_bg)
        bottom.pack(fill="x", padx=7, pady=(0, 5))
        used = page.used_count()
        detail = f"{used} / {page.mode} 张"
        if used == 0:
            detail += " · 空白"
        info_label = tk.Label(
            bottom,
            text=detail,
            bg=card_bg,
            fg=ACCENT if selected else MUTED,
            font=(self.font_family, 8),
            anchor="w",
        )
        info_label.pack(side="left")

        click_targets = [card, thumb_label, title_label, bottom, info_label]
        for widget in click_targets:
            widget.bind(
                "<Button-1>",
                lambda _event, k=kind, i=index: self.select_page(k, i),
            )
            if kind == "custom":
                widget._custom_page_index = index
                self._register_custom_item_drop(widget, index)
        scroll_targets = list(click_targets)
        if kind == "custom":
            remove = tk.Label(
                bottom,
                text="×",
                bg=card_bg,
                fg=MUTED,
                font=(self.font_family, 11),
                cursor="hand2",
                padx=4,
            )
            remove.pack(side="right")
            remove.bind(
                "<Button-1>",
                lambda _event, idx=index: self.remove_custom_page(idx),
            )
            scroll_targets.append(remove)
        for widget in scroll_targets:
            self._bind_scroll_wheel(widget, container.master)

    def select_page(self, kind: str, index: int) -> None:
        if kind == "auto":
            if not (0 <= index < len(self.pages)):
                return
            self.preview_kind = "auto"
            self.current_page = index
            self.active_slot = None
        else:
            if not (0 <= index < len(self.custom_pages)):
                return
            self.preview_kind = "custom"
            self.current_custom_page = index
            self.active_custom_slot = None
        self.preview_photo_index = None
        self.selected_index = None
        self._refresh_all()

    def add_custom_page(self, mode: int) -> None:
        if self._importing or self._exporting:
            self._set_status("当前有任务正在进行，请稍候。")
            return
        self.custom_pages.append(Page(mode, [None] * mode))
        self.preview_kind = "custom"
        self.current_custom_page = len(self.custom_pages) - 1
        self.active_custom_slot = None
        self.selected_index = None
        self._refresh_all()
        self._set_status(
            f"已新建自定义拼接图 {self.current_custom_page + 1}"
            f"（{mode} 张版式），把照片拖进格子里即可。"
        )

    def remove_custom_page(self, index: int) -> None:
        if self._importing or self._exporting:
            return
        if not (0 <= index < len(self.custom_pages)):
            return
        del self.custom_pages[index]
        if self.preview_kind == "custom":
            if self.custom_pages:
                self.current_custom_page = min(
                    index, len(self.custom_pages) - 1
                )
            else:
                self.preview_kind = "auto"
        self.active_custom_slot = None
        self._refresh_all()
        self._set_status("已删除一张自定义拼接图。")

    def _clamp_custom_page(self) -> None:
        if not self.custom_pages:
            self.current_custom_page = 0
            return
        self.current_custom_page = max(
            0, min(self.current_custom_page, len(self.custom_pages) - 1)
        )

    def _active_page(self) -> Page | None:
        if self.preview_kind == "auto" and self.pages:
            self._clamp_page()
            return self.pages[self.current_page]
        if self.preview_kind == "custom" and self.custom_pages:
            self._clamp_custom_page()
            return self.custom_pages[self.current_custom_page]
        return None

    def _active_slot_index(self) -> int | None:
        if self.preview_kind == "auto":
            if (
                self.active_slot is not None
                and self.active_slot[0] == self.current_page
            ):
                return self.active_slot[1]
            return None
        if self.preview_kind == "custom":
            return self.active_custom_slot
        return None

    def _control_photo(self) -> Photo | None:
        """当前用于缩放/旋转等操作的焦点照片。"""
        if self.preview_kind == "custom":
            page = self._active_page()
            slot = self.active_custom_slot
            if page is not None and slot is not None and 0 <= slot < len(
                page.photos
            ):
                return page.photos[slot]
            return None
        return self._selected_photo()

    def _make_photo_card(
        self, index: int, photo: Photo, selected: bool, angle: int
    ) -> tk.Frame:
        """创建左侧列表里的一张照片卡片（导入过程中也会逐张调用）。"""
        card_bg = CARD_SELECTED if selected else CARD_BG
        card = tk.Frame(
            self.photo_list,
            bg=card_bg,
            highlightthickness=1,
            highlightbackground=ACCENT if selected else BORDER,
            cursor="hand2",
        )
        card.pack(fill="x", padx=5, pady=(0, 8))

        thumb_image = self._thumbnail_image(photo, angle)
        self._thumb_refs.append(thumb_image)

        img_label = tk.Label(
            card,
            image=thumb_image,
            bg=card_bg,
            bd=0,
        )
        img_label.pack(side="left", padx=(7, 8), pady=7)

        text_box = tk.Frame(card, bg=card_bg)
        text_box.pack(side="left", fill="both", expand=True, pady=7)

        kind = photo_kind(photo)
        location = self._location_of(photo)
        if location is None:
            page_info = f"{kind} · 未使用 · 可拖入拼接图"
            info_color = MUTED
        else:
            page_index, slot_index = location
            page_mode = self.pages[page_index].mode
            page_info = (
                f"{kind} · 第 {page_index + 1} 页 · "
                f"{slot_label(slot_index, page_mode)}"
            )
            info_color = ACCENT if selected else MUTED

        name_label = tk.Label(
            text_box,
            text=f"{index + 1}. {photo.name}",
            bg=card_bg,
            fg=TEXT,
            font=(self.font_family, 9, "bold" if selected else "normal"),
            anchor="w",
            justify="left",
            wraplength=136,
        )
        name_label.pack(fill="x", anchor="w")
        info_label = tk.Label(
            text_box,
            text=page_info,
            bg=card_bg,
            fg=info_color,
            font=(self.font_family, 8),
            anchor="w",
        )
        info_label.pack(fill="x", anchor="w")

        remove = tk.Label(
            card,
            text="×",
            bg=card_bg,
            fg=MUTED,
            font=(self.font_family, 12),
            cursor="hand2",
            padx=5,
        )
        remove.pack(side="right", padx=(0, 3), pady=7)
        remove.bind(
            "<Button-1>", lambda _event, idx=index: self._remove_index(idx)
        )

        for widget in (card, img_label, text_box, name_label, info_label):
            widget.bind(
                "<ButtonPress-1>",
                lambda event, idx=index: self._on_card_press(idx, event),
            )
            widget.bind("<B1-Motion>", self._on_card_motion)
            widget.bind("<ButtonRelease-1>", self._on_card_release)
            widget.bind(
                "<Double-Button-1>",
                lambda _event, idx=index: self._put_photo_in_slot(idx),
            )

        hover_targets = (
            card,
            img_label,
            text_box,
            name_label,
            info_label,
            remove,
        )

        def on_enter(
            _event,
            targets=hover_targets,
            is_selected=selected,
        ) -> None:
            if is_selected:
                return
            for target in targets:
                try:
                    target.configure(bg=CARD_HOVER)
                except tk.TclError:
                    pass

        def on_leave(
            _event,
            targets=hover_targets,
            is_selected=selected,
        ) -> None:
            if is_selected:
                return
            for target in targets:
                try:
                    target.configure(bg=CARD_BG)
                except tk.TclError:
                    pass

        for widget in hover_targets:
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)
            self._bind_scroll_wheel(widget, self.photo_canvas)
        return card

    def _remove_index(self, index: int) -> None:
        if self._importing or self._exporting or self._list_building:
            return
        if index < 0 or index >= len(self.photos):
            return
        self.selected_index = index
        self.remove_selected()

    def _refresh_controls(self) -> None:
        self._clamp_page()
        self._clamp_custom_page()
        selected = self._selected_photo()
        control = self._control_photo()

        if control is not None:
            if self.preview_kind == "custom":
                suffix = f" · 自定义 {self.current_custom_page + 1}"
                page = self._active_page()
                slot = self.active_custom_slot
                if page is not None and slot is not None:
                    suffix += f" {slot_label(slot, page.mode)}"
                label = f"当前 · {control.name}{suffix}"
            else:
                location = self._location_of(control)
                if location is None:
                    suffix = " · 未使用"
                else:
                    page_mode = self.pages[location[0]].mode
                    suffix = (
                        f" · 第 {location[0] + 1} 页 "
                        f"{slot_label(location[1], page_mode)}"
                    )
                label = f"选中 · {control.name}{suffix}"
            self.selected_label.configure(text=label)
            zoom_percent = int(round(control.zoom * 100))
            self._syncing_zoom = True
            self.zoom_value.set(zoom_percent)
            self._syncing_zoom = False
            self.zoom_text.configure(text=f"{zoom_percent}%")
        else:
            self.selected_label.configure(text="选中照片缩放")
            self.zoom_text.configure(text="—")

        self.zoom_scale.set_state("normal" if control else "disabled")
        self.rotate_button.configure(
            state="normal" if control else "disabled"
        )
        self.reset_view_button.configure(
            state="normal" if control else "disabled"
        )
        self.put_button.configure(
            state="normal" if selected is not None else "disabled"
        )
        if self.preview_kind == "custom":
            page = self._active_page()
            slot = self.active_custom_slot
            can_move_out = (
                page is not None
                and slot is not None
                and 0 <= slot < len(page.photos)
                and page.photos[slot] is not None
            )
        else:
            can_move_out = (
                selected is not None
                and self._location_of(selected) is not None
            )
        self.move_out_button.configure(
            state="normal" if can_move_out else "disabled"
        )
        self.remove_button.configure(
            state="normal" if control is not None else "disabled"
        )

        gap_mm = self.gap_units.get() * UNIT_MM
        margin_mm = self.margin_units.get() * UNIT_MM
        self.gap_text.configure(text=f"{self.gap_units.get()}  ({gap_mm:.1f} mm)")
        self.margin_text.configure(
            text=f"{self.margin_units.get()}  ({margin_mm:.1f} mm)"
        )

        self.reroll_button.configure(
            state="normal"
            if self.mode.get() == 0 and not self._importing
            else "disabled"
        )

        self._refresh_page_navigation()

        self._update_export_buttons()

    def _refresh_page_navigation(self) -> None:
        if self.preview_kind == "photo":
            index = self.preview_photo_index
            if index is None:
                index = self.selected_index
            self.preview_title_label.configure(text="单张照片预览")
            if index is not None and 0 <= index < len(self.photos):
                photo = self.photos[index]
                self.page_label.configure(
                    text=f"{photo.name} · {photo.width} × {photo.height} px"
                )
            else:
                self.page_label.configure(text="")
            self.page_number_label.configure(text="")
            self.prev_button.configure(
                state="normal" if index is not None and index > 0 else "disabled"
            )
            self.next_button.configure(
                state="normal"
                if index is not None and index < len(self.photos) - 1
                else "disabled"
            )
            self.delete_page_button.configure(state="disabled")
            self.new_page_button.configure(state="disabled")
            return

        if self.preview_kind == "custom":
            self.preview_title_label.configure(text="自定义拼接预览")
            self.new_page_button.configure(state="disabled")
            if not self.custom_pages:
                self.page_label.configure(text="还没有自定义拼接图")
                self.page_number_label.configure(text="")
                self.prev_button.configure(state="disabled")
                self.next_button.configure(state="disabled")
                self.delete_page_button.configure(state="disabled")
                return
            page = self.custom_pages[self.current_custom_page]
            kind = "田字格" if page.mode == 4 else "左右"
            self.page_label.configure(
                text=f"本页 {page.used_count()} / {page.mode} 张 · {kind}"
            )
            self.page_number_label.configure(
                text=(
                    f"自定义 {self.current_custom_page + 1} / "
                    f"{len(self.custom_pages)}"
                )
            )
            self.prev_button.configure(
                state="normal" if self.current_custom_page > 0 else "disabled"
            )
            self.next_button.configure(
                state="normal"
                if self.current_custom_page < len(self.custom_pages) - 1
                else "disabled"
            )
            self.delete_page_button.configure(state="normal")
            return

        self.preview_title_label.configure(text="自动拼接预览")
        self.new_page_button.configure(state="normal")
        if self.pages:
            page = self.pages[self.current_page]
            kind = "田字格" if page.mode == 4 else "左右"
            self.page_label.configure(
                text=f"本页 {page.used_count()} / {page.mode} 张 · {kind}"
            )
            self.page_number_label.configure(
                text=f"第 {self.current_page + 1} / {len(self.pages)} 页"
            )
            self.prev_button.configure(
                state="normal" if self.current_page > 0 else "disabled"
            )
            self.next_button.configure(
                state="normal"
                if self.current_page < len(self.pages) - 1
                else "disabled"
            )
            self.delete_page_button.configure(
                state="normal" if len(self.pages) > 1 else "disabled"
            )
        else:
            self.page_label.configure(text="")
            self.page_number_label.configure(text="")
            self.prev_button.configure(state="disabled")
            self.next_button.configure(state="disabled")
            self.delete_page_button.configure(state="disabled")

    # -- 预览绘制 -----------------------------------------------------------

    def schedule_preview(self) -> None:
        if self._render_job is not None:
            self.root.after_cancel(self._render_job)
        self._render_job = self.root.after(30, self.render_preview)

    def _on_preview_resize(self, _event: tk.Event) -> None:
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(70, self.render_preview)

    def _preview_geometry(self) -> tuple[int, int, int, int] | None:
        cw = self.preview_canvas.winfo_width()
        ch = self.preview_canvas.winfo_height()
        if cw <= 20 or ch <= 20:
            return None
        pad = 22
        max_w = cw - pad * 2
        max_h = ch - pad * 2
        if max_w <= 20 or max_h <= 20:
            return None
        width = min(max_w, int(max_h * OUTPUT_WIDTH / OUTPUT_HEIGHT))
        height = int(round(width * OUTPUT_HEIGHT / OUTPUT_WIDTH))
        if width > max_w:
            width = int(max_w)
            height = int(round(width * OUTPUT_HEIGHT / OUTPUT_WIDTH))
        return width, height, (cw - width) // 2, (ch - height) // 2

    def _draw_empty_preview(self, canvas: tk.Canvas, title: str) -> None:
        canvas.create_text(
            canvas.winfo_width() // 2,
            canvas.winfo_height() // 2 - 12,
            text=title,
            fill="#a99cb4",
            font=(self.font_family, 14),
        )
        canvas.create_text(
            canvas.winfo_width() // 2,
            canvas.winfo_height() // 2 + 18,
            text="输出规格：1800 × 1200 px · 300 dpi · 3:2",
            fill="#b7acc0",
            font=(self.font_family, 9),
        )
        self._preview_image = None
        self._preview_composite = None
        self._preview_cells = []
        self._preview_page_photos = []

    def render_preview(self) -> None:
        self._render_job = None
        canvas = self.preview_canvas
        canvas.delete("all")
        geometry = self._preview_geometry()
        if geometry is None:
            return
        width, height, ox, oy = geometry

        if self.preview_kind == "photo":
            self._render_photo_preview(canvas, width, height, ox, oy)
            return

        if not self.photos and self.preview_kind != "custom":
            self._draw_empty_preview(
                canvas, "添加照片后，这里会显示拼接效果"
            )
            return

        page = self._active_page()
        if page is None:
            self._draw_empty_preview(
                canvas, "在右侧自定义栏新建一张空白拼接图"
            )
            return

        page_photos = list(page.photos)
        try:
            composite = compose_page(
                page_photos,
                page.mode,
                self.gap_units.get(),
                self.margin_units.get(),
                width=width,
                height=height,
                preview=True,
                auto_rotate=self.auto_rotate.get(),
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"预览生成失败：{exc}")
            return

        # 圆角相纸 + 两层很浅的投影，在浅色背景上也显得轻盈。
        radius = min(14, max(6, int(min(width, height) * 0.018)))
        _canvas_round_rect(
            canvas,
            ox + 8,
            oy + 10,
            ox + width + 8,
            oy + height + 10,
            radius + 2,
            fill="#ddd2e1",
            outline="",
        )
        _canvas_round_rect(
            canvas,
            ox + 3,
            oy + 5,
            ox + width + 3,
            oy + height + 5,
            radius,
            fill="#e7ddeb",
            outline="",
        )
        self._preview_image = ImageTk.PhotoImage(
            _rounded_image(composite, radius)
        )
        self._preview_composite = composite
        canvas.create_image(ox, oy, image=self._preview_image, anchor="nw")
        _canvas_round_rect(
            canvas,
            ox,
            oy,
            ox + width,
            oy + height,
            radius,
            fill="",
            outline="#d7cbe0",
            width=1,
        )

        scale = width / OUTPUT_WIDTH
        gap_px = unit_to_px(self.gap_units.get(), scale)
        margin_px = unit_to_px(self.margin_units.get(), scale)
        cells = layout_cells(width, height, page.mode, gap_px, margin_px)
        self._preview_origin = (ox, oy)
        self._preview_scale = scale
        self._preview_cells = cells
        self._preview_page_photos = page_photos

        # 只保留当前页照片的旋转缓存，避免浏览大量高清图时内存持续增长。
        current_ids = {id(photo) for photo in page_photos if photo is not None}
        for photo in self.photos:
            if id(photo) not in current_ids:
                photo.clear_rotation_cache()
        for custom_page in self.custom_pages:
            for photo in custom_page.photos:
                if photo is not None and id(photo) not in current_ids:
                    photo.clear_rotation_cache()

        # 空格子画虚线；选中空格再画蓝色框，提示照片会放进去的位置。
        for i, cell in enumerate(cells):
            if page_photos[i] is None:
                x0, y0, x1, y1 = cell.box
                canvas.create_rectangle(
                    ox + x0 + 1,
                    oy + y0 + 1,
                    ox + x1 - 1,
                    oy + y1 - 1,
                    outline="#d9cee2",
                    dash=(5, 4),
                    width=1,
                )

        active_index = self._active_slot_index()
        if (
            active_index is not None
            and active_index < len(page_photos)
            and page_photos[active_index] is None
        ):
            x0, y0, x1, y1 = cells[active_index].box
            canvas.create_rectangle(
                ox + x0 + 2,
                oy + y0 + 2,
                ox + x1 - 2,
                oy + y1 - 2,
                outline=ACCENT,
                dash=(4, 3),
                width=2,
            )

        for i, cell in enumerate(cells):
            if page_photos[i] is None:
                continue
            x0, y0, x1, y1 = cell.box
            color = ACCENT_BLUSH if i == active_index else "#efe7f3"
            width_line = 2 if i == active_index else 1
            canvas.create_rectangle(
                ox + x0 + 1,
                oy + y0 + 1,
                ox + x1 - 1,
                oy + y1 - 1,
                outline=color,
                width=width_line,
            )

        # 从左侧拖照片过来时，高亮即将放入的格子。
        if self._drop_hover is not None and self._drop_hover < len(cells):
            x0, y0, x1, y1 = cells[self._drop_hover].box
            canvas.create_rectangle(
                ox + x0 + 1,
                oy + y0 + 1,
                ox + x1 - 1,
                oy + y1 - 1,
                outline=ACCENT,
                dash=(6, 4),
                width=3,
            )

        self._draw_drag_trail(canvas)

    def _render_photo_preview(
        self,
        canvas: tk.Canvas,
        width: int,
        height: int,
        ox: int,
        oy: int,
    ) -> None:
        index = self.preview_photo_index
        if index is None:
            index = self.selected_index
        if index is None or not (0 <= index < len(self.photos)):
            self._draw_empty_preview(canvas, "点击左侧单张照片进行预览")
            return

        photo = self.photos[index]
        self.preview_photo_index = index
        source = photo.oriented_image(photo.rotation % 360, cache=False)
        canvas_w = canvas.winfo_width()
        canvas_h = canvas.winfo_height()
        pad = 26
        max_w = max(20, canvas_w - pad * 2)
        max_h = max(20, canvas_h - pad * 2)
        if source.width <= 0 or source.height <= 0:
            return
        scale = min(max_w / source.width, max_h / source.height, 1.0)
        disp_w = max(1, int(round(source.width * scale)))
        disp_h = max(1, int(round(source.height * scale)))
        if (disp_w, disp_h) != source.size:
            display = source.resize(
                (disp_w, disp_h), Image.Resampling.LANCZOS
            )
        else:
            display = source
        px = (canvas_w - disp_w) // 2
        py = (canvas_h - disp_h) // 2
        radius = min(14, max(6, int(min(disp_w, disp_h) * 0.018)))

        _canvas_round_rect(
            canvas,
            px + 7,
            py + 9,
            px + disp_w + 7,
            py + disp_h + 9,
            radius + 2,
            fill="#ddd2e1",
            outline="",
        )
        _canvas_round_rect(
            canvas,
            px + 3,
            py + 4,
            px + disp_w + 3,
            py + disp_h + 4,
            radius,
            fill="#e7ddeb",
            outline="",
        )
        self._preview_image = ImageTk.PhotoImage(
            _rounded_image(display.convert("RGB"), radius)
        )
        self._preview_composite = display.convert("RGB")
        canvas.create_image(px, py, image=self._preview_image, anchor="nw")
        _canvas_round_rect(
            canvas,
            px,
            py,
            px + disp_w,
            py + disp_h,
            radius,
            fill="",
            outline="#d7cbe0",
            width=1,
        )
        caption_y = min(canvas_h - 8, py + disp_h + 16)
        canvas.create_text(
            canvas_w // 2,
            caption_y,
            text=(
                f"{photo.name} · {photo.width} × {photo.height} px · "
                f"单张预览"
            ),
            fill=MUTED,
            font=(self.font_family, 9),
        )
        self._preview_origin = (px, py)
        self._preview_scale = scale
        self._preview_cells = []
        self._preview_page_photos = []

    def _draw_drag_trail(self, canvas: tk.Canvas) -> None:
        """拖动照片时在画布上叠几层半透明残影。"""
        if (
            self._drag is None
            or not self._drag_trail
            or not self._drag_ghost_images
        ):
            return
        total = len(self._drag_trail)
        for index, (x, y) in enumerate(self._drag_trail):
            # 最新的位置用最实的残影，越旧越淡。
            ghost_index = min(len(self._drag_ghost_images) - 1, total - 1 - index)
            image = self._drag_ghost_images[ghost_index]
            canvas.create_image(x, y, image=image, anchor="center")
            if index == total - 1:
                half_w = image.width() / 2
                half_h = image.height() / 2
                canvas.create_rectangle(
                    x - half_w,
                    y - half_h,
                    x + half_w,
                    y + half_h,
                    outline="#f2eaf5",
                    width=1,
                )

    # -- 交互 ---------------------------------------------------------------

    def _on_zoom_slider(self, value: str) -> None:
        photo = self._control_photo()
        if self._syncing_zoom or photo is None:
            return
        percent = max(100, min(int(MAX_ZOOM * 100), int(round(float(value)))))
        photo.zoom = percent / 100
        self.zoom_text.configure(text=f"{percent}%")
        self.schedule_page_lists()
        self.schedule_preview()
        self._set_status(f"{photo.name} 缩放 {percent}%。")

    def _on_preview_wheel(self, event: tk.Event) -> str:
        """在拼接区用滚轮缩放鼠标下的那张照片。"""
        if self._drag is not None:
            return "break"
        cell_index = self._preview_cell_at(event)
        page = self._active_page()
        if cell_index is None or page is None:
            return "break"
        if cell_index >= len(page.photos) or page.photos[cell_index] is None:
            self._set_status("这个位置还没有照片，先从左侧拖一张进来。")
            return "break"

        photo = page.photos[cell_index]
        if getattr(event, "num", None) == 4:
            direction = 1
        elif getattr(event, "num", None) == 5:
            direction = -1
        else:
            direction = 1 if getattr(event, "delta", 0) > 0 else -1
        new_zoom = max(1.0, min(MAX_ZOOM, photo.zoom + direction * 0.1))
        if abs(new_zoom - photo.zoom) < 1e-6:
            self._set_status(
                "已经缩放到底了。" if direction < 0 else "已经放到最大了。"
            )
            return "break"

        cell = self._preview_cells[cell_index]
        ox, oy = self._preview_origin
        fraction_x = (event.x - ox - cell.x) / max(1, cell.width)
        fraction_y = (event.y - oy - cell.y) / max(1, cell.height)
        zoom_photo_at(
            photo,
            cell.width,
            cell.height,
            self.auto_rotate.get(),
            fraction_x,
            fraction_y,
            new_zoom,
        )

        index = self._pool_index(photo)
        if index is None:
            index = self._pool_index_by_path(photo)
        selection_changed = index is not None and index != self.selected_index
        if index is not None:
            self.selected_index = index
        if self.preview_kind == "custom":
            self.active_custom_slot = cell_index
        else:
            self.active_slot = (self.current_page, cell_index)
        if selection_changed:
            self._refresh_photo_list()
        self._refresh_controls()
        self.schedule_page_lists()
        self.schedule_preview()
        self._set_status(
            f"{photo.name} 缩放 {int(round(photo.zoom * 100))}%（滚轮，以光标为中心）"
        )
        return "break"

    def _on_spacing_changed(self, _value: str) -> None:
        self._refresh_controls()
        self.schedule_page_lists()
        self.schedule_preview()

    def _on_mode_changed(self) -> None:
        self._reflow_pages()
        self._refresh_all()
        summary = self._category_summary(self._assigned_photos())
        if self.mode.get() == 0:
            self._set_status(
                f"已切换为自动混排：同场景/人物/风景优先，其次同建筑、"
                f"最后同色系（{summary}）。"
            )
        else:
            self._set_status(
                f"已切换为 {self.mode.get()} 张拼版，相近照片会相邻排列（{summary}）。"
            )

    def _on_auto_rotate_changed(self) -> None:
        self._refresh_all()
        state = "开启" if self.auto_rotate.get() else "关闭"
        self._set_status(f"自动旋转已{state}。")

    def _rerandomize(self) -> None:
        if self.mode.get() != 0:
            return
        self._reflow_pages()
        self._refresh_all()
        summary = self._category_summary(self._assigned_photos())
        self._set_status(
            f"已重新随机混排：同场景/人物/风景优先，其次同建筑、"
            f"最后同色系（{summary}）。"
        )

    def _on_card_press(self, index: int, event: tk.Event) -> None:
        if self._importing or self._exporting or self._list_building:
            return
        self._card_drag = {
            "index": index,
            "start": (event.x_root, event.y_root),
            "dragging": False,
        }

    def _on_card_motion(self, event: tk.Event) -> None:
        drag = self._card_drag
        if drag is None:
            return
        if not drag["dragging"]:
            dx = event.x_root - drag["start"][0]
            dy = event.y_root - drag["start"][1]
            if dx * dx + dy * dy < 36:
                return
            index = drag["index"]
            if index >= len(self.photos):
                return
            drag["dragging"] = True
            self.root.configure(cursor="fleur")
            self._floating_ghost.show(
                self.photos[index].image, event.x_root, event.y_root
            )
            self._set_status("拖到右侧格子上松开即可放入；拖回列表取消。")

        if drag["dragging"]:
            self._floating_ghost.move(event.x_root, event.y_root)
            slot = self._cell_at_root(event.x_root, event.y_root)
            if slot != self._drop_hover:
                self._drop_hover = slot
                self.schedule_preview()

    def _on_card_release(self, event: tk.Event) -> None:
        drag = self._card_drag
        self._card_drag = None
        if drag is None:
            return
        self.root.configure(cursor="")
        self._floating_ghost.hide()
        if self._drop_hover is not None:
            self._drop_hover = None
            self.schedule_preview()
        index = drag["index"]
        if index >= len(self.photos):
            return
        photo = self.photos[index]
        if drag["dragging"]:
            slot = self._cell_at_root(event.x_root, event.y_root)
            if slot is not None:
                if self.preview_kind == "custom" and self.custom_pages:
                    self._assign_photo_to_custom_slot(
                        photo, self.current_custom_page, slot
                    )
                else:
                    self._assign_photo_to_slot(
                        photo, self.current_page, slot
                    )
                return
            custom_index = self._custom_page_at_root(
                event.x_root, event.y_root
            )
            if custom_index is not None and 0 <= custom_index < len(
                self.custom_pages
            ):
                target_slot = self._first_empty_custom_slot(custom_index)
                if target_slot is None:
                    self._set_status(
                        "这张自定义拼接图已经放满了，先新建一张空白拼接图。"
                    )
                    return
                self._assign_photo_to_custom_slot(
                    photo, custom_index, target_slot
                )
                return
            self._set_status("没有放到格子里，照片未移动。")
        self.select_photo(index)

    def _setup_external_dnd(self) -> None:
        """Windows 下允许把资源管理器里的图片文件直接拖进预览区。"""
        if not OS_FILE_DND:
            return
        try:
            canvas = self.preview_canvas
            canvas.drop_target_register(DND_FILES)
            canvas.dnd_bind("<<Drop>>", self._on_os_drop)
            canvas.dnd_bind("<<DropEnter>>", self._on_os_drop_hover)
            canvas.dnd_bind("<<DropPosition>>", self._on_os_drop_hover)
            canvas.dnd_bind("<<DropLeave>>", self._on_os_drop_leave)
        except Exception:  # noqa: BLE001 - 外部拖入不可用不影响其他功能
            pass

    def _on_os_drop_hover(self, event: tk.Event):
        cell = self._preview_cell_at(event)
        if cell != self._drop_hover:
            self._drop_hover = cell
            self.schedule_preview()
        return getattr(event, "action", None)

    def _on_os_drop_leave(self, _event: tk.Event) -> None:
        if self._drop_hover is not None:
            self._drop_hover = None
            self.schedule_preview()

    def _event_image_paths(self, event: tk.Event) -> list[str]:
        try:
            raw_paths = self.root.tk.splitlist(event.data)
        except (tk.TclError, ValueError):
            raw_paths = [getattr(event, "data", "")]
        return [
            str(path)
            for path in raw_paths
            if Path(str(path)).suffix.lower() in IMAGE_EXTENSIONS
        ]

    def _on_os_drop(self, event: tk.Event):
        self._drop_hover = None
        paths = self._event_image_paths(event)
        if not paths:
            self._set_status("只支持拖入 JPG / PNG / WebP / BMP / TIFF 图片。")
            return getattr(event, "action", None)
        if self.preview_kind == "custom" and self.custom_pages:
            self._clamp_custom_page()
            page_index = self.current_custom_page
            page = self.custom_pages[page_index]
            cell = None
            if len(paths) == 1:
                dropped = self._preview_cell_at(event)
                if (
                    dropped is not None
                    and dropped < len(page.photos)
                    and page.photos[dropped] is None
                ):
                    cell = dropped
            if cell is None:
                cell = self._first_empty_custom_slot(page_index)
            if cell is None:
                # 当前页已经放满：自动新建一张同样版式的空白拼接图。
                self.custom_pages.append(Page(page.mode, [None] * page.mode))
                page_index = len(self.custom_pages) - 1
                cell = 0
            self._start_import(paths, drop_target=(page_index, cell))
        else:
            self._start_import(paths)
        return getattr(event, "action", None)

    def _register_custom_item_drop(
        self, widget: tk.Widget, page_index: int
    ) -> None:
        """允许把图片文件直接拖到自定义拼接栏的某一页缩略图上。"""
        if not OS_FILE_DND:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind(
                "<<Drop>>",
                lambda event, idx=page_index: self._on_os_drop_on_custom_item(
                    event, idx
                ),
            )
        except Exception:  # noqa: BLE001
            pass

    def _on_os_drop_on_custom_item(
        self, event: tk.Event, page_index: int
    ):
        paths = self._event_image_paths(event)
        if not paths:
            self._set_status("只支持拖入 JPG / PNG / WebP / BMP / TIFF 图片。")
            return getattr(event, "action", None)
        if not (0 <= page_index < len(self.custom_pages)):
            return getattr(event, "action", None)
        slot = self._first_empty_custom_slot(page_index)
        if slot is None:
            page = self.custom_pages[page_index]
            self.custom_pages.append(Page(page.mode, [None] * page.mode))
            page_index = len(self.custom_pages) - 1
            slot = 0
        self.select_page("custom", page_index)
        self._start_import(paths, drop_target=(page_index, slot))
        return getattr(event, "action", None)

    def _custom_page_at_root(
        self, x_root: int, y_root: int
    ) -> int | None:
        try:
            widget = self.root.winfo_containing(x_root, y_root)
        except tk.TclError:
            return None
        while widget is not None:
            page_index = getattr(widget, "_custom_page_index", None)
            if page_index is not None:
                return page_index
            widget = widget.master
        return None

    def _cell_at_root(self, x_root: int, y_root: int) -> int | None:
        if not self._preview_cells:
            return None
        canvas = self.preview_canvas
        x = x_root - canvas.winfo_rootx() - self._preview_origin[0]
        y = y_root - canvas.winfo_rooty() - self._preview_origin[1]
        for index, cell in enumerate(self._preview_cells):
            if cell.contains(x, y):
                return index
        return None

    def _preview_cell_at(self, event: tk.Event) -> int | None:
        if not self._preview_cells:
            return None
        ox, oy = self._preview_origin
        x = event.x - ox
        y = event.y - oy
        for i, cell in enumerate(self._preview_cells):
            if cell.contains(x, y):
                return i
        return None

    def _on_preview_press(self, event: tk.Event) -> None:
        cell_index = self._preview_cell_at(event)
        page = self._active_page()
        if cell_index is None or page is None:
            return
        if self.preview_kind == "custom":
            self.active_custom_slot = cell_index
        else:
            self.active_slot = (self.current_page, cell_index)
        photo = (
            page.photos[cell_index]
            if cell_index < len(page.photos)
            else None
        )
        if photo is None:
            self.render_preview()
            self._set_status(
                "已选中空格：把左侧照片拖进来，或选中照片后点「放入选中格」。"
            )
            return

        index = self._pool_index(photo)
        if index is None:
            index = self._pool_index_by_path(photo)
        if index is not None:
            self.selected_index = index
        cell = self._preview_cells[cell_index]
        ox, oy = self._preview_origin
        self._drag = {
            "photo": photo,
            "start_x": event.x,
            "start_y": event.y,
            "offset_x": photo.offset_x,
            "offset_y": photo.offset_y,
            "cell": cell,
            "ghost_dx": event.x - (ox + cell.x + cell.width / 2),
            "ghost_dy": event.y - (oy + cell.y + cell.height / 2),
        }
        self._drag_moved = False
        self.preview_canvas.configure(cursor="fleur")
        self._prepare_drag_trail(cell_index)
        self._refresh_photo_list()
        self._refresh_controls()
        self.render_preview()

    def _prepare_drag_trail(self, cell_index: int) -> None:
        """把当前格子的画面做成几档透明度的残影，拖动时使用。"""
        self._drag_trail = []
        self._drag_ghost_images = []
        composite = self._preview_composite
        if composite is None or cell_index >= len(self._preview_cells):
            return
        cell = self._preview_cells[cell_index]
        try:
            crop = composite.crop(cell.box).convert("RGBA")
        except (ValueError, OSError):
            return
        max_side = 280
        if max(crop.size) > max_side:
            factor = max_side / max(crop.size)
            crop = crop.resize(
                (
                    max(1, int(crop.width * factor)),
                    max(1, int(crop.height * factor)),
                ),
                Image.Resampling.BILINEAR,
            )
        for alpha in (150, 95, 50):
            ghost = crop.copy()
            ghost.putalpha(alpha)
            self._drag_ghost_images.append(ImageTk.PhotoImage(ghost))

    def _update_drag_trail(self, event: tk.Event) -> None:
        drag = self._drag
        if drag is None:
            return
        position = (
            event.x - drag["ghost_dx"],
            event.y - drag["ghost_dy"],
        )
        if not self._drag_trail:
            self._drag_trail.append(position)
        else:
            last_x, last_y = self._drag_trail[-1]
            if (position[0] - last_x) ** 2 + (position[1] - last_y) ** 2 >= 64:
                self._drag_trail.append(position)
                self._drag_trail = self._drag_trail[-3:]
            else:
                self._drag_trail[-1] = position

    def _on_preview_drag(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        photo: Photo = self._drag["photo"]
        cell: Cell = self._drag["cell"]
        dx = event.x - self._drag["start_x"]
        dy = event.y - self._drag["start_y"]
        if abs(dx) < 1 and abs(dy) < 1:
            return

        geometry = crop_geometry(
            photo, cell.width, cell.height, auto_rotate=self.auto_rotate.get()
        )
        if geometry.max_center_x < 0.5 and geometry.max_center_y < 0.5:
            self._set_status("当前照片已填满格子，滚轮放大后可调整取景位置。")
            return

        if geometry.max_center_x > 0:
            shift_x = dx * geometry.crop_w / max(1, cell.width)
            photo.offset_x = max(
                -1.0,
                min(
                    1.0,
                    self._drag["offset_x"] - shift_x / geometry.max_center_x,
                ),
            )
        if geometry.max_center_y > 0:
            shift_y = dy * geometry.crop_h / max(1, cell.height)
            photo.offset_y = max(
                -1.0,
                min(
                    1.0,
                    self._drag["offset_y"] - shift_y / geometry.max_center_y,
                ),
            )
        self._drag_moved = True
        self._update_drag_trail(event)
        self.schedule_preview()

    def _on_preview_release(self, _event: tk.Event) -> None:
        if self._drag is None:
            self.preview_canvas.configure(cursor="hand2")
            return
        self._drag = None
        self._drag_trail = []
        self._drag_ghost_images = []
        self.preview_canvas.configure(cursor="hand2")
        if self._drag_moved:
            self._set_status("已更新取景位置；滚轮可继续缩放。")
        self._drag_moved = False
        self.schedule_page_lists()
        self.schedule_preview()

    # -- 列表滚动 -----------------------------------------------------------

    def _on_photo_list_configure(self, _event: tk.Event) -> None:
        self.photo_canvas.configure(scrollregion=self.photo_canvas.bbox("all"))

    def _on_photo_canvas_configure(self, event: tk.Event) -> None:
        self.photo_canvas.itemconfigure(self._photo_window, width=event.width)

    def _bind_mousewheel(self, _event: tk.Event) -> None:
        self.photo_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.photo_canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.photo_canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event) -> None:
        self.photo_canvas.unbind_all("<MouseWheel>")
        self.photo_canvas.unbind_all("<Button-4>")
        self.photo_canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.photo_canvas.yview_scroll(delta, "units")

    # -- 导出 ---------------------------------------------------------------

    def export_all(self) -> None:
        if self._importing:
            self._set_status("正在导入照片，请等待导入完成后再导出。")
            return
        if self._exporting:
            self._set_status("正在导出照片，请稍候…")
            return
        entries: list[tuple[str, int, Page]] = []
        entries += [
            ("auto", index, page)
            for index, page in enumerate(self.pages)
            if page.used_count()
        ]
        entries += [
            ("custom", index, page)
            for index, page in enumerate(self.custom_pages)
            if page.used_count()
        ]
        if not entries:
            if self.photos:
                messagebox.showinfo(
                    "没有可导出的拼接页",
                    "当前还没有填好照片的拼接图。\n"
                    "自动拼接页或自定义拼接图里放好照片后再导出。",
                    parent=self.root,
                )
            return
        auto_rotate = self.auto_rotate.get()
        gap_units = self.gap_units.get()
        margin_units = self.margin_units.get()

        def default_name(kind: str, index: int, page: Page) -> str:
            prefix = "六寸拼接" if kind == "auto" else "自定义拼接"
            return f"{prefix}_第{index + 1}页_{page.mode}张.jpg"

        if len(entries) == 1:
            kind, index, page = entries[0]
            path = self._pick_save_path(default_name(kind, index, page))
            if not path:
                return
            mode, photos = self._snapshot_page(page)
            self._export_single = True
            self._export_folder = ""
            self._start_export(
                [(mode, photos, Path(path))],
                gap_units,
                margin_units,
                auto_rotate,
            )
            return

        folder = self._pick_export_folder()
        if not folder:
            return
        targets: list[tuple[int, list[Photo | None], Path]] = []
        for kind, index, page in entries:
            mode, photos = self._snapshot_page(page)
            out_path = Path(folder) / default_name(kind, index, page)
            targets.append((mode, photos, out_path))
        self._export_single = False
        self._export_folder = folder
        self._start_export(
            targets, gap_units, margin_units, auto_rotate
        )

    def export_current_page(self) -> None:
        """只导出预览里正在显示的这张拼接图。"""
        if self._importing:
            self._set_status("正在导入照片，请等待导入完成后再导出。")
            return
        if self._exporting:
            self._set_status("正在导出照片，请稍候…")
            return
        if self.preview_kind == "photo":
            messagebox.showinfo(
                "当前是单张照片预览",
                "请先点击中间「自动拼接」或右侧「自定义拼接」里的"
                "拼接图，再导出本页。",
                parent=self.root,
            )
            return
        page = self._active_page()
        if page is None or not page.used_count():
            messagebox.showinfo(
                "本页还没有照片",
                "当前预览的拼接图还没有放入照片。\n"
                "先在格子里放好照片，再导出本页。",
                parent=self.root,
            )
            return
        if self.preview_kind == "auto":
            default_name = (
                f"六寸拼接_第{self.current_page + 1}页_{page.mode}张.jpg"
            )
        else:
            default_name = (
                f"自定义拼接_第{self.current_custom_page + 1}页_"
                f"{page.mode}张.jpg"
            )
        path = self._pick_save_path(default_name)
        if not path:
            return
        mode, photos = self._snapshot_page(page)
        self._export_single = True
        self._export_folder = ""
        self._start_export(
            [(mode, photos, Path(path))],
            self.gap_units.get(),
            self.margin_units.get(),
            self.auto_rotate.get(),
        )

    @staticmethod
    def _snapshot_page(page: Page) -> tuple[int, list[Photo | None]]:
        """导出前复制裁剪参数，避免导出过程中界面操作影响结果。"""
        return (
            page.mode,
            [
                photo.clone_for_export() if photo is not None else None
                for photo in page.photos
            ],
        )

    def _update_export_buttons(self) -> None:
        busy = self._importing or self._exporting
        has_auto = any(page.used_count() for page in self.pages)
        has_custom = any(page.used_count() for page in self.custom_pages)
        self.export_button.set_enabled((has_auto or has_custom) and not busy)
        current_used = False
        page = self._active_page()
        if page is not None and self.preview_kind in ("auto", "custom"):
            current_used = page.used_count() > 0
        self.export_page_button.set_enabled(current_used and not busy)
        self.clear_button.set_enabled(
            bool(self.photos or self.custom_pages) and not busy
        )

    def _start_export(
        self,
        targets: list[tuple[int, list[Photo | None], Path]],
        gap_units: int,
        margin_units: int,
        auto_rotate: bool,
    ) -> None:
        total = len(targets)
        self._exporting = True
        self._export_total = total
        self._export_done = 0
        self._exported_paths = []
        self._export_error = None
        self._export_queue = queue.Queue()
        self.add_button.set_enabled(False)
        self._update_export_buttons()
        self._show_progress(f"正在导出 0 / {total} 页")
        self._set_status("正在从原文件按全分辨率导出，界面可以继续使用…")
        self._export_thread = threading.Thread(
            target=self._export_worker,
            args=(
                targets,
                gap_units,
                margin_units,
                auto_rotate,
                self._export_queue,
            ),
            name="photo-export",
            daemon=True,
        )
        self._export_thread.start()
        self._export_poll_job = self.root.after(60, self._poll_export)

    @staticmethod
    def _export_worker(
        targets: list[tuple[int, list[Photo | None], Path]],
        gap_units: int,
        margin_units: int,
        auto_rotate: bool,
        out: queue.Queue,
    ) -> None:
        try:
            for mode, photos, path in targets:
                try:
                    image = compose_page(
                        photos,
                        mode,
                        gap_units,
                        margin_units,
                        auto_rotate=auto_rotate,
                        full=True,
                    )
                    saved = PhotoSplicerApp._save_image(image, path)
                    image.close()
                    out.put(("page", saved))
                finally:
                    for photo in photos:
                        if photo is not None:
                            photo.release_full_image()
        except Exception as exc:  # noqa: BLE001
            out.put(("error", str(exc)))
        finally:
            out.put(("done",))

    def _poll_export(self) -> None:
        self._export_poll_job = None
        pending = self._export_queue
        if pending is None:
            return
        done = False
        while True:
            try:
                item = pending.get_nowait()
            except queue.Empty:
                break
            if item[0] == "page":
                self._export_done += 1
                self._exported_paths.append(item[1])
            elif item[0] == "error":
                self._export_error = item[1]
            elif item[0] == "done":
                done = True
                break
        self._update_progress(
            self._export_done,
            self._export_total,
            f"正在导出 {self._export_done} / {self._export_total} 页",
        )
        if done:
            self._finish_export()
        else:
            self._export_poll_job = self.root.after(50, self._poll_export)

    def _finish_export(self) -> None:
        self._exporting = False
        self._hide_progress()
        self.add_button.set_enabled(True)
        self._update_export_buttons()
        exported = self._exported_paths
        if self._export_error:
            self._set_status(f"导出失败：{self._export_error}")
            messagebox.showerror(
                "导出失败", self._export_error, parent=self.root
            )
            return
        if not exported:
            self._set_status("没有导出任何页面。")
            return
        if self._export_single:
            path = exported[0]
            self._set_status(f"已导出：{path}")
            messagebox.showinfo(
                "导出完成", f"照片已保存到：\n{path}", parent=self.root
            )
            return
        folder = self._export_folder
        self._set_status(f"已导出 {len(exported)} 页到 {folder}")
        messagebox.showinfo(
            "导出完成",
            f"已导出 {len(exported)} 页到：\n{folder}\n\n"
            f"{exported[0].name} … {exported[-1].name}",
            parent=self.root,
        )

    @staticmethod
    def _save_image(image: Image.Image, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            image.save(
                path,
                format="JPEG",
                quality=JPEG_QUALITY,
                subsampling=0,
                dpi=(OUTPUT_DPI, OUTPUT_DPI),
            )
            return path
        if suffix == ".png":
            image.save(path, format="PNG", dpi=(OUTPUT_DPI, OUTPUT_DPI))
            return path
        # 没写扩展名时默认 JPEG。
        target = path.with_suffix(".jpg")
        image.save(
            target,
            format="JPEG",
            quality=JPEG_QUALITY,
            subsampling=0,
            dpi=(OUTPUT_DPI, OUTPUT_DPI),
        )
        return target


def main() -> None:
    root = None
    if OS_FILE_DND:
        try:
            root = TkinterDnD.Tk()
        except Exception:  # noqa: BLE001 - 回退到普通 Tk
            root = None
    if root is None:
        root = tk.Tk()
    app = PhotoSplicerApp(root)
    root.bind("<Control-o>", lambda _event: app.add_photos())
    root.bind("<Control-e>", lambda _event: app.export_all())
    root.mainloop()


if __name__ == "__main__":
    main()
