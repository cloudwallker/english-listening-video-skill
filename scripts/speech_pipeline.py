#!/usr/bin/env python3
"""Slow speech, verified PCM caching and an audio-only audition CLI.

Kokoro runs on CPU in an optional isolated Python environment. An explicit
engine never changes voice on failure. Cached audio is verified before reuse.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager


def _helpers():
    # The renderer imports this module from inside its render function. Using
    # its live module keeps the exact same RenderError class for CLI handling.
    for name in ("render_video", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "pcm_info") and hasattr(module, "RenderError"):
            return module
    import render_video
    return render_video


helper = _helpers()
RenderError = helper.RenderError
PROCESSING_VERSION = "speech-v2.1:trim50db:pcm48k:atempo-measured"
TRIM_FILTER = "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,areverse"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def cache_key(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def request_spec(lesson, args):
    engine = getattr(args, "tts", "edge")
    if engine not in ("edge", "sapi", "auto", "kokoro"):
        raise RenderError("配音引擎必须是 edge / sapi / kokoro / auto。")
    accent = lesson.get("accent", "en-US")
    voice = getattr(args, "voice", None) or lesson.get("voice")
    if not voice:
        voice = ({"en-US":"af_heart", "en-GB":"bf_emma"} if engine == "kokoro" else {"en-US":"en-US-JennyNeural", "en-GB":"en-GB-SoniaNeural"}).get(accent) if engine != "sapi" else None
    spoken = []
    for index, item in enumerate(lesson["sentences"]):
        spoken.append(helper.nonempty(item.get("spoken_text", item["en"]), f"sentences[{index}].spoken_text"))
    spec = {"processing_version":PROCESSING_VERSION, "engine":engine, "voice":voice, "accent":accent, "spoken_text":spoken, "target_wpm":float(lesson["target_wpm"]), "pause":float(lesson["pause"]), "pronunciation_overrides":lesson.get("pronunciation_overrides", {}), "context":lesson.get("speech_context", {}), "normalization_version":lesson.get("normalization_version", "literal-en-v1")}
    if not 60 <= spec["target_wpm"] <= 160 or not math.isfinite(spec["target_wpm"]):
        raise RenderError("目标正文词速必须在 60–160 WPM 之间。")
    if not 0.45 <= spec["pause"] <= 0.7 or not math.isfinite(spec["pause"]):
        raise RenderError("句间停顿必须在 0.45–0.7 秒之间。")
    if engine == "kokoro":
        model = Path(getattr(args, "kokoro_model", "") or "")
        voices = Path(getattr(args, "kokoro_voices", "") or "")
        if not model.is_file() or not voices.is_file():
            raise RenderError("Kokoro 需要本地 --kokoro-model 与 --kokoro-voices 文件；不会自动换成其他声音。")
        speed = getattr(args, "kokoro_speed", None)
        speed = speed if speed is not None else max(0.5, min(2.0, spec["target_wpm"] / 180.0))
        if not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.5 <= speed <= 2:
            raise RenderError("Kokoro speed 必须在 0.5–2.0 之间。")
        spec.update(native_rate=float(speed), model_sha256=sha256_file(model), voices_sha256=sha256_file(voices), engine_version=_version("kokoro-onnx"), phonemizer_version=_version("phonemizer"), onnxruntime_version=_version("onnxruntime"), espeak_loader_version=_version("espeakng-loader"))
        data_path = getattr(args,"espeak_data_path",None)
        if data_path:
            data = Path(data_path)
            if not data.is_dir() or not (data/"phontab").is_file():
                raise RenderError("--espeak-data-path 必须指向包含 phontab 的 eSpeak 数据目录。")
            spec["espeak_data_fingerprint"] = cache_key({name:sha256_file(data/name) for name in ("phontab","phondata","phonindex","intonations","en_dict") if (data/name).is_file()})
    elif engine == "sapi":
        spec.update(native_rate=int(lesson.get("sapi_rate", -5)), engine_version="Windows-System.Speech-v1")
    else:
        spec.update(native_rate=lesson.get("edge_rate", "-58%"), engine_version=_version("edge-tts"))
        if engine == "auto":
            spec["fallback_rate"] = int(lesson.get("sapi_rate", -5))
    return spec


def validate_model_contract(inputs, outputs):
    data = {item["name"]:item["type"] for item in inputs}
    speed_type = data.get("speed", "missing")
    if speed_type not in ("tensor(float)", "tensor(double)", "tensor(float16)"):
        raise RenderError("Kokoro 模型 speed 输入必须为浮点；旧整数导出不能可靠进行原生慢读。请使用 model-files-v1.1 中的英文 v1.0 新图。")
    return {"speed_input":speed_type, "has_duration":"duration" in outputs}


def _synthesize_sources(lesson, args, sources, work, native_rate):
    engine = getattr(args, "tts", "edge")
    voice = getattr(args, "voice", None) or lesson.get("voice")
    if engine == "kokoro":
        try:
            import onnxruntime as ort
            import soundfile as sf
            from kokoro_onnx import Kokoro
            from kokoro_onnx.config import EspeakConfig
            import espeakng_loader
        except ImportError as exc:
            raise RenderError("Kokoro 依赖缺失；安装 requirements-kokoro.txt，或用 --kokoro-python 指向独立 Python 3.10–3.13 环境。") from exc
        voice = voice or {"en-US":"af_heart", "en-GB":"bf_emma"}.get(lesson["accent"])
        if not voice:
            raise RenderError("该口音需要显式指定 Kokoro 声音。")
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 4
        session_options.inter_op_num_threads = 1
        loaded = time.perf_counter()
        try:
            session = ort.InferenceSession(str(args.kokoro_model), sess_options=session_options, providers=["CPUExecutionProvider"])
            contract = validate_model_contract([{"name":item.name,"type":item.type} for item in session.get_inputs()], [item.name for item in session.get_outputs()])
            if not contract["has_duration"]:
                raise RenderError("该 Kokoro 图缺少 duration 输出；本版本要求已验证的 model-files-v1.1 英文模型。")
            data_path = getattr(args,"espeak_data_path",None) or espeakng_loader.get_data_path()
            if os.name == "nt" and not str(data_path).isascii():
                raise RenderError("Windows eSpeak 数据路径含非 ASCII 字符；请复制 espeak-ng-data 到可写 ASCII 目录并用 --espeak-data-path 指定。模型和 Python 环境无需移动。")
            espeak_config = EspeakConfig(lib_path=espeakng_loader.get_library_path(), data_path=str(data_path))
            model = Kokoro.from_session(session, str(args.kokoro_voices), espeak_config=espeak_config)
            if voice not in model.get_voices():
                raise RenderError(f"Kokoro voice {voice} 不在所选 voices 文件中。")
            load_seconds = time.perf_counter() - loaded
            synthesized = time.perf_counter()
            phoneme_counts = []
            for index, (sentence, destination) in enumerate(zip(lesson["sentences"], sources)):
                print(f"配音 {index + 1}/{len(sources)}（kokoro CPU，speed={native_rate:.4f}）", flush=True)
                samples, sample_rate, phoneme_timings = model.create_timed(sentence["en"], voice=voice, speed=float(native_rate), lang=lesson["accent"].lower(), trim=True, sentence_pause=0, clause_pause=0.1)
                sf.write(str(destination), samples, sample_rate, subtype="PCM_16")
                phoneme_counts.append(len(phoneme_timings))
            return {"engine":"kokoro","voice":voice,"native_rate":float(native_rate),"model_contract":contract,"providers":session.get_providers(),"model_load_seconds":round(load_seconds,6),"native_generation_seconds":round(time.perf_counter()-synthesized,6),"timing_mode":"sentence PCM measurement; phoneme prediction counts retained, no invented word boundaries","phoneme_timing_counts":phoneme_counts}
        except RenderError:
            raise
        except Exception as exc:
            raise RenderError(f"Kokoro 真实合成失败：{type(exc).__name__}：{str(exc)[-500:]}。未用静音或其他引擎替代。") from exc
    if engine in ("edge", "auto"):
        selected = voice or {"en-US":"en-US-JennyNeural", "en-GB":"en-GB-SoniaNeural"}.get(lesson["accent"])
        if not selected:
            raise RenderError("该口音需要显式指定 Edge 声音。")
        try:
            asyncio.run(helper.edge_synthesize(lesson["sentences"], sources, selected, native_rate))
            return {"engine":"edge","voice":selected,"native_rate":native_rate}
        except RenderError:
            if engine != "auto" or os.name != "nt":
                raise
            print("按显式 --tts auto 回退 Windows 声音；记录引擎与新声线。", flush=True)
            selected = helper.sapi_synthesize(lesson["sentences"], sources, None, lesson["accent"], work, lesson.get("sapi_rate", -5))
            return {"engine":"sapi","voice":selected,"native_rate":lesson.get("sapi_rate", -5),"fallback_from":"edge"}
    selected = helper.sapi_synthesize(lesson["sentences"], sources, voice, lesson["accent"], work, native_rate)
    return {"engine":"sapi","voice":selected,"native_rate":native_rate}


def _read_cache(folder, key, count, work, ffprobe):
    try:
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        if metadata["cache_key"] != key or len(metadata["audio_sha256"]) != count:
            return None
        paths, durations = [], []
        for index in range(count):
            source = folder / f"speech-{index:03d}.wav"
            if sha256_file(source) != metadata["audio_sha256"][index]:
                return None
            helper.pcm_info(source)
            duration, _ = helper.probe(source, ffprobe)
            if abs(duration - metadata["durations"][index]) > 0.002:
                return None
            paths.append(source)
            durations.append(duration)
        copies = []
        for index, path in enumerate(paths):
            destination = work / f"speech-{index:03d}.wav"
            shutil.copyfile(path, destination)
            copies.append(destination)
        metadata.update(cache_hit=True, native_synthesis_passes_this_run=0, generation_seconds_this_run=0.0)
        return copies, durations, metadata
    except (OSError, ValueError, KeyError, TypeError, RenderError):
        return None


def _save_cache(folder, key, paths, metadata):
    folder.mkdir(parents=True, exist_ok=True)
    # Manifest is the commit marker. Never expose a successful manifest before
    # every PCM file is complete; parallel identical writers have equal hashes.
    for index, source in enumerate(paths):
        destination = folder / f"speech-{index:03d}.wav"
        temporary = folder / f".{index:03d}-{os.getpid()}.wav"
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    temporary = folder / f".metadata-{os.getpid()}.json"
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, folder / "metadata.json")


@contextmanager
def _dependency_tempdir(cache_root):
    # phonemizer copies its Windows DLL to tempfile's root and retains its
    # handle until process exit. Keep this outside the audition's temporary
    # folder, and do not alter user or machine environment variables.
    directory = Path(cache_root).resolve() / "runtime-temp"
    directory.mkdir(parents=True, exist_ok=True)
    previous = tempfile.tempdir
    tempfile.tempdir = str(directory)
    try:
        yield directory
    finally:
        tempfile.tempdir = previous


def _external_kokoro(lesson, args, work, ffmpeg, ffprobe):
    request = work / "kokoro-worker-request.json"
    result = work / "kokoro-worker-result.json"
    allowed = ("tts", "voice", "kokoro_model", "kokoro_voices", "kokoro_speed", "audio_cache", "espeak_data_path")
    values = {name:getattr(args,name,None) for name in allowed}
    values["kokoro_python"] = None
    request.write_text(json.dumps({"lesson":lesson,"args":values,"work":str(work),"ffmpeg":str(ffmpeg),"ffprobe":str(ffprobe),"result":str(result)}, ensure_ascii=False), encoding="utf-8")
    helper.run([args.kokoro_python, "-X", "utf8", Path(__file__).resolve(), "_worker", request], timeout=max(300, len(lesson["sentences"]) * 120))
    try:
        payload = json.loads(result.read_text(encoding="utf-8"))
        paths = [Path(path) for path in payload["paths"]]
        for path in paths:
            helper.pcm_info(path)
        return paths, payload["durations"], payload["metadata"]
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise RenderError("独立 Kokoro 进程未返回可验证的音频结果。") from exc


def prepare_audio(lesson, args, work, ffmpeg, ffprobe):
    """Return copied PCM files, measured durations and complete voice metadata."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if lesson.get("pronunciation_overrides") or lesson.get("speech_context"):
        raise RenderError("当前免费配音后端尚未接入显式音素或前后文控制；不接受被忽略的 pronunciation_overrides / speech_context。请先校正英文实际朗读文本。")
    if getattr(args,"tts","edge") == "kokoro" and getattr(args,"kokoro_python",None) and Path(args.kokoro_python).resolve() != Path(sys.executable).resolve():
        return _external_kokoro(lesson,args,work,ffmpeg,ffprobe)
    spec = request_spec(lesson, args)
    key = cache_key(spec)
    cache_root = Path(getattr(args,"audio_cache",None) or Path(__file__).resolve().parents[1] / ".cache" / "audio").expanduser().resolve()
    folder = cache_root / key
    cached = _read_cache(folder,key,len(spec["spoken_text"]),work,ffprobe)
    if cached is not None:
        print(f"音频缓存命中 {key[:12]}；本次 TTS 请求 0。",flush=True)
        return cached
    active = SimpleNamespace(**vars(args))
    working_lesson = dict(lesson)
    working_lesson["sentences"] = [dict(sentence,en=spoken) for sentence,spoken in zip(lesson["sentences"],spec["spoken_text"])]
    total_words = sum(helper.word_count(text) for text in spec["spoken_text"])
    if total_words < 1:
        raise RenderError("朗读正文没有英文单词。")
    native_rate = spec["native_rate"]
    measured_attempts = []
    started = time.perf_counter()
    for attempt in range(2):
        extension = "mp3" if active.tts in ("edge","auto") else "wav"
        sources = [work / f"native-{attempt}-{index:03d}.{extension}" for index in range(len(spec["spoken_text"]))]
        with _dependency_tempdir(cache_root):
            native = _synthesize_sources(working_lesson,active,sources,work,native_rate)
        native_rate = native["native_rate"]
        active.tts = native["engine"]
        active.voice = native["voice"]
        normalized, source_durations = [], []
        for index, source in enumerate(sources):
            if not source.is_file() or source.stat().st_size < 128:
                raise RenderError(f"第 {index+1} 句真实配音未生成；停止，不替代静音。")
            helper.probe(source,ffprobe)
            clean = work / f"clean-{attempt}-{index:03d}.wav"
            helper.run([ffmpeg,"-hide_banner","-loglevel","error","-y","-i",source,"-af",TRIM_FILTER,"-ar",48000,"-ac",1,"-c:a","pcm_s16le",clean])
            helper.pcm_info(clean)
            duration,_ = helper.probe(clean,ffprobe)
            source_durations.append(duration)
            normalized.append(clean)
        source_wpm = total_words / sum(source_durations) * 60
        tempo = spec["target_wpm"] / source_wpm
        measured_attempts.append({"native_rate":native_rate,"source_wpm":source_wpm,"atempo_required":tempo})
        if 0.85 <= tempo <= 1.15 or attempt == 1:
            break
        if active.tts == "kokoro":
            revised = max(0.5,min(2.0,native_rate * tempo))
        elif active.tts == "edge":
            percent = int(str(native_rate)[:-1])
            revised = f"{max(-85,min(40,round(((1+percent/100)*tempo-1)*100))):+d}%"
        else:
            revised = max(-10,min(10,native_rate+round(6*math.log2(tempo))))
        if revised == native_rate:
            break
        native_rate = revised
        print(f"原生正文 {source_wpm:.1f} WPM；先校准引擎速率 {native_rate}，再少量保调速度调整。",flush=True)
    if not 0.75 <= tempo <= 1.25:
        raise RenderError(f"原生正文 {source_wpm:.1f} WPM，与目标仍相差过大（atempo={tempo:.3f}）；需修改原生速率或声音。未生成伪造试听。")
    paths,durations = [],[]
    for index, source in enumerate(normalized):
        destination = work / f"speech-{index:03d}.wav"
        helper.run([ffmpeg,"-hide_banner","-loglevel","error","-y","-i",source,"-af",helper.tempo_filter(tempo),"-ar",48000,"-ac",1,"-c:a","pcm_s16le",destination])
        helper.pcm_info(destination)
        duration,_ = helper.probe(destination,ffprobe)
        paths.append(destination)
        durations.append(duration)
    elapsed = time.perf_counter()-started
    metadata = dict(native,cache_key=key,cache_hit=False,request=spec,source_wpm=source_wpm,atempo=tempo,target_wpm=spec["target_wpm"],actual_speech_wpm=total_words/sum(durations)*60,words_per_pass=total_words,native_synthesis_passes=len(measured_attempts),native_synthesis_passes_this_run=len(measured_attempts),native_calibration=measured_attempts,preferred_tempo_range=0.85<=tempo<=1.15,hard_tempo_range=True,generation_seconds=round(elapsed,6),generation_seconds_this_run=round(elapsed,6),durations=durations,audio_sha256=[sha256_file(path) for path in paths],runtime_python=sys.version.split()[0])
    _save_cache(folder,key,paths,metadata)
    return paths,durations,metadata


