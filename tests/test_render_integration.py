"""Integration gates for exporting and reusing actual speech, without TTS calls."""
import json
import copy
import hashlib
import io
import math
import shutil
import sys
import tempfile
import unittest
import wave
from array import array
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render_video as renderer
TEST_TMP = Path(__file__).resolve().parents[2] / "tmp"
TEST_TMP.mkdir(exist_ok=True)


class RenderIntegrationTests(unittest.TestCase):
    def lesson(self, *extra):
        args = renderer.parser().parse_args([
            "--lesson", str(Path(__file__).resolve().parents[1] / "examples/bookshop.json"),
            "--output-dir", "unused", *extra])
        return renderer.load_lesson(Path(args.lesson), args), args

    def approved_fixture(self, directory):
        root = Path(directory)
        source = root / "approved.wav"
        samples = array("h", (round(1800 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48048)))
        with wave.open(str(source), "wb") as handle:
            handle.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            handle.writeframes(samples.tobytes())
        data = {"title": "小故事", "title_en": "A Little Story", "accent": "en-US", "level": "A1-A2",
                "target_wpm": 100, "repeats": 2, "sentences": [
                    {"en": "Hello.", "zh": "你好。", "ipa": "/həˈloʊ/", "focus": []},
                    {"en": "Goodbye.", "zh": "再见。", "ipa": "/ˌɡʊdˈbaɪ/", "focus": []}]}
        lesson_path = root / "story.json"
        lesson_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        alignment = {"schema_version": 1, "audio_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                     "audio_duration": 1.001, "method": "controlled measured test fixture",
                     "segments": [
                         {"sentence_index": 0, "en": "Hello.", "start": 0.0, "speech_start": .05, "speech_end": .40, "end": .45},
                         {"sentence_index": 1, "en": "Goodbye.", "start": .45, "speech_start": .50, "speech_end": .95, "end": 1.001}]}
        alignment_path = root / "alignment.json"
        alignment_path.write_text(json.dumps(alignment), encoding="utf-8")
        args = renderer.parser().parse_args(["--lesson", str(lesson_path), "--output-dir", str(root / "result"),
                                            "--approved-audio", str(source), "--alignment", str(alignment_path),
                                            "--style", "paper-animation", "--width", "960", "--height", "540", "--fps", "24"])
        return renderer.load_lesson(lesson_path, args), args, alignment, source

    def test_natural_defaults_ignore_legacy_repeat_and_calibration_target(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            original = json.loads((Path(__file__).resolve().parents[1] / "examples/bookshop.json").read_text(encoding="utf-8"))
            original.update(repeats=2, target_wpm=100)
            original.pop("edge_rate", None)
            source = Path(directory) / "legacy.json"
            source.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            args = renderer.parser().parse_args(["--lesson", str(source), "--output-dir", "unused"])
            lesson = renderer.load_lesson(source, args)
            self.assertEqual(args.speech_mode, "natural")
            self.assertEqual(lesson["repeats"], 1)
            self.assertEqual(lesson["edge_rate"], "-20%")
            args.speech_mode = "calibrated"
            calibrated = renderer.load_lesson(source, args)
            self.assertEqual(calibrated["repeats"], 2)
            self.assertEqual(calibrated["edge_rate"], "-58%")

    def test_explicit_repeated_natural_track_is_rejected(self):
        with self.assertRaisesRegex(renderer.RenderError, "单遍|repeats|一遍"):
            self.lesson("--repeats", "2")

    def test_approved_alignment_rejects_changed_audio_text_gaps_or_duration(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            lesson, args, original, _ = self.approved_fixture(directory)
            bad_cases = []
            changed = copy.deepcopy(original); changed["audio_sha256"] = "0" * 64; bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["audio_duration"] = 2.001; bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["segments"][1]["en"] = "Changed."; bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["segments"][1]["start"] = .46; bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["segments"][0]["speech_start"] = -.1; bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["segments"][1]["end"] = 1.5; bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["segments"][0]["start"] = float("nan"); bad_cases.append(changed)
            changed = copy.deepcopy(original); changed["quality"] = {"status": "needs_review"}; bad_cases.append(changed)
            for index, changed in enumerate(bad_cases):
                with self.subTest(case=index):
                    Path(args.alignment).write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(renderer.RenderError):
                        renderer.load_approved_audio(lesson, args)

    def test_natural_video_without_approved_alignment_fails_before_work_or_tts(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            original = sys.argv
            output = Path(directory) / "must-not-exist"
            sys.argv = ["render_video", "--lesson", str(Path(__file__).resolve().parents[1] / "examples/bookshop.json"),
                        "--output-dir", str(output)]
            errors = io.StringIO()
            try:
                with redirect_stderr(errors):
                    self.assertEqual(renderer.main(), 1)
                self.assertFalse(output.exists())
                self.assertRegex(errors.getvalue(), "试听|对齐|alignment")
            finally:
                sys.argv = original

    def test_changed_spoken_reading_requires_matching_alignment(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            lesson, args, alignment, _ = self.approved_fixture(directory)
            lesson["sentences"][0]["spoken_text"] = "Hi there."
            with self.assertRaises(renderer.RenderError):
                renderer.load_approved_audio(lesson, args)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 FFmpeg")
    def test_approved_render_keeps_source_and_published_wav_identical_without_added_pauses(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            lesson, args, _, source = self.approved_fixture(directory)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            mp3 = Path(directory) / "approved.mp3"
            renderer.encode_audio_preview(shutil.which("ffmpeg"), source, mp3)
            args.approved_mp3 = str(mp3)
            renderer.render_approved(lesson, args, Path(args.lesson), Path(args.output_dir), renderer.font_paths(args))
            result = Path(args.output_dir)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertEqual(hashlib.sha256((result / "narration.wav").read_bytes()).hexdigest(), digest)
            self.assertEqual((result / "narration.mp3").read_bytes(), mp3.read_bytes())
            timeline = json.loads((result / "timeline.json").read_text(encoding="utf-8"))
            self.assertFalse(timeline["audio"]["post_speed_adjustment_applied"])
            self.assertTrue(timeline["audio_reused_without_time_stretch_or_reassembly"])
            self.assertEqual(timeline["soundtrack_duration"], 1.001)
            self.assertEqual(timeline["video_frames"], 25)
            self.assertEqual(timeline["segments"][1]["start"], .45)
            self.assertIn("00:00:00,450 --> 00:00:01,001", (result / "captions.srt").read_text(encoding="utf-8"))

    def test_landscape_animation_preserves_complete_story(self):
        lesson, args = self.lesson("--style", "paper-animation", "--width", "1920",
                                   "--height", "1080", "--repeats", "1")
        self.assertEqual((lesson["width"], lesson["height"]), (1920, 1080))
        self.assertEqual(len(lesson["sentences"]), 10)
        self.assertEqual(lesson["repeats"], 1)

    def test_landscape_static_card_rejects_unsupported_layout(self):
        with self.assertRaises(renderer.RenderError):
            self.lesson("--width", "1920", "--height", "1080")

    def test_animation_rejects_ratio_that_would_crop_captions(self):
        with self.assertRaisesRegex(renderer.RenderError, "16:9|裁切"):
            self.lesson("--style", "paper-animation", "--width", "1080", "--height", "1620")

    def test_audio_only_does_not_require_ipa_or_chinese_font(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            original = sys.argv
            sys.argv = ["render_video", "--lesson", str(Path(__file__).resolve().parents[1] / "examples/bookshop.json"),
                        "--output-dir", directory, "--audio-only", "--validate-only",
                        "--font-zh", "does-not-exist.ttf", "--font-ipa", "does-not-exist.ttf"]
            try:
                self.assertEqual(renderer.main(), 0)
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                sys.argv = original

    def test_audio_only_accepts_a_landscape_story_without_visual_style(self):
        lesson, _ = self.lesson("--audio-only", "--width", "1920", "--height", "1080")
        self.assertEqual(len(lesson["sentences"]), 10)

    def test_audio_bundle_rejects_different_text_before_any_synthesis(self):
        lesson, args = self.lesson()
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            bundle = Path(directory) / "bundle"
            bundle.mkdir()
            (bundle / "bundle.json").write_text(json.dumps({
                "schema_version": 1, "english_sha256": "wrong", "accent": "en-US",
                "target_wpm": 100, "audio": {}, "sentences": []}), encoding="utf-8")
            with self.assertRaisesRegex(renderer.RenderError, "英文|文本|稿件"):
                renderer.load_audio_bundle(lesson, bundle, Path(directory))

    def test_audition_loudness_matches_without_changing_duration_or_clipping(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            levels = []
            for amplitude in (1200, 6000):
                path = Path(directory) / f"tone-{amplitude}.wav"
                samples = array("h", (round(amplitude * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(4800)))
                with wave.open(str(path), "wb") as handle:
                    handle.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                    handle.writeframes(samples.tobytes())
                record = renderer.normalize_soundtrack(path)
                count, data = renderer.pcm_info(path)
                self.assertEqual(count, 4800)
                self.assertLessEqual(record["output_peak_dbfs"], -1.99)
                levels.append(record["output_rms_dbfs"])
            self.assertAlmostEqual(levels[0], levels[1], delta=.05)
            self.assertAlmostEqual(levels[0], -22, delta=.05)


if __name__ == "__main__":
    unittest.main()
