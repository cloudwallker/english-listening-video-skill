#!/usr/bin/env python3
"""Warm, readable 2D story animation rendered from measured audio timings.

AI artwork is optional. The built-in scene is an original vector drawing with
independent rain, steam, book-page, character, and outdoor motion. Caption text
stays still for the entire sentence; animation never enters the caption panel.
Only compositing, scaling, and moving supplied artwork is performed here.
"""

import bisect
import math
import os
from pathlib import Path
import random
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont


PAPER = "#f8f2e7"
INK = "#382c28"
MUTED = "#8e7c6c"
BLUE = "#688b9b"
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


def _validate_entries(entries):
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ValueError("动画时间轴必须包含至少一个片段。")
    cursor = 0.0
    for item in entries:
        try:
            start, end = float(item["start"]), float(item["end"])
            index = item["sentence_index"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("动画时间轴缺少句子索引或起止时间。") from exc
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("动画时间轴的起止时间无效。")
        if abs(start - cursor) > 0.00002:
            raise ValueError("动画时间轴必须从零开始并连续，不能有空隙或重叠。")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("动画时间轴的句子索引无效。")
        cursor = end
    return cursor


def frame_count(entries, fps=24):
    """Cover the final measured segment with whole output frames."""
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 120:
        raise ValueError("fps 必须在 1 到 120 之间。")
    frame_position = _validate_entries(entries) * fps
    nearest_frame = round(frame_position)
    # Timelines store microseconds: a frame-aligned PCM duration may round a
    # few millionths above the integer, which must not create an extra frame.
    if abs(frame_position - nearest_frame) <= 0.0001:
        return int(nearest_frame)
    return int(math.ceil(frame_position))


def sentence_at(entries, time):
    """Return the active sentence; each boundary belongs to the next segment."""
    end = _validate_entries(entries)
    if not isinstance(time, (int, float)) or not math.isfinite(time) or time < 0 or time > end + 0.000001:
        raise ValueError("预览时间必须位于动画时间轴内。")
    starts = [float(item["start"]) for item in entries]
    selected = min(len(entries) - 1, bisect.bisect_right(starts, time) - 1)
    return entries[selected]["sentence_index"]


def _fonts(fonts=None):
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = {
        "en": [font_dir / "segoeui.ttf", font_dir / "arial.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")],
        "ipa": [font_dir / "segoeui.ttf", font_dir / "arial.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")],
        "zh": [font_dir / "msyh.ttc", font_dir / "msyh.ttf", Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")],
    }
    result = {}
    for role, paths in candidates.items():
        if fonts and fonts.get(role):
            paths = [Path(fonts[role])]
        found = next((path for path in paths if path.is_file()), None)
        if found is None:
            raise ValueError("找不到 %s 字体，请指定 fonts 字典。" % role)
        result[role] = str(found)
    return result


def _wrap(draw, text, font, width, role):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("EN、ZH、IPA 字幕必须完整。")
    tokens = list(text) if role == "zh" else text.split()
    lines, current = [], ""
    for token in tokens:
        proposal = current + ("" if role == "zh" or not current else " ") + token
        if draw.textlength(proposal, font=font) <= width:
            current = proposal
        else:
            if current:
                lines.append(current)
            current = token
            if draw.textlength(current, font=font) > width:
                raise ValueError("字幕包含过长单词，请拆分句子。")
    if current:
        lines.append(current)
    return lines


def _fit_caption(draw, sentence, fonts, width, height, scale, portrait):
    sizes = {"en": 76 if portrait else 72, "ipa": 40 if portrait else 37, "zh": 48 if portrait else 43}
    minimum = {"en": 66 if portrait else 64, "ipa": 35 if portrait else 32, "zh": 42 if portrait else 38}
    # Fit only within a small readable range. Never shrink an overfull paragraph
    # into microscopic text, or silently drop any line.
    for decrement in range(11):
        blocks = {}
        for role in ("en", "ipa", "zh"):
            size = max(minimum[role], sizes[role] - decrement) * scale
            font = ImageFont.truetype(fonts[role], max(1, round(size)))
            lines = _wrap(draw, sentence.get(role), font, width, role)
            line_height = round(size * (1.50 if role == "ipa" else 1.34))
            blocks[role] = (font, lines, line_height)
        total = sum(len(lines) * line_height for font, lines, line_height in blocks.values()) + round(118 * scale)
        if total <= height:
            return blocks
    raise ValueError("字幕过长，无法以可读字号完整排版；请拆分句子后再生成。")