def _worker(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    paths,durations,metadata = prepare_audio(payload["lesson"],SimpleNamespace(**payload["args"]),Path(payload["work"]),payload["ffmpeg"],payload["ffprobe"])
    Path(payload["result"]).write_text(json.dumps({"paths":[str(p) for p in paths],"durations":durations,"metadata":metadata},ensure_ascii=False,indent=2),encoding="utf-8")


def prepare_natural_audio(renderer, args, lesson, work_dir):
    """One native Edge utterance. Preserve all source pauses; never time-stretch.

    A single constant-gain loudness adjustment is applied before audition.
    The resulting WAV is the approval artifact; later rendering must copy it.
    """
    if getattr(args, "tts", "edge") != "edge":
        raise RenderError("natural 整段模式当前支持 Edge；其他引擎请显式使用 --speech-mode calibrated。")
    if lesson.get("repeats", 1) != 1:
        raise RenderError("natural 试听保留单遍原轨；repeats 必须为 1。")
    if lesson.get("pronunciation_overrides") or lesson.get("speech_context"):
        raise RenderError("Edge 尚未接入显式音素或前后文控制；请校正实际朗读英文。")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = renderer.require_binary("ffmpeg"), renderer.require_binary("ffprobe")
    text = " ".join(sentence.get("spoken_text", sentence["en"]) for sentence in lesson["sentences"])
    words = renderer.word_count(text)
    voice = getattr(args, "voice", None) or lesson.get("voice") or {
        "en-US": "en-US-JennyNeural", "en-GB": "en-GB-SoniaNeural"
    }.get(lesson["accent"])
    if not voice:
        raise RenderError("该口音需要显式指定 Edge voice。")
    rate = getattr(args, "edge_rate", None) or lesson.get("edge_rate", "-20%")
    spec = {"processing_version": "natural-v1:whole-native:no-trim:no-stretch:gain-once",
            "engine": "edge", "voice": voice, "accent": lesson["accent"],
            "spoken_text": text, "native_rate": rate, "engine_version": _version("edge-tts")}
    key = cache_key(spec)
    cache_root = Path(getattr(args, "audio_cache", None) or
                      Path(__file__).resolve().parents[1] / ".cache" / "audio").expanduser().resolve()
    cached = _read_cache(cache_root / key, key, 1, work, ffprobe)
    wav = work / "narration.wav"
    began = time.perf_counter()
    if cached is not None:
        paths, durations, metadata = cached
        shutil.copyfile(paths[0], wav)
        duration = durations[0]
        print(f"整轨缓存命中 {key[:12]}；本次 TTS 请求 0。", flush=True)
    else:
        native = work / "native-source.mp3"
        asyncio.run(renderer.edge_synthesize([{"en": text}], [native], voice, rate))
        renderer.probe(native, ffprobe)
        renderer.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", native,
                      "-ar", 48000, "-ac", 1, "-c:a", "pcm_s16le", wav])
        count, _ = renderer.pcm_info(wav)
        loudness = renderer.normalize_soundtrack(wav)
        final_count, _ = renderer.pcm_info(wav)
        if count != final_count:
            raise RenderError("试听响度调整改变了原轨时长；停止交付。")
        duration = final_count / renderer.SAMPLE_RATE
        metadata = {"engine": "edge", "voice": voice, "native_rate": rate,
                    "mode": "natural", "atempo": None, "post_speed_adjustment_applied": False,
                    "repeats": 1, "added_sentence_pause_seconds": 0,
                    "source_synthesis_requests": 1, "whole_text_single_request": True,
                    "source_sentence_count": len(lesson["sentences"]), "words": words,
                    "words_per_pass": words, "duration": duration,
                    "actual_wpm_with_native_pauses": words / duration * 60,
                    "english_sha256": renderer.english_digest(lesson),
                    "full_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "loudness": loudness, "cache_key": key, "cache_hit": False,
                    "request": spec, "durations": [duration],
                    "audio_sha256": [sha256_file(wav)],
                    "generation_seconds": round(time.perf_counter() - began, 6)}
        _save_cache(cache_root / key, key, [wav], metadata)
    # Do not normalize a cache hit again. Only its compressed preview is rebuilt.
    mp3 = work / "narration.mp3"
    metadata["preview_encoder"] = renderer.encode_audio_preview(ffmpeg, wav, mp3)
    for path in (wav, mp3):
        renderer.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
                      "-f", "null", "-"])
    metadata.update(audio_sha256=[sha256_file(wav)],
                    english_sha256=renderer.english_digest(lesson),
                    source_sentence_count=len(lesson["sentences"]),
                    generation_seconds_this_run=round(time.perf_counter() - began, 6))
    (work / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (work / "en.txt").write_text(text + "\n", encoding="utf-8")
    return {"audio_path": wav, "mp3_path": mp3, "duration": duration,
            "audio_metadata": metadata, "audio_sha256": sha256_file(wav)}


def natural_audition(lesson, args, source, output):
    output.mkdir(parents=True, exist_ok=True)
    files = ("narration.wav", "narration.mp3", "metadata.json", "lesson.json", "en.txt")
    with helper.output_lock(output), tempfile.TemporaryDirectory(prefix="._natural-", dir=output) as temporary:
        for name in files:
            if (output / name).resolve() == source:
                raise RenderError("试听输出不得覆盖输入 lesson.json。")
            if (output / name).exists() and not args.overwrite:
                raise RenderError(f"试听文件 {name} 已存在，请使用新目录或 --overwrite。")
        work = Path(temporary)
        result = prepare_natural_audio(helper, args, lesson, work)
        (work / "lesson.json").write_text(json.dumps(lesson, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in files:
            if args.overwrite:
                os.replace(work / name, output / name)
            else:
                os.rename(work / name, output / name)
    metadata = result["audio_metadata"]
    print(f"试听已输出：{output / 'narration.mp3'}", flush=True)
    print(f"{metadata['voice']} / 原生 {metadata['native_rate']}，含自然停顿 "
          f"{metadata['actual_wpm_with_native_pauses']:.2f} WPM，{result['duration']:.2f} 秒。", flush=True)
    return metadata


def audition(lesson,args,source,output):
    if getattr(args, "speech_mode", "calibrated") == "natural":
        return natural_audition(lesson, args, source, output)
    ffmpeg,ffprobe = helper.require_binary("ffmpeg"),helper.require_binary("ffprobe")
    output.mkdir(parents=True,exist_ok=True)
    protected = ("narration.wav","narration.mp3","metadata.json","timeline.json","lesson.json","bundle.json") + tuple(f"speech-{index:03d}.wav" for index in range(len(lesson["sentences"])))
    with helper.output_lock(output), tempfile.TemporaryDirectory(prefix="._audition-",dir=output) as temporary:
        for filename in protected:
            destination = output/filename
            if destination.resolve() == source:
                raise RenderError("试听输出不得覆盖输入 lesson.json。")
            if destination.exists() and not args.overwrite:
                raise RenderError(f"试听文件 {filename} 已存在，请使用新目录或 --overwrite。")
        work = Path(temporary)
        paths,durations,metadata = prepare_audio(lesson,args,work,ffmpeg,ffprobe)
        soundtrack,frames,entries,total = helper.make_soundtrack(lesson,paths,durations,work,args.fps)
        metadata["loudness"] = helper.normalize_soundtrack(soundtrack)
        measured,_ = helper.probe(soundtrack,ffprobe)
        if abs(measured-total)>0.002:
            raise RenderError("试听音轨与句时间轴不一致。")
        metadata["preview_encoder"] = helper.encode_audio_preview(ffmpeg,soundtrack,work/"narration.mp3")
        helper.probe(work/"narration.mp3",ffprobe)
        metadata.update(actual_wpm_with_pauses=metadata["words_per_pass"]*lesson["repeats"]/measured*60,soundtrack_duration=measured,role="same-script slow audition")
        (work/"metadata.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
        timeline = {"schema_version":2,"mode":"audio_only_slow_story","audio":metadata,"repeats":lesson["repeats"],"fps":args.fps,"soundtrack_duration":measured,"segments":entries}
        (work/"timeline.json").write_text(json.dumps(timeline,ensure_ascii=False,indent=2),encoding="utf-8")
        (work/"lesson.json").write_text(json.dumps(lesson,ensure_ascii=False,indent=2),encoding="utf-8")
        english_sha = hashlib.sha256(json.dumps([sentence["en"] for sentence in lesson["sentences"]],ensure_ascii=False,separators=(",",":")).encode("utf-8")).hexdigest()
        bundle = {"schema_version":1,"english_sha256":english_sha,"accent":lesson["accent"],"target_wpm":lesson["target_wpm"],"audio":metadata,"sentences":[{"file":path.name,"sha256":sha256_file(path),"duration":duration} for path,duration in zip(paths,durations)]}
        (work/"bundle.json").write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding="utf-8")
        for filename in protected:
            if args.overwrite:
                os.replace(work/filename,output/filename)
            else:
                os.rename(work/filename,output/filename)
    print(f"试听已输出：{output/'narration.mp3'}",flush=True)
    print(f"{metadata['engine']} / {metadata['voice']}，正文 {metadata['actual_speech_wpm']:.2f} WPM，含停顿 {metadata['actual_wpm_with_pauses']:.2f} WPM，{measured:.2f} 秒，cache_hit={metadata['cache_hit']}",flush=True)
    return metadata


def parser():
    result = argparse.ArgumentParser(description="同稿慢速试听：真实配音、实测 WPM、持久 PCM 缓存，不渲染视频。")
    result.add_argument("--lesson",required=True)
    result.add_argument("--output-dir",required=True)
    result.add_argument("--engine","--tts",dest="tts",choices=("edge","kokoro","sapi","auto"),default="edge")
    result.add_argument("--speech-mode", choices=("natural", "calibrated"), default="natural")
    result.add_argument("--voice")
    result.add_argument("--target-wpm",type=float)
    result.add_argument("--edge-rate")
    result.add_argument("--sapi-rate",type=int)
    result.add_argument("--pause",type=float)
    result.add_argument("--repeats",type=int,default=1)
    result.add_argument("--kokoro-model","--model",dest="kokoro_model")
    result.add_argument("--kokoro-voices","--voices",dest="kokoro_voices")
    result.add_argument("--kokoro-speed",type=float)
    result.add_argument("--kokoro-python","--runtime-python",dest="kokoro_python")
    result.add_argument("--espeak-data-path",help="Windows eSpeak 数据目录；含中文路径时指定纯 ASCII 目录")
    result.add_argument("--audio-cache")
    result.add_argument("--fps",type=int,choices=(24,25,30),default=24)
    result.add_argument("--overwrite",action="store_true")
    return result


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        if len(sys.argv)>1 and sys.argv[1]=="_worker":
            _worker(sys.argv[2])
            return 0
        args = parser().parse_args()
        source = Path(args.lesson).expanduser().resolve()
        lesson = helper.load_lesson(source,args)
        audition(lesson,args,source,Path(args.output_dir).expanduser().resolve())
        return 0
    except (RenderError,OSError,ValueError) as exc:
        print(f"错误：{exc}",file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断，未确认成功产物。",file=sys.stderr)
        return 130


if __name__=="__main__":
    sys.exit(main())
