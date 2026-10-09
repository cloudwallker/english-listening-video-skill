#!/usr/bin/env python3
"""Align an approved English recording without regenerating or altering it.

The optional CPU TorchAudio backend aligns known text to acoustic frames.
Subtitle transitions prefer measured waveform silence between sentences.
JSON contains filenames and hashes, never absolute local input/model paths.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import wave
from array import array
from pathlib import Path

MODEL_NAME = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"
MODEL_SOURCE = "https://download.pytorch.org/torchaudio/models/" + MODEL_NAME
MODEL_REFERENCE = "https://docs.pytorch.org/audio/2.5.0/tutorials/forced_alignment_tutorial.html"


class AlignmentError(Exception):
    """Input, acoustic evidence or model failure requiring user attention."""


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_lesson(path):
    try:
        lesson = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AlignmentError("无法读取 lesson JSON。") from exc
    known_words(lesson)
    return lesson


def known_words(lesson):
    if not isinstance(lesson, dict) or not isinstance(lesson.get("sentences"), list) or not lesson["sentences"]:
        raise AlignmentError("lesson 必须包含非空 sentences 列表。")
    result = []
    for index, sentence in enumerate(lesson["sentences"]):
        if not isinstance(sentence, dict) or not isinstance(sentence.get("en"), str) or not sentence["en"].strip():
            raise AlignmentError(f"第 {index + 1} 句缺少英文 en。")
        spoken = sentence.get("spoken_text", sentence["en"])
        if not isinstance(spoken, str) or not spoken.strip():
            raise AlignmentError(f"第 {index + 1} 句 spoken_text 无效。")
        if any(character.isdigit() for character in spoken):
            raise AlignmentError(f"第 {index + 1} 句含数字；请用 spoken_text 写明录音实际读法，例如 three，不能在对齐时静默省略数字。")
        if re.search(r"[\u3400-\u9fff]", spoken):
            raise AlignmentError("英语对齐模型不能用于中文朗读正文。")
        if any(character.isalpha() and character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" for character in spoken):
            raise AlignmentError(f"第 {index + 1} 句含模型不支持的字母；请用 spoken_text 写明只含英语字母的实际读法，不能静默截断单词。")
        words = re.findall(r"[A-Z]+(?:'[A-Z]+)*", spoken.upper().replace("’", "'"))
        if not words:
            raise AlignmentError(f"第 {index + 1} 句没有可对齐的英文单词。")
        result.append(words)
    return result


def read_pcm(path):
    try:
        with wave.open(str(path), "rb") as source:
            rate, count = source.getframerate(), source.getnframes()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != "NONE":
                raise AlignmentError("对齐输入需要单声道 16-bit PCM WAV；请使用试听导出的 narration.wav。")
            if rate < 8000 or not count:
                raise AlignmentError("对齐输入采样率或时长无效。")
            data = source.readframes(count)
    except (wave.Error, OSError, EOFError) as exc:
        raise AlignmentError("无法读取 PCM WAV；MP3 请先明确转换为供审核使用的 WAV。") from exc
    values = array("h", data)
    if sys.byteorder != "little":
        values.byteswap()
    if len(values) != count or not values or max(abs(value) for value in values) < 100:
        raise AlignmentError("对齐音轨为空、截断或几乎无声。")
    return {"data": data, "samples": values, "sample_rate": rate, "sample_count": count, "duration": count / rate}


def detect_silences(pcm, threshold_db=-40.0, minimum_seconds=0.16):
    """Measure quiet PCM sample runs, matching mono silencedetect semantics."""
    rate = pcm["sample_rate"]
    threshold = 32768 * 10 ** (threshold_db / 20)
    result, beginning = [], None
    for index, value in enumerate(pcm["samples"]):
        if abs(value) <= threshold:
            if beginning is None:
                beginning = index
        elif beginning is not None:
            if (index - beginning) / rate >= minimum_seconds:
                result.append({"start": beginning / rate, "end": index / rate, "duration": (index - beginning) / rate})
            beginning = None
    if beginning is not None and (pcm["sample_count"] - beginning) / rate >= minimum_seconds:
        result.append({"start": beginning / rate, "end": pcm["duration"], "duration": (pcm["sample_count"] - beginning) / rate})
    return result


def infer_known_words(pcm, lesson, model_dir, offline=False):
    """The sole optional heavyweight boundary: inference + known-text CTC."""
    try:
        import numpy as np
        import torch
        import torchaudio
    except (ImportError, OSError) as exc:
        raise AlignmentError("缺少可选 CPU 对齐环境。请在独立 Python 3.10–3.12 环境安装 requirements-align.txt；主视频依赖无需更改。") from exc
    checkpoint = Path(model_dir) / MODEL_NAME
    if offline and not checkpoint.is_file():
        raise AlignmentError("--offline 指定的模型缓存中没有官方英语 Wav2Vec2 权重。")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    if not checkpoint.is_file():
        print("首次下载官方英语 Wav2Vec2 BASE 960h 权重（约 378 MB）；后续复用缓存。", flush=True)
    bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
    torch.set_num_threads(4)
    waveform = torch.from_numpy(np.frombuffer(pcm["data"], dtype="<i2").astype(np.float32) / 32768).unsqueeze(0)
    try:
        model = bundle.get_model(dl_kwargs={"model_dir": str(checkpoint.parent), "weights_only": True}).eval()
        resampled = torchaudio.functional.resample(waveform, pcm["sample_rate"], bundle.sample_rate)
        with torch.inference_mode():
            emissions, _ = model(resampled)
            log_probs = torch.log_softmax(emissions, dim=-1)
        labels = bundle.get_labels()
        dictionary = {symbol: index for index, symbol in enumerate(labels)}
        sentence_words = known_words(lesson)
        flattened = [word for words in sentence_words for word in words]
        transcript = "|".join(flattened)
        targets = torch.tensor([[dictionary[character] for character in transcript]], dtype=torch.int32)
        paths, scores = torchaudio.functional.forced_align(log_probs, targets, blank=0)
        spans = torchaudio.functional.merge_tokens(paths[0], scores[0].exp(), blank=0)
        if "".join(labels[span.token] for span in spans) != transcript:
            raise AlignmentError("CTC 路径未覆盖完整已知文本；没有用均匀分段替代。")
        groups, current = [], []
        for span in spans:
            if labels[span.token] == "|":
                if not current:
                    raise AlignmentError("CTC 单词分隔路径无效。")
                groups.append(current)
                current = []
            else:
                current.append(span)
        if current:
            groups.append(current)
        if len(groups) != len(flattened):
            raise AlignmentError("CTC 单词覆盖不完整。")
        frame_count = log_probs.shape[1]
        frame_seconds = pcm["duration"] / frame_count
        words, cursor = [], 0
        for sentence_index, expected_words in enumerate(sentence_words):
            for sentence_word_index, expected in enumerate(expected_words):
                group = groups[cursor]
                if "".join(labels[span.token] for span in group) != expected:
                    raise AlignmentError("CTC 单词与已知文本不一致。")
                total_frames = sum(len(span) for span in group)
                words.append({"word_index": cursor, "sentence_index": sentence_index, "sentence_word_index": sentence_word_index, "text": expected, "start": round(group[0].start * frame_seconds, 6), "end": round(group[-1].end * frame_seconds, 6), "ctc_score": round(sum(span.score * len(span) for span in group) / total_frames, 6), "frame_start": group[0].start, "frame_end": group[-1].end})
                cursor += 1
        greedy = torch.argmax(log_probs[0], dim=-1)
        greedy_text = "".join(labels[index] for index in torch.unique_consecutive(greedy).tolist() if index != 0).replace("|", " ").strip()
        metadata = {"bundle": "WAV2VEC2_ASR_BASE_960H", "checkpoint_file": checkpoint.name, "checkpoint_source": MODEL_SOURCE, "checkpoint_sha256": file_sha256(checkpoint), "torch_version": torch.__version__, "torchaudio_version": torchaudio.__version__, "reference": MODEL_REFERENCE, "alignment_api": "torchaudio.functional.forced_align + merge_tokens", "ctc_sample_rate": bundle.sample_rate, "emission_frames": frame_count, "frame_seconds": frame_seconds}
        return words, greedy_text, metadata
    except AlignmentError:
        raise
    except Exception as exc:
        raise AlignmentError(f"真实声学对齐失败（{type(exc).__name__}）；未猜测时间或重新配音。") from exc


def edit_distance(first, second):
    row = list(range(len(second) + 1))
    for index, left in enumerate(first, 1):
        following = [index]
        for column, right in enumerate(second, 1):
            following.append(min(following[-1] + 1, row[column] + 1, row[column - 1] + (left != right)))
        row = following
    return row[-1]


def assemble_alignment(lesson, words, silences, duration, greedy_text):
    expected_sentences = known_words(lesson)
    expected = [(sentence_index, word) for sentence_index, children in enumerate(expected_sentences) for word in children]
    if len(words) != len(expected):
        raise AlignmentError("对齐单词数与原稿不一致。")
    previous_end = 0.0
    for index, (word, (sentence_index, text)) in enumerate(zip(words, expected)):
        if word.get("sentence_index") != sentence_index or word.get("text") != text or word.get("word_index") != index:
            raise AlignmentError("对齐单词或顺序与原稿不一致。")
        values = [word.get(key) for key in ("start", "end", "ctc_score")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise AlignmentError("词级时间或声学评分无效。")
        if not previous_end <= word["start"] < word["end"] <= duration or not 0 <= word["ctc_score"] <= 1:
            raise AlignmentError("词级时间越界、倒序或声学评分越界。")
        previous_end = word["end"]
    sentence_spans = []
    for index, sentence in enumerate(lesson["sentences"]):
        children = [word for word in words if word["sentence_index"] == index]
        sentence_spans.append({"sentence_index": index, "en": sentence["en"], "speech_start": children[0]["start"], "speech_end": children[-1]["end"], "word_start_index": children[0]["word_index"], "word_end_index": children[-1]["word_index"], "mean_ctc_word_score": sum(word["ctc_score"] for word in children) / len(children), "minimum_ctc_word_score": min(word["ctc_score"] for word in children)})
    boundaries, evidence = [0.0], []
    for index, (left, right) in enumerate(zip(sentence_spans, sentence_spans[1:])):
        if not left["speech_end"] < right["speech_start"]:
            raise AlignmentError("相邻句子的声学时间重叠，不能安全设置字幕切换。")
        candidates = []
        for silence in silences:
            start = max(silence["start"], left["speech_end"])
            end = min(silence["end"], right["speech_start"])
            if end - start >= 0.12:
                candidates.append((end - start, start, end, silence))
        if candidates:
            _, start, end, silence = max(candidates, key=lambda entry: entry[0])
            midpoint = (start + end) / 2
            item = {"method": "measured_pcm_silence_midpoint", "silence": silence, "usable_silence_start": start, "usable_silence_end": end}
        else:
            midpoint = (left["speech_end"] + right["speech_start"]) / 2
            item = {"method": "ctc_gap_midpoint_needs_review", "review_reason": "No measured waveform silence of at least 0.12 s between sentence anchors"}
        boundary = round(midpoint, 6)
        boundaries.append(boundary)
        evidence.append(dict(item, after_sentence_index=index, caption_boundary=boundary, left_ctc_last_word_end=left["speech_end"], right_ctc_first_word_start=right["speech_start"]))
    boundaries.append(duration)
    segments = [dict(span, start=boundaries[index], end=boundaries[index + 1]) for index, span in enumerate(sentence_spans)]
    if any(not segment["start"] <= segment["speech_start"] < segment["speech_end"] <= segment["end"] for segment in segments):
        raise AlignmentError("字幕页不能完整覆盖对应句子的声学范围。")
    letters = re.sub("[^A-Z]", "", " ".join(word for _, word in expected))
    greedy_letters = re.sub("[^A-Z]", "", greedy_text.upper())
    cer = edit_distance(letters, greedy_letters) / len(letters)
    mean_score = sum(word["ctc_score"] for word in words) / len(words)
    low_words = [word for word in words if word["ctc_score"] < 0.45]
    all_measured = all(item["method"] == "measured_pcm_silence_midpoint" for item in evidence)
    passed = mean_score >= 0.7 and not low_words and cer <= 0.1 and all_measured
    quality = {"status": "passed" if passed else "needs_review", "known_text_words": len(expected), "aligned_words": len(words), "word_coverage": 1.0, "mean_ctc_word_score": mean_score, "minimum_ctc_word_score": min(word["ctc_score"] for word in words), "low_score_threshold": 0.45, "low_score_words": low_words, "greedy_asr_text": greedy_text, "greedy_character_error_rate_without_spaces": cer, "greedy_check_use": "Independent acoustic plausibility check; original transcript remains authoritative", "all_boundaries_have_measured_silence": all_measured, "continuous_caption_timeline": True, "score_caveat": "Model scores are not calibrated probabilities of boundary accuracy", "manual_auditory_alignment_review": False}
    return {"schema_version": 1, "audio_duration": duration, "method": "Known-text CTC forced alignment + measured PCM silence midpoint subtitle boundaries", "quality": quality, "segments": segments, "words": words, "boundary_evidence": evidence, "silence_intervals": silences}


def parser():
    result = argparse.ArgumentParser(description="对已批准的整轨 WAV 做真实声学字幕对齐，不重新配音、不调整语速。")
    result.add_argument("--audio", required=True, help="单声道 16-bit PCM WAV")
    result.add_argument("--lesson", required=True, help="含 sentences[].en 的 UTF-8 JSON")
    result.add_argument("--output", required=True, help="输出 alignment.json")
    result.add_argument("--model-dir", help="官方模型缓存；默认 Skill .cache/alignment/models")
    result.add_argument("--offline", action="store_true", help="只使用已缓存模型，不下载")
    result.add_argument("--overwrite", action="store_true", help="覆盖已有对齐 JSON，永不覆盖输入文件")
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    temporary = None
    try:
        source, lesson_path, destination = (Path(value).expanduser().resolve() for value in (args.audio, args.lesson, args.output))
        if destination in (source, lesson_path):
            raise AlignmentError("输出路径不得覆盖已批准音轨或输入课件。")
        if destination.exists() and (not args.overwrite or not destination.is_file()):
            raise AlignmentError("输出对齐文件已存在；请选择新文件或 --overwrite。")
        before = file_sha256(source)
        pcm, lesson = read_pcm(source), read_lesson(lesson_path)
        model_dir = Path(args.model_dir).expanduser().resolve() if args.model_dir else Path(__file__).resolve().parents[1] / ".cache" / "alignment" / "models"
        began = time.perf_counter()
        words, greedy, model = infer_known_words(pcm, lesson, model_dir, args.offline)
        silences = detect_silences(pcm)
        result = assemble_alignment(lesson, words, silences, pcm["duration"], greedy)
        if before != file_sha256(source):
            raise AlignmentError("对齐过程中已批准音轨发生变化，停止发布。")
        english = json.dumps([sentence["en"] for sentence in lesson["sentences"]], ensure_ascii=False, separators=(",", ":"))
        spoken = json.dumps([sentence.get("spoken_text", sentence["en"]) for sentence in lesson["sentences"]], ensure_ascii=False, separators=(",", ":"))
        result.update(audio_file=source.name, audio_sha256=before, audio_sample_rate=pcm["sample_rate"], audio_samples=pcm["sample_count"], lesson_file=lesson_path.name, english_sha256=hashlib.sha256(english.encode("utf-8")).hexdigest(), source_audio_unchanged=True, model=model, alignment_runtime_seconds=time.perf_counter() - began)
        result["spoken_text_sha256"] = hashlib.sha256(spoken.encode("utf-8")).hexdigest()
        result["quality"]["source_hash_before_equals_after"] = True
        result["timing"] = {"frame_seconds": model.get("frame_seconds"), "conversion": "CTC frame index * original duration / emission frame count", "precision": "Acoustic CTC estimates at about 20 ms frame resolution; not phonetic ground truth", "subtitle_frame_quantization": "Not quantized here; renderer may choose nearest video frame without changing audio", "silence_threshold_db": -40, "silence_min_seconds": 0.16, "usable_boundary_silence_min_seconds": 0.12}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".alignment-", suffix=".json", dir=destination.parent, delete=False, mode="w", encoding="utf-8") as handle:
            temporary = Path(handle.name)
            json.dump(result, handle, ensure_ascii=False, indent=2)
        if args.overwrite:
            os.replace(temporary, destination)
        elif os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        temporary = None
        status = result["quality"]["status"]
        print(f"声学对齐 {status}：{len(result['segments'])} 句，{len(words)} 词，{pcm['duration']:.3f} 秒；原音轨未改变。", flush=True)
        return 0 if status == "passed" else 2
    except (AlignmentError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