def _draw_lines(draw, block, x, y, color):
    font, lines, line_height = block
    for line in lines:
        draw.text((x, y), line, font=font, fill=color, anchor="lt")
        y += line_height
    return y


def _fit_image(image, size, cover=True):
    factor = max(size[0] / image.width, size[1] / image.height) if cover else min(size[0] / image.width, size[1] / image.height)
    resized = image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))), RESAMPLE)
    if cover:
        x, y = (resized.width - size[0]) // 2, (resized.height - size[1]) // 2
        return resized.crop((x, y, x + size[0], y + size[1]))
    return resized


def _vector_room(width, height):
    """Draw original paper-cut scenery, not third-party stock graphics."""
    canvas = Image.new("RGB", (width, height), "#e7ddc8")
    d = ImageDraw.Draw(canvas)
    sx, sy = width / 1000, height / 812
    def box(v): return tuple(round(c * (sx if i % 2 == 0 else sy)) for i, c in enumerate(v))
    def line(v, color, thick=3): d.line(box(v), fill=color, width=max(1, round(thick * sx)))
    def rectangle(v, color, radius=0, outline=None, thick=2):
        d.rounded_rectangle(box(v), radius=max(0, round(radius * sx)), fill=color, outline=outline, width=max(1, round(thick * sx)))
    def ellipse(v, color): d.ellipse(box(v), fill=color)
    # A softly lit wall and rainy pale blue window.
    rectangle((54, 50, 468, 530), "#a48665", 20)
    rectangle((69, 65, 453, 511), "#bacbd0", 9)
    rectangle((86, 82, 437, 492), "#c7d5d6", 2)
    d.polygon([box((87, 492)), box((87, 423)), box((164, 355)), box((229, 419)), box((316, 331)), box((437, 431)), box((437, 492))], fill="#a3b3ae")
    # Distant rooftops and gentle cloud strips are anchored behind the rain.
    rectangle((88, 111, 432, 119), "#dbe2db", 4)
    rectangle((92, 143, 310, 148), "#dbe2db", 2)
    rectangle((250, 77, 262, 512), "#a48665")
    rectangle((72, 292, 450, 305), "#a48665")
    rectangle((38, 514, 486, 544), "#9b7858", 5)
    # Bookshelf: varied bindings and lightly imperfect edges.
    rectangle((550, 60, 968, 628), "#bb9875", 12)
    rectangle((572, 80, 948, 606), "#cfb294", 5)
    book_colors = ["#a46753", "#74909b", "#9caa8d", "#e0bd81", "#7f7069", "#b49692"]
    rng = random.Random(21)
    for shelf_y in (217, 393, 572):
        x = 586
        while x < 929:
            bw = rng.randint(26, 49)
            bh = rng.randint(95, 129)
            color = book_colors[rng.randrange(len(book_colors))]
            rectangle((x, shelf_y - bh, min(933, x + bw - 4), shelf_y), color, 3)
            line((x + 7, shelf_y - bh + 17, x + bw - 11, shelf_y - bh + 17), "#ead9bb", 2)
            line((x + 7, shelf_y - 18, x + bw - 11, shelf_y - 18), "#ead9bb", 2)
            x += bw
        rectangle((567, shelf_y, 952, shelf_y + 17), "#a98462", 3)
    rectangle((0, 674, 1000, 812), "#c9b195")
    line((0, 713, 1000, 713), "#b49b7f", 2)
    line((0, 775, 1000, 775), "#b49b7f", 2)
    for x in (140, 390, 720): line((x, 674, x + 90, 812), "#bda387", 2)
    # Plant near the window: leaves are stable, steam provides nearby motion.
    rectangle((88, 649, 197, 758), "#bd8165", 13)
    rectangle((83, 640, 202, 661), "#cf9678", 7)
    for x, y, tilt in [(143, 558, 0), (116, 578, -1), (174, 587, 1), (142, 614, 0)]:
        line((141, 648, x, y + 18), "#74876b", 5)
        ellipse((x - 28, y - 18, x + 23, y + 25), "#879b7c")
    ellipse((325, 741, 809, 799), "#b79a7c")
    return canvas


