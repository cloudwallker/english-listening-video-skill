"""Whole-paragraph native speech must preserve pauses and never time-stretch."""
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
import speech_pipeline as speech


class NaturalSpeechTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[2] / "tmp"
        temp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.args = SimpleNamespace(tts="edge", voice=None, edge_rate=None,
                                    speech_mode="natural", audio_cache=str(self.root / "cache"))
        self.lesson = {"accent": "en-US", "edge_rate": "-20%", "repeats": 1,
                       "sentences": [{"en": "I open a book.", "zh": "我打开一本书。", "ipa": "/aɪ/"},
                                     {"en": "The story begins.", "zh": "故事开始了。", "ipa": "/ðə/"}]}
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg and FFprobe required")

    async def source(self, sentences, destinations, voice, rate):
        self.assertEqual([s["en"] for s in sentences], ["I open a book. The story begins."])
        self.assertEqual(len(destinations), 1)
        self.assertEqual(rate, "-20%")
        # A real PCM fixture with both leading/trailing and internal silence.
        values = array("h", (round(2200 * math.sin(2 * math.pi * 330 * i / 48000))
                             if 24000 <= i < 72000 or 120000 <= i < 168000 else 0
                             for i in range(192000)))
        if sys.byteorder != "little":
            values.byteswap()
        with wave.open(str(destinations[0]), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(values.tobytes())

    def prepare(self):
        return speech.prepare_natural_audio(speech.helper, self.args, self.lesson, self.work)

    def test_one_request_preserves_full_duration_and_pauses(self):
        with patch.object(speech.helper, "edge_synthesize", side_effect=self.source) as synth, \
             patch.object(speech.helper, "tempo_filter", side_effect=AssertionError("time stretch forbidden")), \
             patch.object(speech.helper, "make_soundtrack", side_effect=AssertionError("sentence assembly forbidden")):
            result = self.prepare()
        self.assertEqual(synth.call_count, 1)
        self.assertAlmostEqual(result["duration"], 4.0, places=4)
        with wave.open(str(result["audio_path"]), "rb") as audio:
            pcm = array("h", audio.readframes(audio.getnframes()))
        self.assertTrue(all(v == 0 for v in pcm[:24000]))
        self.assertTrue(all(v == 0 for v in pcm[72000:120000]))
        self.assertTrue(all(v == 0 for v in pcm[168000:]))
        metadata = result["audio_metadata"]
        self.assertFalse(metadata["post_speed_adjustment_applied"])
        self.assertIsNone(metadata["atempo"])
        self.assertEqual(metadata["added_sentence_pause_seconds"], 0)

    def test_verified_whole_track_cache_ignores_translation_and_target(self):
        with patch.object(speech.helper, "edge_synthesize", side_effect=self.source) as synth:
            first = self.prepare()
            first_hash = speech.sha256_file(first["audio_path"])
            self.lesson["target_wpm"] = 100
            self.lesson["sentences"][0]["zh"] = "修订中文。"
            second = self.prepare()
        self.assertEqual(synth.call_count, 1)
        self.assertTrue(second["audio_metadata"]["cache_hit"])
        self.assertEqual(first_hash, speech.sha256_file(second["audio_path"]))

    def test_natural_mode_rejects_unsupported_engine_and_repetition(self):
        self.args.tts = "kokoro"
        with self.assertRaises(speech.RenderError):
            self.prepare()
        self.args.tts = "edge"
        self.lesson["repeats"] = 2
        with self.assertRaises(speech.RenderError):
            self.prepare()

    def test_explicit_spoken_text_is_preserved_and_used_for_synthesis(self):
        self.lesson["sentences"][0].update(en="I open 1 book.", spoken_text="I open a book.")
        source = self.root / "lesson.json"
        self.lesson.update(title="书", title_en="Book", level="A1")
        source.write_text(json.dumps(self.lesson), encoding="utf-8")
        args = speech.helper.parser().parse_args(["--lesson", str(source), "--output-dir", str(self.work), "--audio-only"])
        loaded = speech.helper.load_lesson(source, args)
        self.assertEqual(loaded["sentences"][0].get("spoken_text"), "I open a book.")
        with patch.object(speech.helper, "edge_synthesize", side_effect=self.source):
            speech.prepare_natural_audio(speech.helper, args, loaded, self.work)


if __name__ == "__main__":
    unittest.main()
