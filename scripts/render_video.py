#!/usr/bin/env python3
"""Render a continuous slow English story with Chinese and IPA captions.

Only English ``en`` or explicit ``spoken_text`` goes to speech synthesis.
Edge TTS is online; choose ``--speech-mode calibrated --tts sapi`` for local Windows speech. Timings use measured audio,
never character-count estimates. Run ``--validate-only`` before synthesis.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from contextlib import contextmanager
from pathlib import Path

LOCAL_RUNTIME = Path(__file__).resolve().parents[1] / ".runtime"
if LOCAL_RUNTIME.is_dir():
    sys.path.insert(0, str(LOCAL_RUNTIME))

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


class RenderError(Exception):
    """An actionable validation or rendering failure."""


OUTPUT_FILES = ("video.mp4", "cover.png", "preview.png", "captions.srt", "timeline.json", "lesson.json", "narration.wav", "narration.mp3", "bundle.json")
SAMPLE_RATE = 48000
PALETTE = {"bg": "#251d36", "paper": "#f6efe4", "ink": "#2c223d", "muted": "#766d81", "accent": "#c4a6ed", "line": "#ded3e4", "white": "#fcf8f2"}


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise RenderError(f"{name} 必须是非空字符串。")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise RenderError(f"{name} 含有不支持的控制字符。")
    return " ".join(value.split())


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RenderError(f"{name} 必须是数值。")
    if not minimum <= value <= maximum:
        raise RenderError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def load_lesson(source, args):
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"无法读取 lesson JSON：{type(exc).__name__}。") from exc
    if not isinstance(raw, dict):
        raise RenderError("lesson 顶层必须是 JSON 对象。")
    lesson = dict(raw)
    for key in ("title", "title_en", "level"):
        lesson[key] = nonempty(raw.get(key), key)
    lesson["accent"] = nonempty(raw.get("accent", "en-US"), "accent")
    if not re.fullmatch(r"en-[A-Z]{2}", lesson["accent"]):
        raise RenderError("accent 必须是英语地区代码，例如 en-US。")
    if raw.get("voice") is not None:
        lesson["voice"] = nonempty(raw["voice"], "voice")
    for key, default in (("width", 1080), ("height", 1920)):
        override = getattr(args, key, None)
        value = number(override if override is not None else raw.get(key, default), key, 540, 3840)
        if int(value) != value or int(value) % 2:
            raise RenderError(f"{key} 必须是偶数像素。")
        lesson[key] = int(value)
    style = getattr(args, "style", "card")
    ratio = lesson["height"] / lesson["width"]
    if not getattr(args, "audio_only", False) and style == "card" and not 1.35 <= ratio <= 2.0:
        raise RenderError("当前卡片版式需要竖屏，height/width 应在 1.35 到 2.0 之间。")
    if not getattr(args, "audio_only", False) and style == "paper-animation" and not (lesson["width"] * 9 == lesson["height"] * 16 or lesson["height"] * 9 == lesson["width"] * 16):
        raise RenderError("纸面动画需精确 16:9 横屏或 9:16 竖屏，例如1920×1080或1080×1920，以免裁切字幕。")
    natural = getattr(args, "speech_mode", "calibrated") == "natural" or bool(getattr(args, "approved_audio", None))
    if natural and args.repeats is not None and args.repeats != 1:
        raise RenderError("自然整轨与已批准音频仅支持单遍；--repeats 必须为 1。重复训练请另用 calibrated 模式。")
    if natural and (getattr(args, "target_wpm", None) is not None or getattr(args, "pause", None) is not None):
        raise RenderError("自然整轨不强制目标词速、不追加停顿；--target-wpm 和 --pause 仅用于 --speech-mode calibrated。")
    repeats = args.repeats if args.repeats is not None else (1 if natural else raw.get("repeats", 2))
    if int(number(repeats, "repeats", 1, 5)) != repeats:
        raise RenderError("repeats 必须是整数。")
    lesson["repeats"] = int(repeats)
    target = args.target_wpm if args.target_wpm is not None else raw.get("target_wpm", 100)
    lesson["target_wpm"] = float(number(target, "target_wpm", 60, 160))
    edge_rate = getattr(args, "edge_rate", None) or raw.get("edge_rate")
    if edge_rate is None:
        edge_rate = "-20%" if natural else f"{max(-75, min(0, round((lesson['target_wpm'] / 240 - 1) * 100))):+d}%"
    if not isinstance(edge_rate, str) or not re.fullmatch(r"[+-]?\d+%", edge_rate):
        raise RenderError("edge_rate 必须是百分比字符串，例如 -60%。")
    number(int(edge_rate[:-1]), "edge_rate", -85, 40)
    lesson["edge_rate"] = edge_rate
    sapi_rate = getattr(args, "sapi_rate", None)
    if sapi_rate is None:
        sapi_rate = raw.get("sapi_rate", max(-9, min(0, round((lesson["target_wpm"] - 180) / 15))))
    if int(number(sapi_rate, "sapi_rate", -10, 10)) != sapi_rate:
        raise RenderError("sapi_rate 必须是整数。")
    lesson["sapi_rate"] = int(sapi_rate)
    gap = args.pause if args.pause is not None else raw.get("pause", 0.6)
    lesson["pause"] = float(number(gap, "pause", 0.45, 0.7))
    sentences = raw.get("sentences")
    if not isinstance(sentences, list) or not sentences or len(sentences) > 100:
        raise RenderError("sentences 必须包含 1 到 100 个句子。")
    checked = []
    for index, sentence in enumerate(sentences):
        name = f"sentences[{index}]"
        if not isinstance(sentence, dict):
            raise RenderError(f"{name} 必须是对象。")
        item = {key: nonempty(sentence.get(key), f"{name}.{key}") for key in ("en", "zh", "ipa")}
        if "spoken_text" in sentence:
            item["spoken_text"] = nonempty(sentence["spoken_text"], f"{name}.spoken_text")
            if re.search(r"[\u3400-\u9fff]", item["spoken_text"]) or not re.search(r"[A-Za-z]", item["spoken_text"]):
                raise RenderError(f"{name}.spoken_text 必须是实际朗读的英文。")
        if re.search(r"[\u3400-\u9fff]", item["en"]) or not re.search(r"[A-Za-z]", item["en"]):
            raise RenderError(f"{name}.en 必须是英语正文；中文翻译放入 zh。")
        if word_count(item["en"]) > 60:
            raise RenderError(f"{name}.en 超过 60 个单词，请拆成两个自然句。")
        focus = sentence.get("focus", [])
        if not isinstance(focus, list) or len(focus) > 3:
            raise RenderError(f"{name}.focus 必须是最多 3 项的列表。")
        item["focus"] = []
        for focus_index, entry in enumerate(focus):
            if not isinstance(entry, dict):
                raise RenderError(f"{name}.focus[{focus_index}] 必须是对象。")
            item["focus"].append({key: nonempty(entry.get(key), f"{name}.focus[{focus_index}].{key}") for key in ("word", "ipa", "zh")})
        checked.append(item)
    lesson["sentences"] = checked
    return lesson


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_approved_audio(lesson, args):
    """Bind known-text alignment to the exact approved PCM file, without edits."""
    if lesson.get("repeats") != 1:
        raise RenderError("已批准的整轨必须单遍输出，不能重复或重新拼接。")
    if not getattr(args, "approved_audio", None) or not getattr(args, "alignment", None):
        raise RenderError("已批准音频需要 --approved-audio WAV 与 --alignment JSON。")
    source = Path(args.approved_audio).expanduser().resolve()
    alignment_path = Path(args.alignment).expanduser().resolve()
    try:
        with wave.open(str(source), "rb") as audio:
            rate, samples = audio.getframerate(), audio.getnframes()
            if audio.getcomptype() != "NONE" or audio.getnchannels() not in (1, 2) or audio.getsampwidth() not in (1, 2, 3, 4) or not 8000 <= rate <= 192000 or samples <= 0:
                raise RenderError("已批准音轨需要非空的标准 PCM WAV，1/2 声道、8k–192k 采样率。")
            duration = samples / rate
        digest = file_sha256(source)
        alignment = json.loads(alignment_path.read_text(encoding="utf-8-sig"))
        if not isinstance(alignment, dict) or alignment.get("audio_sha256") != digest:
            raise RenderError("对齐文件 audio_sha256 与已批准 WAV 不一致，不能复用。")
        recorded_duration = number(alignment.get("audio_duration"), "alignment.audio_duration", 0.000001, 86400)
        if abs(recorded_duration - duration) > .002:
            raise RenderError("对齐记录与原轨实际时长不一致。")
        if alignment.get("english_sha256") is not None and alignment["english_sha256"] != english_digest(lesson):
            raise RenderError("对齐文件的英文稿件哈希不一致。")
        spoken = json.dumps([sentence.get("spoken_text", sentence["en"]) for sentence in lesson["sentences"]], ensure_ascii=False, separators=(",", ":"))
        spoken_hash = hashlib.sha256(spoken.encode("utf-8")).hexdigest()
        if alignment.get("spoken_text_sha256") is not None:
            if alignment["spoken_text_sha256"] != spoken_hash:
                raise RenderError("对齐文件的实际朗读文本已改变，请重新试听并对齐。")
        elif any(sentence.get("spoken_text", sentence["en"]) != sentence["en"] for sentence in lesson["sentences"]):
            raise RenderError("显式 spoken_text 需要新版对齐文件记录实际朗读文本哈希。")
        quality = alignment.get("quality")
        if quality is not None and (not isinstance(quality, dict) or quality.get("status") != "passed"):
            raise RenderError("对齐质量未通过，请复核字幕时间后再生成视频。")
        rows = alignment.get("segments")
        if not isinstance(rows, list) or len(rows) != len(lesson["sentences"]):
            raise RenderError("对齐片段数量与原稿句子数量不一致。")
        entries, cursor = [], 0.0
        for index, (row, sentence) in enumerate(zip(rows, lesson["sentences"])):
            if not isinstance(row, dict) or isinstance(row.get("sentence_index"), bool) or not isinstance(row.get("sentence_index"), int) or row.get("sentence_index") != index or row.get("en") != sentence["en"]:
                raise RenderError("对齐必须逐句按原稿顺序匹配英文，不可替换成识别文本。")
            values = {key: float(number(row.get(key), "alignment.%s" % key, 0, duration + .00002)) for key in ("start", "speech_start", "speech_end", "end")}
            if abs(values["start"] - cursor) > .00002 or not values["start"] <= values["speech_start"] < values["speech_end"] <= values["end"]:
                raise RenderError("对齐时间轴必须从零连续，朗读起止时间须在本句字幕区间内。")
            entry = dict(row)
            entry.update(values, en=sentence["en"], ipa=sentence["ipa"], zh=sentence["zh"], **{"pass": 1})
            entries.append(entry)
            cursor = values["end"]
        if abs(cursor - duration) > .00002:
            raise RenderError("最后一条字幕必须结束于原轨真实时长。")
        mp3_path = Path(args.approved_mp3).expanduser().resolve() if getattr(args, "approved_mp3", None) else None
        if mp3_path is not None:
            preview_duration, info = probe(mp3_path, require_binary("ffprobe"))
            if abs(preview_duration - duration) > .15 or not any(stream.get("codec_type") == "audio" and stream.get("codec_name") == "mp3" for stream in info["streams"]):
                raise RenderError("已批准 MP3 的格式或时长与 WAV 不一致。")
        return {"audio_path": source, "mp3_path": mp3_path, "audio_sha256": digest,
                "duration": duration, "sample_rate": rate, "samples": samples,
                "mp3_sha256": file_sha256(mp3_path) if mp3_path is not None else None,
                "alignment_path": alignment_path, "alignment": alignment, "entries": entries}
    except RenderError:
        raise
    except (OSError, ValueError, KeyError, TypeError, wave.Error, EOFError) as exc:
        raise RenderError("无法安全读取已批准音频与对齐文件：%s。" % type(exc).__name__) from exc


def word_count(text):
    return len(re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)*|\d+(?:\.\d+)?", text))


def require_binary(name):
    result = shutil.which(name)
    if not result:
        raise RenderError(f"找不到 {name}，请安装并加入 PATH。")
    return result


def run(command, timeout=180):
    kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([str(value) for value in command], **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"外部工具执行失败：{type(exc).__name__}。") from exc
    if result.returncode:
        detail = result.stderr.strip()[-1800:]
        raise RenderError(f"{Path(str(command[0])).name} 执行失败（{result.returncode}）：\n{detail}")
    return result.stdout


def probe(path, ffprobe):
    try:
        data = json.loads(run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path]))
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RenderError(f"无法取得 {path.name} 的真实时长。") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RenderError(f"{path.name} 的音视频时长无效。")
    return duration, data


def font_paths(args):
    if Image is None:
        raise RenderError("缺少 Pillow，请安装 scripts/requirements.txt。")
    directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = {
        "en": [directory / "segoeui.ttf", directory / "arial.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")],
        "ipa": [directory / "segoeui.ttf", directory / "arial.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")],
        "zh": [directory / "msyh.ttc", directory / "msyh.ttf", Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")],
    }
    found = {}
    for role, paths in candidates.items():
        override = getattr(args, "font_" + role)
        if override:
            paths = [Path(override).expanduser()]
        selected = next((path for path in paths if path.is_file()), None)
        if selected is None:
            raise RenderError(f"找不到 {role} 字体，请使用 --font-{role} 指定字体文件。")
        found[role] = str(selected)
    return found


def wrap_text(draw, text, font, max_width, role, field):
    tokens = list(text) if role == "zh" else re.findall(r"\S+\s*", text)
    lines, line = [], ""
    for token in tokens:
        proposal = line + token
        if draw.textlength(proposal.rstrip(), font=font) <= max_width:
            line = proposal
            continue
        if line.strip():
            lines.append(line.rstrip())
        line = token.lstrip()
        if draw.textlength(line.rstrip(), font=font) > max_width:
            raise RenderError(f"{field} 中有无法放入画面的长词，请拆分句子或调整字体。")
    if line.strip():
        lines.append(line.rstrip())
    return lines


def text_block(draw, text, role, fonts, size, width, field):
    font = ImageFont.truetype(fonts[role], max(1, round(size)))
    lines = wrap_text(draw, text, font, width, role, field)
    line_height = round(size * (1.42 if role == "ipa" else 1.34))
    return {"lines": lines, "font": font, "height": len(lines) * line_height, "line_height": line_height}


def draw_block(draw, block, x, y, fill):
    for line in block["lines"]:
        draw.text((x, y), line, font=block["font"], fill=fill, anchor="lt")
        y += block["line_height"]
    return y


def headphone(draw, x, y, size, color):
    thick = max(3, round(size / 14))
    draw.arc((x, y, x + size, y + size), 180, 360, fill=color, width=thick)
    draw.rounded_rectangle((x, y + size * .48, x + size * .20, y + size * .88), radius=thick, fill=color)
    draw.rounded_rectangle((x + size * .80, y + size * .48, x + size, y + size * .88), radius=thick, fill=color)


def render_card(lesson, index, fonts, cover=False):
    width, height = lesson["width"], lesson["height"]
    scale = width / 1080
    image = Image.new("RGB", (width, height), PALETTE["bg"])
    draw = ImageDraw.Draw(image)
    margin = round(72 * scale)
    tiny = ImageFont.truetype(fonts["en"], round(27 * scale))
    zh_small = ImageFont.truetype(fonts["zh"], round(29 * scale))
    pill_y = round(height * .055)
    draw.rounded_rectangle((margin, pill_y, margin + 385 * scale, pill_y + 66 * scale), radius=33 * scale, fill=PALETTE["accent"])
    draw.text((margin + 28 * scale, pill_y + 17 * scale), "慢速英语磨耳朵", font=zh_small, fill=PALETTE["ink"], anchor="lt")
    headphone(draw, width - margin - 64 * scale, pill_y + 2 * scale, 62 * scale, PALETTE["accent"])
    available = width - margin * 2
    title = None
    for size in (69, 63, 57, 51, 45):
        title = text_block(draw, lesson["title"], "zh", fonts, size * scale, available, "title")
        if title["height"] <= height * .13:
            break
    else:
        raise RenderError("title 过长，请缩短中文标题。")
    title_y = pill_y + 112 * scale
    title_end = draw_block(draw, title, margin, title_y, PALETTE["white"])
    english_title = text_block(draw, lesson["title_en"], "en", fonts, 29 * scale, available, "title_en")
    if english_title["height"] > 90 * scale:
        raise RenderError("title_en 过长，请缩短英文标题。")
    draw_block(draw, english_title, margin + 2 * scale, title_end + 12 * scale, PALETTE["accent"])
    card_top = max(round(height * .268), round(title_end + english_title["height"] + 64 * scale))
    card_bottom = round(height * .837)
    inner_x = margin + round(54 * scale)
    inner_width = width - inner_x * 2
    sentence = lesson["sentences"][index]
    blocks = None
    available_height = card_bottom - card_top - 152 * scale
    for shrink in (1.0, .96, .92, .88):
        sizes = {"en": max(68, 78 * shrink) * scale, "ipa": 44 * scale, "zh": max(48, 51 * shrink) * scale, "focus": 32 * scale * shrink}
        try:
            en = text_block(draw, sentence["en"], "en", fonts, sizes["en"], inner_width, f"sentences[{index}].en")
            ipa = text_block(draw, sentence["ipa"], "ipa", fonts, sizes["ipa"], inner_width, f"sentences[{index}].ipa")
            zh = text_block(draw, sentence["zh"], "zh", fonts, sizes["zh"], inner_width, f"sentences[{index}].zh")
            focus = []
            for fi, entry in enumerate(sentence["focus"]):
                # Each role uses its own font: Chinese glyphs must not fall back
                # to an English font that lacks them.
                label = text_block(draw, entry["word"] + "  " + entry["ipa"], "ipa", fonts, sizes["focus"], inner_width, f"sentences[{index}].focus[{fi}]")
                meaning = text_block(draw, entry["zh"], "zh", fonts, sizes["focus"] * .93, inner_width, f"sentences[{index}].focus[{fi}].zh")
                focus.append((label, meaning))
            needed = en["height"] + ipa["height"] + zh["height"] + 145 * scale
            if focus:
                needed += 43 * scale + sum(a["height"] + b["height"] + 12 * scale for a, b in focus)
            if needed <= available_height:
                blocks = en, ipa, zh, focus, needed
                break
        except RenderError:
            if shrink == .88:
                raise
    if blocks is None:
        raise RenderError(f"sentences[{index}] 的英文、IPA、中文和重点词超出版面；请拆成两个自然句或减少重点词，脚本不会裁字。")
    draw.rounded_rectangle((margin + 9 * scale, card_top + 15 * scale, width - margin + 9 * scale, card_bottom + 15 * scale), radius=34 * scale, fill="#1b1529")
    draw.rounded_rectangle((margin, card_top, width - margin, card_bottom), radius=34 * scale, fill=PALETTE["paper"])
    tag_font = ImageFont.truetype(fonts["en"], round(23 * scale))
    tag_y = card_top + 37 * scale
    draw.text((inner_x, tag_y), "CONTINUOUS SLOW LISTENING", font=tag_font, fill=PALETTE["muted"], anchor="lt")
    count = f"{index + 1:02d} / {len(lesson['sentences']):02d}"
    count_width = draw.textlength(count, font=tag_font)
    draw.text((width - inner_x - count_width, tag_y), count, font=tag_font, fill=PALETTE["muted"], anchor="lt")
    en, ipa, zh, focus, needed = blocks
    y = card_top + 101 * scale + max(0, (available_height - needed) * .22)
    y = draw_block(draw, en, inner_x, y, PALETTE["ink"])
    draw.text((inner_x, y + 18 * scale), "美式 IPA" if lesson["accent"] == "en-US" else "IPA", font=ImageFont.truetype(fonts["zh"], round(22 * scale)), fill=PALETTE["muted"], anchor="lt")
    y = draw_block(draw, ipa, inner_x, y + 50 * scale, PALETTE["muted"])
    y += 26 * scale
    draw.line((inner_x, y, width - inner_x, y), fill=PALETTE["line"], width=max(1, round(2 * scale)))
    y = draw_block(draw, zh, inner_x, y + 30 * scale, PALETTE["ink"])
    if focus:
        y += 30 * scale
        for label, meaning in focus:
            y = draw_block(draw, label, inner_x, y, "#675182")
            y = draw_block(draw, meaning, inner_x, y + 2 * scale, PALETTE["muted"]) + 10 * scale
    if y > card_bottom - 25 * scale:
        raise RenderError(f"sentences[{index}] 超出版面；请拆分句子，脚本不会裁字。")
    footer_y = round(height * .879)
    draw.rounded_rectangle((margin, footer_y, width - margin, footer_y + 8 * scale), radius=4 * scale, fill="#514162")
    progress_end = margin + available * ((index + 1) / len(lesson["sentences"]))
    draw.rounded_rectangle((margin, footer_y, progress_end, footer_y + 8 * scale), radius=4 * scale, fill=PALETTE["accent"])
    footer = f"{lesson['level']}  ·  {lesson['accent']}  ·  {lesson['target_wpm']:g} WPM  ·  {lesson['repeats']} PASSES"
    if draw.textlength(footer, font=tiny) > available:
        raise RenderError("level 过长，页脚无法放入画面。")
    draw.text((margin, footer_y + 34 * scale), footer, font=tiny, fill=PALETTE["accent"], anchor="lt")
    draw.text((margin, footer_y + 88 * scale), "慢一点，听懂一个完整的故事。", font=zh_small, fill=PALETTE["white"], anchor="lt")
    return image


async def edge_synthesize(sentences, paths, voice, rate):
    try:
        import edge_tts
    except ImportError as exc:
        raise RenderError("缺少 edge-tts，请安装 requirements.txt 或设置运行库 PYTHONPATH；也可显式使用 --tts sapi。") from exc
    for index, (sentence, path) in enumerate(zip(sentences, paths)):
        print(f"配音 {index + 1}/{len(sentences)}（edge）", flush=True)
        try:
            await asyncio.wait_for(edge_tts.Communicate(sentence["en"], voice, rate=rate).save(str(path)), timeout=90)
        except Exception as exc:
            raise RenderError(f"edge 第 {index + 1} 句配音失败：{type(exc).__name__}。未生成静音替代。可检查网络后重试，或显式使用 --tts sapi。") from exc


def sapi_synthesize(sentences, paths, voice, accent, work, rate):
    if os.name != "nt":
        raise RenderError("SAPI 仅支持 Windows。")
    powershell = require_binary("powershell.exe")
    script = work / "sapi.ps1"
    payload = work / "sapi-input.json"
    # The payload intentionally contains English only, never IPA or Chinese.
    payload.write_text(json.dumps({"voice": voice or "", "accent": accent, "rate": rate, "items": [{"text": sentence["en"], "path": str(path)} for sentence, path in zip(sentences, paths)]}, ensure_ascii=False), encoding="utf-8-sig")
    script.write_text(r'''param([Parameter(Mandatory=$true)][string]$InputJson)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
Add-Type -AssemblyName System.Speech
$data = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 | ConvertFrom-Json
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $chosen = $null
    foreach ($installed in $speaker.GetInstalledVoices()) {
        if (-not $installed.Enabled) { continue }
        $info = $installed.VoiceInfo
        if ($data.voice -and $info.Name -eq $data.voice -and $info.Culture.Name -eq $data.accent) { $chosen = $info; break }
        if (-not $data.voice -and $info.Culture.Name -eq $data.accent) { $chosen = $info; break }
    }
    if ($null -eq $chosen) { throw 'No matching English SAPI voice is installed; use an installed SAPI voice name or edge TTS.' }
    $speaker.SelectVoice($chosen.Name)
    $speaker.Rate = [int]$data.rate
    $speaker.Volume = 100
    foreach ($item in $data.items) {
        try {
            $speaker.SetOutputToWaveFile([string]$item.path)
            $speaker.Speak([string]$item.text)
        } finally { $speaker.SetOutputToNull() }
    }
    Write-Output $chosen.Name
} finally { $speaker.Dispose() }
''', encoding="utf-8-sig")
    print(f"本地配音 {len(sentences)} 句（SAPI）", flush=True)
    return run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, "-InputJson", payload], timeout=max(120, len(sentences) * 25)).strip()


def pcm_info(path):
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SAMPLE_RATE):
                raise RenderError(f"{path.name} 的 PCM 格式不符合预期。")
            count = audio.getnframes()
            frames = audio.readframes(count)
    except (wave.Error, OSError) as exc:
        raise RenderError(f"无法读取 {path.name} 音频。") from exc
    samples = array("h", frames)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples or max(abs(value) for value in samples) < 100:
        raise RenderError(f"{path.name} 没有可听配音，停止合成。")
    return count, frames


def tempo_filter(factor):
    filters = []
    while factor < .5:
        filters.append("atempo=0.5")
        factor /= .5
    while factor > 2:
        filters.append("atempo=2.0")
        factor /= 2
    filters.append(f"atempo={factor:.10f}")
    return ",".join(filters)


def create_audio(lesson, args, work, ffmpeg, ffprobe):
    # Auto is an explicit online-first choice. A local-only request never
    # changes to an online engine. Native speed does most of the slowing;
    # atempo is reserved for a small measured correction.
    engine = "edge" if args.tts == "auto" else args.tts
    requested_voice = args.voice or lesson.get("voice")
    selected_voice = requested_voice
    edge_rate = lesson.get("edge_rate", "-60%")
    sapi_rate = lesson.get("sapi_rate", -6)
    total_words = sum(word_count(sentence["en"]) for sentence in lesson["sentences"])
    trim = "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,areverse"
    for attempt in range(2):
        extension = "mp3" if engine == "edge" else "wav"
        sources = [work / f"source-{attempt}-{i:03d}.{extension}" for i in range(len(lesson["sentences"]))]
        if engine == "edge":
            selected_voice = requested_voice or {"en-US": "en-US-AriaNeural", "en-GB": "en-GB-SoniaNeural"}.get(lesson["accent"])
            if not selected_voice:
                raise RenderError("该 accent 需要显式指定 --voice。")
            try:
                asyncio.run(edge_synthesize(lesson["sentences"], sources, selected_voice, edge_rate))
            except RenderError:
                if args.tts != "auto" or os.name != "nt":
                    raise
                print("edge 配音失败，按 --tts auto 根据 accent 选择本地 SAPI 声音。", flush=True)
                engine = "sapi"
                requested_voice = None
                sources = [work / f"source-sapi-{attempt}-{i:03d}.wav" for i in range(len(lesson["sentences"]))]
                selected_voice = sapi_synthesize(lesson["sentences"], sources, None, lesson["accent"], work, sapi_rate)
        else:
            selected_voice = sapi_synthesize(lesson["sentences"], sources, requested_voice, lesson["accent"], work, sapi_rate)
        normalized, source_durations = [], []
        for index, source in enumerate(sources):
            if not source.is_file() or source.stat().st_size < 128:
                raise RenderError(f"第 {index + 1} 句配音为空，停止合成。")
            probe(source, ffprobe)
            destination = work / f"clean-{attempt}-{index:03d}.wav"
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-af", trim, "-ar", SAMPLE_RATE, "-ac", 1, "-c:a", "pcm_s16le", destination])
            pcm_info(destination)
            duration, _ = probe(destination, ffprobe)
            source_durations.append(duration)
            normalized.append(destination)
        source_wpm = total_words / sum(source_durations) * 60
        tempo = lesson["target_wpm"] / source_wpm
        if .75 <= tempo <= 1.25:
            break
        if attempt:
            raise RenderError(f"原生配音实测 {source_wpm:.1f} WPM，仍需超过允许幅度的音频调整；请修改 --edge-rate 或 --sapi-rate，使原生语速接近目标。未生成成片。")
        if engine == "edge":
            percent = int(edge_rate[:-1])
            revised = max(-85, min(40, round(((1 + percent / 100) * tempo - 1) * 100)))
            edge_rate = f"{revised:+d}%"
        else:
            change = round(6 * math.log2(tempo))
            sapi_rate = max(-10, min(10, sapi_rate + change))
        print(f"原生语速 {source_wpm:.1f} WPM 偏离目标；调整引擎速率后重合成一次。", flush=True)
    adjusted, durations = [], []
    for index, source in enumerate(normalized):
        destination = work / f"speech-{index:03d}.wav"
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-af", tempo_filter(tempo), "-ar", SAMPLE_RATE, "-ac", 1, "-c:a", "pcm_s16le", destination])
        pcm_info(destination)
        duration, _ = probe(destination, ffprobe)
        adjusted.append(destination)
        durations.append(duration)
    return adjusted, durations, {"engine": engine, "voice": selected_voice, "native_rate": edge_rate if engine == "edge" else sapi_rate, "native_synthesis_passes": attempt + 1, "source_wpm": source_wpm, "atempo": tempo, "target_wpm": lesson["target_wpm"], "actual_speech_wpm": total_words / sum(durations) * 60, "words_per_pass": total_words}


def make_soundtrack(lesson, audio_paths, durations, work, fps):
    samples_per_frame = SAMPLE_RATE // fps
    chunks, clip_frames = [], []
    for path in audio_paths:
        count, data = pcm_info(path)
        length = math.ceil((count + round(lesson["pause"] * SAMPLE_RATE)) / samples_per_frame) * samples_per_frame
        chunks.append(data + b"\0\0" * (length - count))
        clip_frames.append(length // samples_per_frame)
    soundtrack = work / "narration.wav"
    with wave.open(str(soundtrack), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        for _ in range(lesson["repeats"]):
            for chunk in chunks:
                audio.writeframes(chunk)
    entries, cursor = [], 0
    for repeat in range(lesson["repeats"]):
        for index, frames in enumerate(clip_frames):
            segment_duration = frames / fps
            sentence = lesson["sentences"][index]
            entries.append({"sentence_index": index, "pass": repeat + 1, "start": round(cursor, 6), "speech_end": round(cursor + durations[index], 6), "end": round(cursor + segment_duration, 6), "speech_duration": round(durations[index], 6), "pause_duration": round(segment_duration - durations[index], 6), "en": sentence["en"], "ipa": sentence["ipa"], "zh": sentence["zh"]})
            cursor += segment_duration
    return soundtrack, clip_frames, entries, cursor


def normalize_soundtrack(path, target_rms_db=-22.0, peak_ceiling_db=-2.0):
    """Match audition levels with one constant gain, preserving timing/dynamics."""
    count, data = pcm_info(path)
    samples = array("h", data)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if rms <= 0:
        raise RenderError("配音音轨为静音，不能进行音量校准。")
    source_rms_db = 20 * math.log10(rms / 32768)
    source_peak_db = 20 * math.log10(peak / 32768)
    gain_db = min(target_rms_db - source_rms_db, peak_ceiling_db - source_peak_db)
    gain = 10 ** (gain_db / 20)
    adjusted = array("h", (round(value * gain) for value in samples))
    output_peak = max(abs(value) for value in adjusted)
    output_rms = math.sqrt(sum(value * value for value in adjusted) / len(adjusted))
    if sys.byteorder != "little":
        adjusted.byteswap()
    temporary = Path(path).with_suffix(".normalized.wav")
    with wave.open(str(temporary), "wb") as audio:
        audio.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        audio.writeframes(adjusted.tobytes())
    os.replace(temporary, path)
    return {"method": "constant gain; no compression or duration change", "metric": "RMS dBFS including natural pauses; not LUFS",
            "target_rms_dbfs": target_rms_db, "peak_ceiling_dbfs": peak_ceiling_db,
            "gain_db": round(gain_db, 6), "source_rms_dbfs": round(source_rms_db, 6),
            "source_peak_dbfs": round(source_peak_db, 6), "output_rms_dbfs": round(20 * math.log10(output_rms / 32768), 6),
            "output_peak_dbfs": round(20 * math.log10(output_peak / 32768), 6), "pcm_samples": count}


def srt_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def concat_escape(path):
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def english_digest(lesson):
    text = json.dumps([item["en"] for item in lesson["sentences"]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_audio_bundle(lesson, directory, work=None, requested_voice=None):
    """Reject stale, changed or malformed audio before reusing its measured PCM."""
    directory = Path(directory).resolve()
    try:
        bundle = json.loads((directory / "bundle.json").read_text(encoding="utf-8-sig"))
        if not isinstance(bundle, dict):
            raise RenderError("试听 bundle 必须是 JSON 对象。")
        if bundle.get("schema_version") != 1:
            raise RenderError("试听 bundle 版本不支持。")
        if bundle.get("english_sha256") != english_digest(lesson):
            raise RenderError("试听英文稿件与当前文本不一致，请重新生成试听。")
        if bundle.get("accent") != lesson["accent"] or bundle.get("target_wpm") != lesson["target_wpm"]:
            raise RenderError("试听口音或目标语速与当前内容不一致。")
        rows = bundle.get("sentences")
        if not isinstance(rows, list) or len(rows) != len(lesson["sentences"]):
            raise RenderError("试听音频数量与句子数量不一致。")
        paths, durations = [], []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RenderError("试听的逐句音频记录无效。")
            name = row.get("file", "")
            if name != f"speech-{index:03d}.wav":
                raise RenderError("试听音频路径无效，拒绝读取目录外文件。")
            source = directory / name
            if source.resolve().parent != directory:
                raise RenderError("试听音频路径越界。")
            if hashlib.sha256(source.read_bytes()).hexdigest() != row.get("sha256"):
                raise RenderError(f"第 {index + 1} 句试听音频校验失败，请重新生成。")
            samples, _ = pcm_info(source)
            duration = samples / SAMPLE_RATE
            if duration <= 0:
                raise RenderError("试听音频为空。")
            if abs(duration - float(row["duration"])) > .002:
                raise RenderError("试听记录与音频真实时长不一致。")
            destination = source if work is None else Path(work) / name
            if destination != source:
                shutil.copyfile(source, destination)
            paths.append(destination)
            durations.append(duration)
        metadata = dict(bundle["audio"])
        if requested_voice and metadata.get("voice") != requested_voice:
            raise RenderError("指定声音与试听 bundle 不一致，请为新声音重新生成试听。")
        words = sum(word_count(sentence["en"]) for sentence in lesson["sentences"])
        metadata.update(words_per_pass=words, actual_speech_wpm=words / sum(durations) * 60,
                        target_wpm=lesson["target_wpm"], audio_bundle_reused=True)
        if not metadata.get("engine") or not metadata.get("voice"):
            raise RenderError("试听缺少声音来源记录。")
        return paths, durations, metadata
    except RenderError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RenderError(f"无法读取完整试听 bundle：{type(exc).__name__}。") from exc


def write_audio_bundle(lesson, paths, durations, metadata, work, ffmpeg, soundtrack):
    metadata["preview_encoder"] = encode_audio_preview(ffmpeg, soundtrack, work / "narration.mp3")
    rows = []
    for index, (source, duration) in enumerate(zip(paths, durations)):
        name = f"speech-{index:03d}.wav"
        destination = work / name
        if Path(source) != destination:
            shutil.copyfile(source, destination)
        rows.append({"file": name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "duration": duration})
    bundle = {"schema_version": 1, "english_sha256": english_digest(lesson), "accent": lesson["accent"],
              "target_wpm": lesson["target_wpm"], "audio": metadata, "sentences": rows}
    (work / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")


def encode_audio_preview(ffmpeg, source, destination):
    """Use the MP3 encoder shipped with this FFmpeg build, including Windows MF."""
    encoders = run([ffmpeg, "-hide_banner", "-encoders"])
    encoder = next((name for name in ("libmp3lame", "mp3_mf") if re.search(r"\b" + name + r"\b", encoders)), None)
    if encoder is None:
        raise RenderError("当前 FFmpeg 缺少 MP3 编码器；请使用带 libmp3lame 或 mp3_mf 的构建。原始 WAV 保留在配音缓存中。")
    # Some Windows Media Foundation builds write an incorrect Xing frame
    # count. Disable that header, then check actual container duration.
    options = ["-write_xing", "0"] if encoder == "mp3_mf" else []
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source,
         "-c:a", encoder, "-b:a", "160k", *options, destination])
    ffprobe = require_binary("ffprobe")
    source_duration, _ = probe(source, ffprobe)
    preview_duration, _ = probe(destination, ffprobe)
    if abs(source_duration - preview_duration) > .15:
        raise RenderError("压缩试听时长与 WAV 不一致，停止发布。请检查 MP3 编码器。")
    return encoder


def output_files(lesson, audio_only=False):
    base = tuple(name for name in OUTPUT_FILES if not audio_only or name not in ("video.mp4", "cover.png", "preview.png"))
    return base + tuple(f"speech-{index:03d}.wav" for index in range(len(lesson["sentences"])))


def visual_frame(lesson, index, fonts, args, timestamp=0):
    if args.style == "paper-animation":
        from animation_renderer import frame_at
        entries = [{"sentence_index": index, "pass": 1, "start": 0, "speech_end": 59, "end": 60}]
        return frame_at(lesson, entries, timestamp, fonts=fonts, assets_dir=args.assets_dir)
    return render_card(lesson, index, fonts)


@contextmanager
def output_lock(output):
    lock = output / ".render.lock"
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RenderError("输出目录已被另一合成任务锁定；请选择新目录。若此前进程已结束，请确认后移除 .render.lock 再重试。") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


APPROVED_OUTPUT_FILES = ("video.mp4", "cover.png", "preview.png", "captions.srt", "timeline.json", "lesson.json", "narration.wav", "narration.mp3", "alignment.json")


def render_approved(lesson, args, source, output, fonts):
    """Animate one approved recording without synthesis, gain, or reassembly."""
    approved = load_approved_audio(lesson, args)
    source_audio, entries, duration = approved["audio_path"], approved["entries"], approved["duration"]
    protected = [Path(source).resolve(), source_audio, approved["alignment_path"]]
    if approved["mp3_path"] is not None:
        protected.append(approved["mp3_path"])
    for name in APPROVED_OUTPUT_FILES:
        destination = (output / name).resolve()
        if any(destination == item or (destination.exists() and item.exists() and os.path.samefile(destination, item)) for item in protected):
            raise RenderError("输出路径会覆盖输入音频、对齐或稿件，请使用新目录。")
    check_output(source, output, args.overwrite, APPROVED_OUTPUT_FILES)
    words = sum(word_count(sentence.get("spoken_text", sentence["en"])) for sentence in lesson["sentences"])
    actual_wpm = words / duration * 60
    visual_lesson = dict(lesson)
    visual_lesson.update(repeats=1, actual_wpm_with_native_pauses=actual_wpm, speech_mode="natural", displayed_wpm_metric="including_native_pauses")
    ffmpeg, ffprobe = require_binary("ffmpeg"), require_binary("ffprobe")
    expected_frames = int(math.ceil(duration * args.fps - .000001))
    output.mkdir(parents=True, exist_ok=True)
    with output_lock(output), tempfile.TemporaryDirectory(prefix="._approved-", dir=output) as temporary:
        work = Path(temporary)
        check_output(source, output, args.overwrite, APPROVED_OUTPUT_FILES)
        soundtrack = work / "narration.wav"
        shutil.copyfile(source_audio, soundtrack)
        if file_sha256(soundtrack) != approved["audio_sha256"]:
            raise RenderError("复制已批准 WAV 后哈希不一致，停止发布。")
        if approved["mp3_path"] is not None:
            shutil.copyfile(approved["mp3_path"], work / "narration.mp3")
            if file_sha256(work / "narration.mp3") != approved["mp3_sha256"]:
                raise RenderError("复制已批准 MP3 后哈希不一致，停止发布。")
        else:
            encode_audio_preview(ffmpeg, soundtrack, work / "narration.mp3")
        if args.style == "paper-animation":
            from animation_renderer import AnimationScene, render_animated_video
            scene = AnimationScene(visual_lesson, entries, fonts=fonts, assets_dir=args.assets_dir)
            images = [scene.render_frame(entry["speech_start"]) for entry in entries]
            scene.render_frame(0).save(work / "cover.png")
            render_animated_video(visual_lesson, entries, work / "silent.mp4", ffmpeg, fps=args.fps, fonts=fonts, assets_dir=args.assets_dir, preset=args.preset, threads=args.threads)
        else:
            images = [render_card(visual_lesson, index, fonts) for index in range(len(entries))]
            images[0].save(work / "cover.png")
            clips = []
            for index, (entry, image) in enumerate(zip(entries, images)):
                count = int(math.ceil(entry["end"] * args.fps - .000001) - math.ceil(entry["start"] * args.fps - .000001))
                if count <= 0:
                    continue
                card, clip = work / ("card-%03d.png" % index), work / ("clip-%03d.mp4" % index)
                image.save(card)
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", args.fps, "-i", card, "-frames:v", count, "-an", "-c:v", "libx264", "-preset", args.preset, "-crf", "20", "-pix_fmt", "yuv420p", "-threads", args.threads, clip], timeout=max(180, duration * 15))
                clips.append(clip)
            concat = work / "video.ffconcat"
            concat.write_text("ffconcat version 1.0\n" + "".join("file '%s'\n" % concat_escape(path) for path in clips), encoding="utf-8")
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", concat, "-c:v", "copy", "-an", work / "silent.mp4"])
        indices = sorted(set((0, len(images) // 2, len(images) - 1)))
        thumb_width = 420
        thumb_height = round(thumb_width * lesson["height"] / lesson["width"])
        preview = Image.new("RGB", (thumb_width * len(indices), thumb_height), PALETTE["paper"])
        for position, index in enumerate(indices):
            preview.paste(images[index].resize((thumb_width, thumb_height), getattr(Image, "Resampling", Image).LANCZOS), (position * thumb_width, 0))
        preview.save(work / "preview.png")
        final = work / "video.mp4"
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", work / "silent.mp4", "-i", soundtrack, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-t", "%.9f" % duration, "-movflags", "+faststart", final], timeout=max(180, duration * 3))
        final_duration, info = probe(final, ffprobe)
        video = next((stream for stream in info["streams"] if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in info["streams"] if stream.get("codec_type") == "audio"), None)
        if not video or not audio or video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p" or audio.get("codec_name") != "aac" or (video.get("width"), video.get("height")) != (lesson["width"], lesson["height"]) or int(video.get("nb_frames", -1)) != expected_frames:
            raise RenderError("已批准音轨的视频流、尺寸或帧数与真实对齐不一致。")
        if abs(final_duration - duration) > 1 / args.fps + .002 or abs(float(audio.get("duration", 0)) - duration) > .03:
            raise RenderError("最终视频或音轨时长与已批准 WAV 不一致。")
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", final, "-f", "null", "-"], timeout=max(180, duration * 3))
        if file_sha256(source_audio) != approved["audio_sha256"] or file_sha256(soundtrack) != approved["audio_sha256"]:
            raise RenderError("原轨或交付 WAV 在渲染期间发生变化，停止发布。")
        if approved["mp3_path"] is not None and file_sha256(approved["mp3_path"]) != approved["mp3_sha256"]:
            raise RenderError("已批准 MP3 在渲染期间发生变化，停止发布。")
        captions = ["%d\n%s --> %s\n%s\n%s\n%s\n" % (index + 1, srt_time(entry["start"]), srt_time(entry["end"]), entry["en"], entry["ipa"], entry["zh"]) for index, entry in enumerate(entries)]
        (work / "captions.srt").write_text("\n".join(captions), encoding="utf-8")
        audio_metadata = {"engine": "approved-recording", "voice": "provided recording", "mode": "natural", "words": words, "duration": duration, "actual_wpm_with_native_pauses": actual_wpm, "post_speed_adjustment_applied": False, "added_sentence_pause_seconds": 0, "source_synthesis_requests_this_render": 0, "loudness_adjustment_this_render": False, "audio_sha256": approved["audio_sha256"]}
        metadata = {"schema_version": 3, "mode": "approved_natural_slow_audio", "style": args.style, "width": lesson["width"], "height": lesson["height"], "fps": args.fps, "repeats": 1, "english_sha256": english_digest(lesson), "audio": audio_metadata, "source_audio_sha256": approved["audio_sha256"], "source_audio_pcm_samples": approved["samples"], "source_audio_sample_rate": approved["sample_rate"], "soundtrack_duration": duration, "video_duration": final_duration, "video_frames": expected_frames, "audio_reused_without_time_stretch_or_reassembly": True, "displayed_wpm_metric": "whole approved recording including native pauses", "timing_source": approved["alignment"].get("method", "provided known-text alignment"), "alignment_file": "alignment.json", "segments": entries}
        published_alignment = dict(approved["alignment"])
        if "audio_path" in published_alignment:
            published_alignment["audio_path"] = "narration.wav"
        (work / "alignment.json").write_text(json.dumps(published_alignment, ensure_ascii=False, indent=2), encoding="utf-8")
        (work / "timeline.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        (work / "lesson.json").write_text(json.dumps(visual_lesson, ensure_ascii=False, indent=2), encoding="utf-8")
        for filename in APPROVED_OUTPUT_FILES:
            if args.overwrite:
                os.replace(work / filename, output / filename)
            elif os.name == "nt":
                os.rename(work / filename, output / filename)
            else:
                os.link(work / filename, output / filename)
    print("已输出：%s" % (output / "video.mp4"), flush=True)
    print("复用已批准整轨；含自然停顿 %.2f WPM；%.3f 秒；未后期变速、未追加停顿、未重新合成。" % (actual_wpm, duration), flush=True)


def render(lesson, args, source, output, fonts):
    ffmpeg, ffprobe = require_binary("ffmpeg"), require_binary("ffprobe")
    output.mkdir(parents=True, exist_ok=True)
    with output_lock(output), tempfile.TemporaryDirectory(prefix="._render-", dir=output) as temporary:
        check_output(source, output, args.overwrite, output_files(lesson, args.audio_only))
        work = Path(temporary)
        cards = []
        for index in range(0 if args.audio_only else len(lesson["sentences"])):
            image = visual_frame(lesson, index, fonts, args, timestamp=index * 2.0)
            card = work / f"card-{index:03d}.png"
            image.save(card)
            cards.append(card)
        if cards:
            shutil.copyfile(cards[0], work / "cover.png")
        # Three different sentences remain legible in a contact sheet.
        indices = sorted(set((0, len(cards) // 2, len(cards) - 1))) if cards else []
        thumbnail_width = 420
        thumbnail_height = round(thumbnail_width * lesson["height"] / lesson["width"])
        if indices:
            preview = Image.new("RGB", (thumbnail_width * len(indices), thumbnail_height), PALETTE["bg"])
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            for position, index in enumerate(indices):
                with Image.open(cards[index]) as image:
                    preview.paste(image.resize((thumbnail_width, thumbnail_height), resampling), (position * thumbnail_width, 0))
            preview.save(work / "preview.png")
        if args.audio_bundle:
            audio_paths, durations, audio_metadata = load_audio_bundle(lesson, args.audio_bundle, work, requested_voice=args.voice or lesson.get("voice"))
        else:
            from speech_pipeline import prepare_audio
            audio_paths, durations, audio_metadata = prepare_audio(lesson, args, work, ffmpeg, ffprobe)
        soundtrack, frame_counts, entries, total_duration = make_soundtrack(lesson, audio_paths, durations, work, args.fps)
        audio_metadata["loudness"] = normalize_soundtrack(soundtrack)
        measured_soundtrack, _ = probe(soundtrack, ffprobe)
        if abs(measured_soundtrack - total_duration) > .002:
            raise RenderError("配音时间轴与实际音轨不一致，停止输出。")
        videos = []
        for index, (card, frames) in enumerate(zip(cards if args.style == "card" else [], frame_counts)):
            print(f"画面编码 {index + 1}/{len(cards)}", flush=True)
            clip = work / f"clip-{index:03d}.mp4"
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", 1, "-framerate", args.fps, "-i", card, "-frames:v", frames, "-an", "-c:v", "libx264", "-preset", args.preset, "-tune", "stillimage", "-crf", 20, "-pix_fmt", "yuv420p", "-r", args.fps, "-threads", args.threads, clip], timeout=max(180, frames / args.fps * 15))
            clip_duration, _ = probe(clip, ffprobe)
            if abs(clip_duration - frames / args.fps) > 1 / args.fps:
                raise RenderError(f"第 {index + 1} 句视频时长不符合音频时间轴。")
            videos.append(clip)
        final_duration = None
        if not args.audio_only:
            final = work / "video.mp4"
            if args.style == "paper-animation":
                from animation_renderer import render_animated_video
                silent = work / "animation.mp4"
                render_animated_video(lesson, entries, silent, ffmpeg, fps=args.fps,
                                      fonts=fonts, assets_dir=args.assets_dir, preset=args.preset, threads=args.threads)
                video_input = ["-i", silent]
            else:
                concat = work / "video.ffconcat"
                concat.write_text("ffconcat version 1.0\n" + "".join(f"file '{concat_escape(clip)}'\n" for _ in range(lesson["repeats"]) for clip in videos), encoding="utf-8")
                video_input = ["-f", "concat", "-safe", 0, "-i", concat]
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *video_input, "-i", soundtrack, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", SAMPLE_RATE, "-t", f"{total_duration:.6f}", "-movflags", "+faststart", final], timeout=max(180, total_duration * 3))
            final_duration, info = probe(final, ffprobe)
            video_stream = next((stream for stream in info["streams"] if stream.get("codec_type") == "video"), None)
            audio_stream = next((stream for stream in info["streams"] if stream.get("codec_type") == "audio"), None)
            if not video_stream or not audio_stream or video_stream.get("codec_name") != "h264" or video_stream.get("pix_fmt") != "yuv420p" or audio_stream.get("codec_name") != "aac":
                raise RenderError("最终文件缺少预期的 H264/yuv420p/AAC 音视频流。")
            if video_stream.get("width") != lesson["width"] or video_stream.get("height") != lesson["height"]:
                raise RenderError("最终视频尺寸与请求不符。")
            expected_frames = sum(frame_counts) * lesson["repeats"]
            if int(video_stream.get("nb_frames", -1)) != expected_frames:
                raise RenderError("最终视频帧数与逐句 PCM 时间轴不一致，停止发布。")
            if abs(final_duration - total_duration) > .08:
                raise RenderError("最终视频时长与真实配音时间轴不一致，停止输出。")
        captions = []
        for number_, entry in enumerate(entries, 1):
            captions.append(f"{number_}\n{srt_time(entry['start'])} --> {srt_time(entry['end'])}\n{entry['en']}\n{entry['ipa']}\n{entry['zh']}\n")
        (work / "captions.srt").write_text("\n".join(captions), encoding="utf-8")
        audio_metadata["actual_wpm_with_pauses"] = audio_metadata["words_per_pass"] * lesson["repeats"] / measured_soundtrack * 60
        metadata = {"schema_version": 2, "mode": "continuous_slow_story", "style": "audio-only" if args.audio_only else args.style, "english_sha256": english_digest(lesson), "width": lesson["width"], "height": lesson["height"], "fps": args.fps, "repeats": lesson["repeats"], "accent": lesson["accent"], "audio": audio_metadata, "soundtrack_duration": round(measured_soundtrack, 6), "video_duration": round(final_duration, 6) if final_duration is not None else None, "timing_source": "ffprobe durations and PCM sample counts; video aligned to whole frames", "segments": entries}
        write_audio_bundle(lesson, audio_paths, durations, audio_metadata, work, ffmpeg, soundtrack)
        (work / "timeline.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        (work / "lesson.json").write_text(json.dumps(lesson, ensure_ascii=False, indent=2), encoding="utf-8")
        # Publish only after every asset and final stream has been verified.
        for filename in output_files(lesson, args.audio_only):
            if args.overwrite:
                os.replace(work / filename, output / filename)
            else:
                # Windows rename atomically rejects an existing target. On
                # POSIX rename replaces it, so use no-replace linking there.
                try:
                    if os.name == "nt":
                        os.rename(work / filename, output / filename)
                    else:
                        os.link(work / filename, output / filename)
                except FileExistsError as exc:
                    raise RenderError(f"发布时发现 {filename} 已存在，已拒绝覆盖。") from exc
    print(f"已输出：{output / ('narration.mp3' if args.audio_only else 'video.mp4')}", flush=True)
    print(f"引擎 {audio_metadata['engine']}；配音实测 {audio_metadata['actual_speech_wpm']:.1f} WPM；含句间停顿 {audio_metadata['actual_wpm_with_pauses']:.1f} WPM；时长 {measured_soundtrack:.2f} 秒。", flush=True)


def check_output(source, output, overwrite, filenames=OUTPUT_FILES):
    if output.exists() and not output.is_dir():
        raise RenderError("--output-dir 必须是目录。")
    for filename in filenames:
        destination = output / filename
        if destination.resolve() == source or (destination.exists() and source.exists() and os.path.samefile(destination, source)):
            raise RenderError("输出 lesson.json 会覆盖输入文件，请使用另一个输出目录。")
        if destination.exists() and (not overwrite or not destination.is_file()):
            raise RenderError(f"输出文件 {filename} 已存在；请选择新目录，或显式使用 --overwrite。")


def parser():
    result = argparse.ArgumentParser(description="慢速英语：默认整段自然试听，批准原轨并完成对齐后生成中英字幕与 IPA 视频。")
    result.add_argument("--lesson", required=True, help="UTF-8 lesson JSON 路径")
    result.add_argument("--output-dir", required=True, help="成品输出目录")
    result.add_argument("--speech-mode", choices=("natural", "calibrated"), default="natural", help="natural 整轨原生慢讲；calibrated 旧版逐句目标词速训练")
    result.add_argument("--tts", choices=("edge", "sapi", "kokoro", "auto"), default="edge", help="edge 在线；kokoro 本地 ONNX；sapi Windows 本地；auto Edge→SAPI 回退")
    result.add_argument("--voice", help="覆盖 JSON voice；edge 声音名或已安装 SAPI 名称")
    result.add_argument("--edge-rate", help="Edge 原生语速百分比，natural 默认 -20%%，calibrated 按目标词速估算")
    result.add_argument("--sapi-rate", type=int, help="SAPI 原生语速整数-10到10；100WPM默认约-5，可指定-6")
    result.add_argument("--target-wpm", type=float, help="仅calibrated：目标词速，默认 JSON target_wpm 或100")
    result.add_argument("--repeats", type=int, help="natural/approved 仅单遍；calibrated 默认 JSON repeats 或2")
    result.add_argument("--pause", type=float, help="仅calibrated：追加句间停顿0.45–0.7秒，默认0.6秒")
    result.add_argument("--audio-only", action="store_true", help="只生成同稿试听；natural 导出完整 WAV/MP3，calibrated 保留逐句 bundle")
    result.add_argument("--approved-audio", help="已试听批准的完整 PCM WAV；保持字节不变，用真实对齐渲染")
    result.add_argument("--alignment", help="与已批准 WAV 哈希一致的逐句真实对齐 JSON")
    result.add_argument("--approved-mp3", help="可选：已批准试听 MP3，直接复制交付；不作为重拼接来源")
    result.add_argument("--audio-bundle", help="复用试听目录中经文本和音频哈希校验的逐句配音")
    result.add_argument("--audio-cache", help="持久配音缓存目录；只改字幕、音标或画面时复用音频")
    result.add_argument("--kokoro-python", help="含 Kokoro 依赖的独立 Python 解释器")
    result.add_argument("--kokoro-model", help="浮点 speed 输入的 Kokoro ONNX 模型路径")
    result.add_argument("--kokoro-voices", help="与模型配套的 voices bin 路径")
    result.add_argument("--kokoro-speed", type=float, help="Kokoro 原生 speed，默认按 target_wpm 估算")
    result.add_argument("--espeak-data-path", help="Windows Kokoro 的 eSpeak 数据目录，建议纯 ASCII 路径")
    result.add_argument("--style", choices=("card", "paper-animation"), default="card", help="静态卡片或分层纸面动画")
    result.add_argument("--assets-dir", help="动画背景/透明角色素材目录；缺省使用 Skill assets 或矢量场景")
    result.add_argument("--width", type=int, help="覆盖画面宽度；横屏动画推荐1920")
    result.add_argument("--height", type=int, help="覆盖画面高度；横屏动画推荐1080")
    result.add_argument("--font-en", help="英文字体 TTF/TTC")
    result.add_argument("--font-zh", help="中文字体 TTF/TTC，Windows 默认微软雅黑")
    result.add_argument("--font-ipa", help="含 IPA 字符的字体 TTF/TTC")
    result.add_argument("--fps", type=int, choices=(24, 25, 30), default=30)
    result.add_argument("--preset", choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"), default="fast")
    result.add_argument("--threads", type=int, default=4, help="ffmpeg 画面编码线程数，默认4")
    result.add_argument("--validate-only", action="store_true", help="仅校验 JSON、字体和每句版面，不合成、不联网、不写输出")
    result.add_argument("--overwrite", action="store_true", help="允许替换本脚本生成的成品文件，永不覆盖输入")
    return result


def main():
    # A stable UTF-8 CLI works both in Windows terminals and captured logs.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        local_config = Path(__file__).resolve().parents[1] / "runtime.local.json"
        if local_config.is_file():
            try:
                config = json.loads(local_config.read_text(encoding="utf-8-sig"))
                for key in ("kokoro_python", "kokoro_model", "kokoro_voices", "audio_cache", "espeak_data_path"):
                    if getattr(args, key) is None and isinstance(config.get(key), str):
                        setattr(args, key, config[key])
            except (OSError, ValueError, AttributeError) as exc:
                raise RenderError("本机 runtime.local.json 无效，请修复或暂时移开该文件。") from exc
        if args.threads < 1 or args.threads > 32:
            raise RenderError("--threads 必须在1到32之间。")
        source = Path(args.lesson).expanduser().resolve()
        output = Path(args.output_dir).expanduser().resolve()
        lesson = load_lesson(source, args)
        if args.audio_bundle and args.speech_mode != "calibrated":
            raise RenderError("逐句 --audio-bundle 仅适用于 --speech-mode calibrated；自然整轨请使用 --approved-audio 与 --alignment。")
        if (args.alignment or args.approved_mp3) and not args.approved_audio:
            raise RenderError("--alignment/--approved-mp3 需要同时提供 --approved-audio WAV。")
        if args.approved_audio and args.audio_only:
            raise RenderError("已批准音频用于视频交付；生成新试听请用 --audio-only，勿同时指定 --approved-audio。")
        if args.speech_mode == "natural" and not args.audio_only and not args.approved_audio and not args.validate_only:
            raise RenderError("自然慢讲请先用 --audio-only 试听，完成对齐后用 --approved-audio WAV --alignment JSON 生成视频。")
        if args.speech_mode == "natural" and not args.approved_audio and args.tts != "edge":
            raise RenderError("natural 整轨目前支持 Edge；其他引擎请显式使用 --speech-mode calibrated。")
        check_output(source, output, args.overwrite, APPROVED_OUTPUT_FILES if args.approved_audio else output_files(lesson, args.audio_only))
        if args.audio_bundle and Path(args.audio_bundle).resolve() == output:
            raise RenderError("请在新目录生成视频，不能覆盖正在复用的试听 bundle。")
        fonts = None if args.audio_only else font_paths(args)
        for index in range(0 if args.audio_only else len(lesson["sentences"])):
            visual_frame(lesson, index, fonts, args)
        if args.audio_bundle:
            load_audio_bundle(lesson, args.audio_bundle, requested_voice=args.voice or lesson.get("voice"))
        if args.approved_audio:
            load_approved_audio(lesson, args)
        if args.validate_only:
            if args.speech_mode == "natural" or args.approved_audio:
                print(f"输入与版面校验通过：{len(lesson['sentences'])}句，单遍自然整轨。未生成音频或视频。")
            else:
                print(f"输入与版面校验通过：{len(lesson['sentences'])}句，{lesson['repeats']}遍，目标{lesson['target_wpm']:g} WPM。未生成音频或视频。")
            return 0
        if args.approved_audio:
            render_approved(lesson, args, source, output, fonts)
        elif args.audio_only and args.speech_mode == "natural":
            from speech_pipeline import audition
            audition(lesson, args, source, output)
        else:
            render(lesson, args, source, output, fonts)
        return 0
    except RenderError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"错误：文件操作失败（{type(exc).__name__}）；未确认成功输出。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断；未发布本次未验证成品。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
