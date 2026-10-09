"""Behavior tests: audio is real PCM; only the external TTS call is replaced."""
import importlib
import json
import math
import shutil
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    speech = importlib.import_module("speech_pipeline")
except ModuleNotFoundError:
    speech = None


def write_tone(path, seconds=6.0, silent=False):
    values = array("h", (0 if silent else round(2400 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(round(seconds * 48000))))
    if sys.byteorder != "little":
        values.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(values.tobytes())


class SpeechBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(speech, "配音模块尚未实现；当前流程没有持久音频缓存与独立试听接口。")
        temporary_root = Path(__file__).resolve().parents[2] / "tmp"
        temporary_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="speech-test-", dir=temporary_root)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.work = self.folder / "work"
        self.work.mkdir()
        self.args = SimpleNamespace(tts="edge", voice="en-US-JennyNeural", audio_cache=str(self.folder / "cache"), kokoro_python=None)
        self.lesson = {"accent":"en-US", "target_wpm":100.0, "edge_rate":"-58%", "sapi_rate":-5, "pause":0.6, "repeats":1, "sentences":[{"en":"A little book is waiting on the shelf for me.","zh":"架子上有一本书。","ipa":"/ə ˈlɪtəl bʊk/"}]}
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and FFprobe required for real PCM integration tests")

    def fixture_synthesis(self, lesson, args, sources, work, native_rate):
        for path in sources:
            write_tone(path)
        return {"engine":"edge","voice":"en-US-JennyNeural","native_rate":native_rate}

    def prepare(self):
        return speech.prepare_audio(self.lesson, self.args, self.work, self.ffmpeg, self.ffprobe)

    def test_second_request_uses_verified_pcm_cache_without_calling_tts(self):
        with patch.object(speech, "_synthesize_sources", side_effect=self.fixture_synthesis) as synth:
            paths, durations, first = self.prepare()
            self.assertFalse(first["cache_hit"])
            self.assertAlmostEqual(durations[0], 6.0, delta=0.1)
            original = paths[0].read_bytes()
            paths[0].unlink()
            paths2, _, second = self.prepare()
            self.assertTrue(second["cache_hit"])
            self.assertEqual(paths2[0].read_bytes(), original)
            self.assertEqual(synth.call_count, 1, "Cache hit must make zero new external speech requests")
            self.assertEqual(len(second["audio_sha256"][0]), 64)

    def test_display_changes_and_repeat_count_reuse_audio(self):
        with patch.object(speech, "_synthesize_sources", side_effect=self.fixture_synthesis) as synth:
            _, _, first = self.prepare()
            self.lesson["sentences"][0].update(zh="改进翻译", ipa="/改正展示音标/")
            self.lesson.update(repeats=2, width=1080, height=1920)
            _, _, second = self.prepare()
            self.assertTrue(second["cache_hit"])
            self.assertEqual(first["cache_key"], second["cache_key"])
            self.assertEqual(synth.call_count, 1)

    def test_voice_rate_pause_and_text_changes_invalidate_cache(self):
        with patch.object(speech, "_synthesize_sources", side_effect=self.fixture_synthesis):
            _, _, previous = self.prepare()
            changes = [lambda: setattr(self.args, "voice", "en-US-AriaNeural"), lambda: self.lesson.update(edge_rate="-60%"), lambda: self.lesson.update(pause=0.65), lambda: self.lesson["sentences"][0].update(en="A small book is waiting on the shelf for me.")]
            for change in changes:
                change()
                _, _, current = self.prepare()
                self.assertFalse(current["cache_hit"])
                self.assertNotEqual(previous["cache_key"], current["cache_key"])
                previous = current

    def test_unsupported_phoneme_or_context_controls_are_not_silently_ignored(self):
        self.lesson["pronunciation_overrides"] = {"book":"bʊk"}
        with patch.object(speech,"_synthesize_sources",side_effect=self.fixture_synthesis) as synth:
            with self.assertRaises(speech.RenderError):
                self.prepare()
            self.assertEqual(synth.call_count,0)
        self.lesson.pop("pronunciation_overrides")
        self.lesson["speech_context"] = {"previous_text":"Yesterday."}
        with self.assertRaises(speech.RenderError):
            self.prepare()

    def test_corrupt_cached_audio_is_regenerated(self):
        with patch.object(speech, "_synthesize_sources", side_effect=self.fixture_synthesis) as synth:
            _, _, first = self.prepare()
            cache = Path(self.args.audio_cache) / first["cache_key"]
            (cache / "speech-000.wav").write_bytes(b"not audio")
            paths, _, second = self.prepare()
            self.assertFalse(second["cache_hit"])
            self.assertGreater(paths[0].stat().st_size, 1000)
            self.assertEqual(synth.call_count, 2)

    def test_explicit_engine_failure_never_publishes_fake_or_silent_audio(self):
        with patch.object(speech, "_synthesize_sources", side_effect=speech.RenderError("真实后端失败")):
            with self.assertRaises(speech.RenderError):
                self.prepare()
        self.assertFalse(list(Path(self.args.audio_cache).glob("*/metadata.json")))

    def test_silent_source_cannot_be_cached_as_audition(self):
        def silent(lesson, args, sources, work, native_rate):
            for path in sources:
                write_tone(path, silent=True)
            return {"engine":"edge","voice":"test","native_rate":native_rate}
        with patch.object(speech, "_synthesize_sources", side_effect=silent):
            with self.assertRaises(speech.RenderError):
                self.prepare()
        self.assertFalse(list(Path(self.args.audio_cache).glob("*/metadata.json")))

    def test_float_speed_model_contract_rejects_integer_and_accepts_duration_graph(self):
        integer = [{"name":"speed","type":"tensor(int32)"}]
        float_ = [{"name":"speed","type":"tensor(float)"}]
        with self.assertRaises(speech.RenderError):
            speech.validate_model_contract(integer, ["audio", "duration"])
        info = speech.validate_model_contract(float_, ["audio", "duration"])
        self.assertEqual(info["speed_input"], "tensor(float)")
        self.assertTrue(info["has_duration"])

    def test_changed_model_bytes_are_part_of_cache_identity(self):
        model, voices = self.folder / "model.onnx", self.folder / "voices.bin"
        model.write_bytes(b"model one")
        voices.write_bytes(b"voice data")
        self.args.tts = "kokoro"
        self.args.voice = "af_heart"
        self.args.kokoro_model = str(model)
        self.args.kokoro_voices = str(voices)
        one = speech.request_spec(self.lesson, self.args)
        model.write_bytes(b"model two")
        two = speech.request_spec(self.lesson, self.args)
        self.assertNotEqual(speech.cache_key(one), speech.cache_key(two))

    def test_dependency_temp_files_stay_in_writable_cache_and_restore_process_setting(self):
        original = tempfile.tempdir
        root = self.folder / "cache"
        self.assertTrue(callable(getattr(speech, "_dependency_tempdir", None)), "依赖临时目录尚未隔离到可写工作区。")
        with speech._dependency_tempdir(root):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary)
                self.assertIn(root.resolve(), path.resolve().parents)
                (path / "native-library-test.bin").write_bytes(b"test")
        self.assertEqual(tempfile.tempdir, original)

    def test_audition_publishes_playable_compressed_audio_and_reusable_bundle(self):
        output = self.folder / "audition"
        self.args.fps = 24
        self.args.overwrite = False
        with patch.object(speech, "_synthesize_sources", side_effect=self.fixture_synthesis):
            speech.audition(self.lesson,self.args,self.folder/"input.json",output)
        bundle = json.loads((output/"bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["schema_version"],1)
        self.assertEqual(bundle["sentences"][0]["file"],"speech-000.wav")
        self.assertGreater((output/"speech-000.wav").stat().st_size,1000)
        duration, streams = speech.helper.probe(output/"narration.mp3",self.ffprobe)
        self.assertAlmostEqual(duration,6.6,delta=0.15)
        self.assertEqual(streams["streams"][0]["codec_name"],"mp3")
        with wave.open(str(output/"narration.wav"),"rb") as narration:
            values = array("h",narration.readframes(narration.getnframes()))
        if sys.byteorder != "little":
            values.byteswap()
        rms_db = 20 * math.log10(math.sqrt(sum(value*value for value in values)/len(values))/32768)
        self.assertAlmostEqual(rms_db,-22.0,delta=0.1, msg="Candidate audition tracks must use matching audible level")


if __name__ == "__main__":
    unittest.main()