def _vector_fox(include_chair=True):
    """Original fox in a reading chair; the book is a separate moving layer."""
    image = Image.new("RGBA", (600, 650), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    # Tail, chair, slippers, body.
    d.ellipse((9, 384, 210, 600), fill="#c77648")
    d.ellipse((12, 458, 113, 603), fill="#f1d9b5")
    if include_chair:
        d.rounded_rectangle((180, 296, 564, 581), radius=95, fill="#a78984")
        d.rounded_rectangle((187, 472, 554, 590), radius=48, fill="#8f7271")
        d.rounded_rectangle((222, 574, 245, 644), radius=8, fill="#856848")
        d.rounded_rectangle((500, 574, 523, 644), radius=8, fill="#856848")
    d.ellipse((217, 551, 334, 609), fill="#6d7976")
    d.ellipse((392, 547, 511, 605), fill="#6d7976")
    d.ellipse((227, 287, 493, 547), fill="#c27b4c")
    d.ellipse((280, 331, 438, 534), fill="#f1d9b5")
    # Fox ears and soft muzzle.
    d.polygon([(226, 177), (205, 23), (320, 114)], fill="#c77648")
    d.polygon([(425, 146), (521, 38), (501, 235)], fill="#c77648")
    d.polygon([(233, 131), (225, 53), (285, 113)], fill="#6d534b")
    d.polygon([(452, 137), (499, 71), (486, 183)], fill="#6d534b")
    d.ellipse((198, 102, 515, 331), fill="#d18752")
    d.polygon([(200, 219), (256, 209), (346, 274), (435, 211), (515, 224), (464, 308), (349, 355), (243, 297)], fill="#f4e0be")
    d.ellipse((329, 270, 365, 290), fill="#41332b")
    d.arc((262, 193, 301, 225), 0, 170, fill="#49392f", width=6)
    d.arc((405, 195, 444, 227), 0, 170, fill="#49392f", width=6)
    d.arc((334, 288, 365, 310), 10, 165, fill="#775847", width=3)
    d.ellipse((271, 252, 305, 269), fill="#d69c74")
    d.ellipse((400, 251, 434, 268), fill="#d69c74")
    # Arms cradle the book; small vertical bob gives breathing movement.
    d.ellipse((206, 383, 304, 489), fill="#c77648")
    d.ellipse((420, 379, 515, 484), fill="#c77648")
    return image


def _book_layer(width, height, time):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    sx, sy = width / 270, height / 155
    def p(points): return [(round(x * sx), round(y * sy)) for x, y in points]
    d.polygon(p([(8, 30), (125, 9), (137, 30), (254, 13), (263, 129), (147, 150), (133, 137), (19, 151)]), fill="#698390")
    d.polygon(p([(14, 21), (122, 9), (135, 29), (130, 128), (22, 142)]), fill="#efe4c9")
    d.polygon(p([(135, 29), (253, 6), (250, 121), (139, 139)]), fill="#fbf3dc")
    for y in (49, 64, 79, 94, 109):
        d.line(p([(33, y), (114, y - 8)]), fill="#bfb39a", width=max(1, round(2 * sx)))
        d.line(p([(154, y - 6), (231, y - 22)]), fill="#c8bba0", width=max(1, round(2 * sx)))
    # A real page rotates around the spine, rather than translating the scene.
    phase = (time % 7.5) / 7.5
    if .64 < phase < .92:
        progress = (phase - .64) / .28
        tip = 137 + math.cos(progress * math.pi) * 102
        rise = math.sin(progress * math.pi) * 31
        d.polygon(p([(135, 29), (tip, 3 - rise), (tip + 4, 116 - rise), (138, 139)]), fill="#fff8e3", outline="#d4c6a7")
    return image


def _thin_page_layer(width, height, time):
    """Only the thin moving leaf; supplied fox artwork already contains a book."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    phase = (time % 7.5) / 7.5
    if .64 < phase < .92:
        progress = (phase - .64) / .28
        tip = .50 + math.cos(progress * math.pi) * .45
        rise = math.sin(progress * math.pi) * .13
        points = [(.50, .25), (tip, .05 - rise), (tip + .03, .68 - rise), (.50, .90)]
        d = ImageDraw.Draw(image)
        d.polygon([(round(x * width), round(y * height)) for x, y in points], fill=(251, 238, 205, 210), outline=(205, 181, 143, 160))
    return image


class AnimationScene:
    """Prepared reusable scene. Call render_frame for many timestamps cheaply."""

    def __init__(self, lesson, entries, fonts=None, assets_dir=None):
        self.lesson, self.entries = lesson, entries
        self.total_duration = _validate_entries(entries)
        self.starts = [float(item["start"]) for item in entries]
        self.width, self.height = int(lesson.get("width", 1920)), int(lesson.get("height", 1080))
        if self.width < 320 or self.height < 320 or self.width % 2 or self.height % 2:
            raise ValueError("动画宽高需要至少 320 像素的偶数。")
        self.portrait = self.height > self.width
        if not (self.width * 9 == self.height * 16 or self.width * 16 == self.height * 9):
            raise ValueError("动画版式需要精确的横屏 16:9 或竖屏 9:16。")
        self.scale = self.width / 1080 if self.portrait else self.width / 1920
        self.fonts = _fonts(fonts)
        self.assets_dir = Path(assets_dir) if assets_dir is not None else Path(__file__).resolve().parents[1] / "assets"
        self.base = Image.new("RGB", (self.width, self.height), PAPER)
        self._prepare_paper()
        s = self.scale
        if self.portrait:
            self.scene_box = tuple(round(v * s) for v in (76, 235, 1004, 1125))
            self.caption_box = tuple(round(v * s) for v in (76, 1218, 1004, 1813))
        else:
            self.scene_box = tuple(round(v * s) for v in (72, 180, 1070, 992))
            self.caption_box = tuple(round(v * s) for v in (1150, 229, 1830, 928))
        x0, y0, x1, y1 = self.scene_box
        self.scene_size = x1 - x0, y1 - y0
        self.scene_background = _vector_room(*self.scene_size)
        background_path = self.assets_dir / "bookshop-background.png"
        self.has_art_background = background_path.is_file()
        self.art_transform = (self.scene_size[0], self.scene_size[1], 0, 0)
        if background_path.is_file():
            with Image.open(background_path) as supplied:
                # Keep the existing right-hand chair and cup in both layouts.
                # This is a normal layer crop/scale, not content editing.
                factor = max(self.scene_size[0] / supplied.width, self.scene_size[1] / supplied.height)
                art_w, art_h = round(supplied.width * factor), round(supplied.height * factor)
                left, top = art_w - self.scene_size[0], (art_h - self.scene_size[1]) // 2
                self.scene_background = supplied.convert("RGB").resize((art_w, art_h), RESAMPLE).crop((left, top, left + self.scene_size[0], top + self.scene_size[1]))
                self.art_transform = (art_w, art_h, left, top)
        self.fox = _vector_fox(include_chair=not self.has_art_background)
        fox_path = self.assets_dir / "reading-fox.png"
        self.has_art_fox = fox_path.is_file()
        if fox_path.is_file():
            with Image.open(fox_path) as supplied:
                self.fox = supplied.convert("RGBA")
        if self.has_art_background:
            fox_h = round(self.art_transform[1] * .65)
            self.fox = _fit_image(self.fox, (round(fox_h * .8), fox_h), cover=False)
            chair_x, chair_y = self._art_point(.786, .701)
            self.fox_origin = (round(chair_x - self.fox.width * .59), round(chair_y - self.fox.height * .647))
        else:
            self.fox = _fit_image(self.fox, (round(self.scene_size[0] * .53), round(self.scene_size[1] * .69)), cover=False)
            self.fox_origin = (round(self.scene_size[0] * .34), round(self.scene_size[1] * .27))
        self.scene_mask = Image.new("L", self.scene_size, 0)
        ImageDraw.Draw(self.scene_mask).rounded_rectangle((0, 0, self.scene_size[0] - 1, self.scene_size[1] - 1), radius=max(6, round(24 * s)), fill=255)
        # Caption blocks are pre-rendered once and stay perfectly still.
        self.captions = {}
        sentences = lesson.get("sentences", [])
        for item in entries:
            index = item["sentence_index"]
            if index >= len(sentences):
                raise ValueError("动画时间轴引用了不存在的句子。")
            if index not in self.captions:
                self.captions[index] = self._caption_image(index)

    def _art_point(self, x, y):
        width, height, left, top = self.art_transform
        return round(x * width - left), round(y * height - top)

    def _prepare_paper(self):
        d, s = ImageDraw.Draw(self.base), self.scale
        rng = random.Random(10)
        for _ in range(1400):
            x, y = rng.randrange(self.width), rng.randrange(self.height)
            d.point((x, y), fill="#e9dfd0")
        def font(role, size): return ImageFont.truetype(self.fonts[role], max(1, round(size * s)))
        if self.portrait:
            d.text((76 * s, 69 * s), "SLOW ENGLISH", font=font("en", 22), fill=BLUE, anchor="lt")
            d.text((76 * s, 116 * s), self.lesson.get("title_en", "A Little Story"), font=font("en", 47), fill=INK, anchor="lt")
            d.text((76 * s, 180 * s), self.lesson.get("title", "慢速英语"), font=font("zh", 27), fill=MUTED, anchor="lt")
            if self.lesson.get("actual_wpm_with_native_pauses") is not None:
                label = "约 %.0f WPM（含自然停顿）" % self.lesson["actual_wpm_with_native_pauses"]
                d.text((1004 * s, 181 * s), label, font=font("zh", 23), fill=MUTED, anchor="rt")
            d.text((76 * s, 1167 * s), "LISTEN TO THE STORY", font=font("en", 21), fill=BLUE, anchor="lt")
            d.line((76 * s, 1852 * s, 1004 * s, 1852 * s), fill="#d9cdb9", width=max(1, round(s)))
            d.text((76 * s, 1874 * s), "慢一点，听懂一个完整的故事。", font=font("zh", 23), fill=MUTED, anchor="lt")
        else:
            d.text((72 * s, 50 * s), "SLOW ENGLISH / A LITTLE STORY", font=font("en", 21), fill=BLUE, anchor="lt")
            d.text((72 * s, 91 * s), self.lesson.get("title_en", "A Little Story"), font=font("en", 49), fill=INK, anchor="lt")
            d.text((1150 * s, 58 * s), self.lesson.get("title", "慢速英语"), font=font("zh", 32), fill=INK, anchor="lt")
            if self.lesson.get("actual_wpm_with_native_pauses") is not None:
                subtitle = "%s · 约 %.0f WPM（含自然停顿）" % (self.lesson.get("level", "A1-A2"), self.lesson["actual_wpm_with_native_pauses"])
                subtitle_font = font("zh", 23)
            else:
                subtitle = "%s  ·  %s WPM" % (self.lesson.get("level", "A1-A2"), int(self.lesson.get("target_wpm", 100)))
                subtitle_font = font("en", 23)
            d.text((1150 * s, 108 * s), subtitle, font=subtitle_font, fill=MUTED, anchor="lt")
            d.text((1150 * s, 182 * s), "LISTEN TO THE STORY", font=font("en", 21), fill=BLUE, anchor="lt")
            d.line((1150 * s, 966 * s, 1830 * s, 966 * s), fill="#d9cdb9", width=max(1, round(s)))
            d.text((1150 * s, 995 * s), "慢一点，听懂一个完整的故事。", font=font("zh", 24), fill=MUTED, anchor="lt")

    def _caption_image(self, index):
        x0, y0, x1, y1 = self.caption_box
        width, height, s = x1 - x0, y1 - y0, self.scale
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        d = ImageDraw.Draw(image)
        blocks = _fit_caption(d, self.lesson["sentences"][index], self.fonts, width, height, s, self.portrait)
        y = _draw_lines(d, blocks["en"], 0, 0, INK)
        y += round(28 * s)
        d.text((0, y), "IPA / 美式音标" if self.lesson.get("accent") == "en-US" else "IPA / 英式音标", font=ImageFont.truetype(self.fonts["zh"], max(1, round(19 * s))), fill=BLUE, anchor="lt")
        y += round(34 * s)
        y = _draw_lines(d, blocks["ipa"], 0, y, "#7c7e71")
        y += round(31 * s)
        _draw_lines(d, blocks["zh"], 0, y, "#8b6753")
        return image

    def _scene_frame(self, time):
        scene = self.scene_background.copy().convert("RGBA")
        width, height = self.scene_size
        sx, sy = width / 1000, height / 812
        d = ImageDraw.Draw(scene)
        # Rain is clipped to the window pane. It moves at independent speeds,
        # so an otherwise still bookshop stays gently alive for long listening.
        rain = Image.new("RGBA", self.scene_size, (0, 0, 0, 0))
        rd = ImageDraw.Draw(rain)
        rain_mask = Image.new("L", self.scene_size, 0)
        mask_draw = ImageDraw.Draw(rain_mask)
        if self.has_art_background:
            for xa, ya, xb, yb in ((0, 0, .060, .522), (.081, 0, .220, .510), (.242, .041, .339, .498)):
                mask_draw.rectangle((*self._art_point(xa, ya), *self._art_point(xb, yb)), fill=255)
            for i in range(55):
                px = .012 + (i * .079 % .322)
                py = .012 + ((i * .097 + time * (.11 + i % 5 * .012)) % .520)
                rd.line((*self._art_point(px, py), *self._art_point(px - .004, py + .017)), fill=(232, 241, 235, 108), width=max(1, round(1.6 * sx)))
        else:
            mask_draw.rectangle((round(86 * sx), round(83 * sy), round(437 * sx), round(492 * sy)), fill=255)
            for i in range(45):
                px = 96 + (i * 79 % 333)
                py = 86 + ((i * 97 + time * (105 + i % 5 * 16)) % 397)
                rd.line((round(px * sx), round(py * sy), round((px - 7) * sx), round((py + 18) * sy)), fill=(240, 246, 239, 120), width=max(1, round(2 * sx)))
        rain.putalpha(Image.composite(rain.getchannel("A"), Image.new("L", self.scene_size, 0), rain_mask))
        scene.alpha_composite(rain)
        # Tiny pedestrian silhouettes pass behind the window glass.
        outdoor = Image.new("RGBA", self.scene_size, (0, 0, 0, 0))
        od = ImageDraw.Draw(outdoor)
        for j in range(2):
            px = 93 + ((time * 14 + j * 210) % 346)
            py = 450 + j * 11
            od.ellipse((round((px - 6) * sx), round((py - 19) * sy), round((px + 6) * sx), round((py - 7) * sy)), fill=(83, 109, 109, 110))
            od.line((round(px * sx), round((py - 5) * sy), round(px * sx), round((py + 16) * sy)), fill=(83, 109, 109, 110), width=max(2, round(10 * sx)))
            stride = math.sin(time * 3 + j) * 5
            for sign in (-1, 1):
                od.line((round(px * sx), round((py + 13) * sy), round((px + sign * stride) * sx), round((py + 25) * sy)), fill=(83, 109, 109, 110), width=max(1, round(3 * sx)))
        outdoor.putalpha(Image.composite(outdoor.getchannel("A"), Image.new("L", self.scene_size, 0), rain_mask))
        scene.alpha_composite(outdoor)
        # Fox breathing has its own phase, rather than a global camera move.
        fox_x = round(self.fox_origin[0] + math.sin(time * .37) * 1.8 * sx)
        fox_y = round(self.fox_origin[1] + math.sin(time * 1.25) * 2.2 * sy)
        scene.alpha_composite(self.fox, (fox_x, fox_y))
        if self.has_art_fox:
            page_size = max(16, round(self.fox.width * .28)), max(16, round(self.fox.height * .105))
            page = _thin_page_layer(*page_size, time)
            scene.alpha_composite(page, (fox_x + round(self.fox.width * .625), fox_y + round(self.fox.height * .425)))
        else:
            book_size = max(50, round(width * .225)), max(30, round(height * .145))
            book = _book_layer(*book_size, time)
            scene.alpha_composite(book, (round(width * .545), round(height * .667 + math.sin(time * 1.25) * 2 * sy)))
        # Foreground table, cup, and changing steam strokes.
        d = ImageDraw.Draw(scene)
        if not self.has_art_background:
            d.ellipse((round(777 * sx), round(635 * sy), round(1017 * sx), round(705 * sy)), fill="#aa8059")
            d.rounded_rectangle((round(828 * sx), round(690 * sy), round(847 * sx), round(812 * sy)), radius=max(1, round(4 * sx)), fill="#9a734f")
            d.ellipse((round(797 * sx), round(622 * sy), round(913 * sx), round(653 * sy)), fill="#e3d2b5")
            d.arc((round(869 * sx), round(569 * sy), round(915 * sx), round(616 * sy)), -100, 95, fill="#6f8790", width=max(2, round(8 * sx)))
            d.rounded_rectangle((round(811 * sx), round(567 * sy), round(882 * sx), round(630 * sy)), radius=max(1, round(15 * sx)), fill="#7e98a0")
            d.ellipse((round(811 * sx), round(562 * sy), round(882 * sx), round(585 * sy)), fill="#adc0bf")
            d.ellipse((round(817 * sx), round(566 * sy), round(876 * sx), round(580 * sy)), fill="#94704c")
        steam = Image.new("RGBA", self.scene_size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(steam)
        for i in range(3):
            points = []
            for k in range(16):
                if self.has_art_background:
                    py = .654 - k * .004 - ((time * .008 + i * .006) % .014)
                    px = .911 + i * .009 + math.sin(k * .38 + time * 1.6 + i * .8) * .004
                    points.append(self._art_point(px, py))
                else:
                    py = 551 - k * 4 - ((time * 10 + i * 8) % 18)
                    px = 834 + i * 13 + math.sin(k * .38 + time * 1.6 + i * .8) * 5
                    points.append((round(px * sx), round(py * sy)))
            sd.line(points, fill=(250, 246, 228, 126), width=max(1, round(3 * sx)))
        scene.alpha_composite(steam)
        return scene.convert("RGB")

    def render_frame(self, time):
        if not isinstance(time, (int, float)) or not math.isfinite(time) or time < 0 or time > self.total_duration + .000001:
            raise ValueError("预览时间必须位于动画时间轴内。")
        position = min(len(self.entries) - 1, bisect.bisect_right(self.starts, time) - 1)
        index = self.entries[position]["sentence_index"]
        frame = self.base.copy()
        frame.paste(self._scene_frame(time), self.scene_box[:2], self.scene_mask)
        caption = self.captions[index]
        frame.paste(caption, self.caption_box[:2], caption)
        # The tiny progress mark stays outside both artwork and caption panels.
        d, s = ImageDraw.Draw(frame), self.scale
        if self.portrait:
            count_x, count_y = 877 * s, 1167 * s
        else:
            count_x, count_y = 1709 * s, 182 * s
        count = "%02d / %02d" % (index + 1, len(self.lesson["sentences"]))
        d.text((count_x, count_y), count, font=ImageFont.truetype(self.fonts["en"], max(1, round(21 * s))), fill=MUTED, anchor="lt")
        return frame


def frame_at(lesson, entries, time, fonts=None, assets_dir=None):
    """Render one preview frame; reuse AnimationScene for video or contact sheets."""
    return AnimationScene(lesson, entries, fonts=fonts, assets_dir=assets_dir).render_frame(time)


def render_animated_video(lesson, entries, output_path, ffmpeg, fps=24, fonts=None, assets_dir=None, preset="fast", threads=4):
    """Render silent H.264/yuv420p animation; the caller muxes measured audio."""
    count = frame_count(entries, fps)
    scene = AnimationScene(lesson, entries, fonts=fonts, assets_dir=assets_dir)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"stdin": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    # A temporary output prevents publishing a partial video after a pipe or
    # encoder error. No shell commands or user text are interpreted by a shell.
    with tempfile.TemporaryDirectory(prefix="animation-", dir=str(output_path.parent)) as work:
        temporary = Path(work) / "silent.mp4"
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "%dx%d" % (scene.width, scene.height), "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", preset, "-crf", "20", "-pix_fmt", "yuv420p", "-threads", str(threads), "-frames:v", str(count), "-movflags", "+faststart", str(temporary)]
        with tempfile.TemporaryFile(dir=str(output_path.parent)) as errors:
            process = subprocess.Popen(command, stderr=errors, **kwargs)
            try:
                for number in range(count):
                    process.stdin.write(scene.render_frame(number / fps).tobytes())
                process.stdin.close()
                returncode = process.wait(timeout=max(120, int(scene.total_duration * 15)))
            except BaseException:
                process.kill()
                process.wait()
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace")[-1800:]
                if detail:
                    raise ValueError("动画编码失败：" + detail)
                raise
            if returncode:
                errors.seek(0)
                raise ValueError("动画编码失败：" + errors.read().decode("utf-8", errors="replace")[-1800:])
        os.replace(str(temporary), str(output_path))
    return {"frame_count": count, "duration": count / fps, "fps": fps, "width": scene.width, "height": scene.height, "animation": "rain, outdoor walking, fox breathing, page turns, tea steam", "assets": "optional AI layers with original vector fallback"}
