"""Animation tests protect real motion, readable captions, and measured timings."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageChops


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "animation_renderer.py"
WORK_TEMP = Path(__file__).resolve().parents[2] / "tmp"
WORK_TEMP.mkdir(exist_ok=True)


def load_renderer(test):
    test.assertTrue(SCRIPT.is_file(), "动画渲染模块尚未实现")
    spec = importlib.util.spec_from_file_location("animation_renderer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def lesson(width=1920, height=1080):
    return {
        "title": "街角书店", "title_en": "The Little Bookshop", "level": "A1-A2",
        "accent": "en-US", "target_wpm": 100, "width": width, "height": height,
        "sentences": [
            {"en": "I sit by the window and read a page.", "zh": "我坐在窗边，读了一页书。",
             "ipa": "/aɪ sɪt baɪ ðə ˈwɪn.doʊ ənd riːd ə peɪdʒ/", "focus": []},
            {"en": "The rain is soft.", "zh": "雨下得很轻。", "ipa": "/ðə reɪn ɪz sɔːft/", "focus": []},
        ],
    }


def entries():
    return [
        {"sentence_index": 0, "start": 0.0, "speech_end": 0.8, "end": 1.0, "pass": 1},
        {"sentence_index": 1, "start": 1.0, "speech_end": 1.8, "end": 2.0, "pass": 1},
    ]


class AnimationRendererTests(unittest.TestCase):
    def test_rounded_pcm_timeline_does_not_add_an_extra_frame(self):
        module = load_renderer(self)
        self.assertEqual(module.frame_count([{"sentence_index": 0, "start": 0, "end": 55.291667}], 24), 1327)

    def test_scene_moves_while_same_sentence_captions_stay_fixed(self):
        # Removing local rain, steam, book, and character motion must fail this.
        module = load_renderer(self)
        first = module.frame_at(lesson(), entries(), 0.12)
        later = module.frame_at(lesson(), entries(), 0.87)
        scene = (72, 180, 1070, 992)
        captions = (1150, 205, 1840, 925)
        self.assertIsNotNone(ImageChops.difference(first.crop(scene), later.crop(scene)).getbbox())
        self.assertIsNone(ImageChops.difference(first.crop(captions), later.crop(captions)).getbbox())

    def test_unreadable_long_subtitle_is_rejected_instead_of_clipped(self):
        # A silent truncation or unlimited font shrink must fail this.
        module = load_renderer(self)
        data = lesson()
        data["sentences"][0]["en"] = "A very long sentence with many ordinary English words. " * 14
        with self.assertRaisesRegex(ValueError, "字幕|拆分"):
            module.frame_at(data, entries(), 0.2)

    def test_missing_assets_have_a_complete_original_animated_fallback(self):
        # Treating optional AI art as mandatory must fail this.
        module = load_renderer(self)
        with tempfile.TemporaryDirectory(dir=str(WORK_TEMP)) as directory:
            frame = module.frame_at(lesson(1080, 1920), entries(), 0.2, assets_dir=directory)
        self.assertEqual(frame.size, (1080, 1920))
        # The warm illustration occupies this region even with no files present.
        colors = frame.crop((80, 300, 1000, 1070)).getcolors(1080 * 1920)
        self.assertGreater(len(colors), 24)

    def test_ai_art_does_not_receive_a_second_programmatic_book_or_table(self):
        # Unconditionally painting fallback furniture over AI art must fail.
        module = load_renderer(self)
        with tempfile.TemporaryDirectory(dir=str(WORK_TEMP)) as directory:
            Image.new("RGB", (1448, 1086), (38, 77, 89)).save(Path(directory) / "bookshop-background.png")
            Image.new("RGBA", (1024, 1536), (0, 0, 0, 0)).save(Path(directory) / "reading-fox.png")
            scene = module.AnimationScene(lesson(), entries(), assets_dir=directory)
            frame = scene.render_frame(.2)
        self.assertEqual(frame.getpixel((72 + round(998 * .835), 180 + round(812 * .92))), (38, 77, 89))
        self.assertEqual(frame.getpixel((72 + round(998 * .61), 180 + round(812 * .71))), (38, 77, 89))

    def test_sentence_end_boundary_switches_without_losing_last_frame(self):
        # Inclusive previous-sentence end or estimated timing must fail this.
        module = load_renderer(self)
        self.assertEqual(module.sentence_at(entries(), 0.999), 0)
        self.assertEqual(module.sentence_at(entries(), 1.0), 1)
        self.assertEqual(module.sentence_at(entries(), 2.0), 1)
        self.assertEqual(module.frame_count(entries(), 24), 48)
        with self.assertRaises(ValueError):
            module.sentence_at(entries(), 2.01)

    def test_gap_in_measured_timeline_is_rejected(self):
        # Letting mismatched audio/video timelines proceed must fail this.
        module = load_renderer(self)
        broken = entries()
        broken[1]["start"] = 1.1
        with self.assertRaisesRegex(ValueError, "时间|连续"):
            module.frame_count(broken, 24)

    def test_non_story_aspect_ratio_is_rejected_before_captions_can_be_cut(self):
        module = load_renderer(self)
        with self.assertRaisesRegex(ValueError, "16:9|9:16"):
            module.frame_at(lesson(1280, 800), entries(), .2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 FFmpeg")
    def test_real_h264_video_has_exact_frame_count_and_no_audio(self):
        # Exporting estimated duration, wrong pixel format, or audio must fail this.
        module = load_renderer(self)
        short = [{"sentence_index": 0, "start": 0.0, "speech_end": .2, "end": .25, "pass": 1}]
        with tempfile.TemporaryDirectory(dir=str(WORK_TEMP)) as directory:
            output = Path(directory) / "animation.mp4"
            result = module.render_animated_video(lesson(960, 540), short, output, shutil.which("ffmpeg"), fps=8)
            probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(output)], capture_output=True, text=True, check=True)
            streams = json.loads(probe.stdout)["streams"]
        self.assertEqual(result["frame_count"], 2)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["codec_name"], "h264")
        self.assertEqual(streams[0]["pix_fmt"], "yuv420p")
        self.assertEqual(streams[0]["nb_read_frames"], "2")


if __name__ == "__main__":
    unittest.main()
