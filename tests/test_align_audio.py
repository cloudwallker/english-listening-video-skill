"""Real PCM and known acoustic spans test the alignment contract without a model download."""
import hashlib
import importlib
import json
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
try:
    alignment=importlib.import_module("align_audio")
except ModuleNotFoundError:
    alignment=None


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(alignment,"可复用对齐 CLI 尚未实现。")
        temporary_root=Path(__file__).resolve().parents[2]/"tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix="alignment-test-",dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.audio=self.root/"approved.wav"
        rate=48000
        samples=array("h",(round(2500*math.sin(2*math.pi*440*i/rate)) if i/rate<0.8 or i/rate>=1.3 else 0 for i in range(2*rate)))
        if sys.byteorder!="little": samples.byteswap()
        with wave.open(str(self.audio),"wb") as destination:
            destination.setparams((1,2,rate,0,"NONE","not compressed"))
            destination.writeframes(samples.tobytes())
        self.lesson={"accent":"en-US","sentences":[{"en":"A cat."},{"en":"A dog."}]}
        self.words=[{"word_index":0,"sentence_index":0,"text":"A","start":0.10,"end":0.20,"ctc_score":0.99},{"word_index":1,"sentence_index":0,"text":"CAT","start":0.30,"end":0.70,"ctc_score":0.97},{"word_index":2,"sentence_index":1,"text":"A","start":1.40,"end":1.50,"ctc_score":0.96},{"word_index":3,"sentence_index":1,"text":"DOG","start":1.60,"end":1.80,"ctc_score":0.95}]

    def result(self,words=None,silences=None,greedy="A CAT A DOG"):
        pcm=alignment.read_pcm(self.audio)
        detected=alignment.detect_silences(pcm) if silences is None else silences
        return alignment.assemble_alignment(self.lesson,self.words if words is None else words,detected,pcm["duration"],greedy)

    def test_caption_changes_at_measured_pcm_silence_midpoint_without_audio_mutation(self):
        original=hashlib.sha256(self.audio.read_bytes()).hexdigest()
        result=self.result()
        self.assertEqual(result["quality"]["status"],"passed")
        segments=result["segments"]
        self.assertEqual(segments[0]["start"],0)
        self.assertAlmostEqual(segments[0]["end"],1.05,delta=0.001)
        self.assertEqual(segments[0]["end"],segments[1]["start"])
        self.assertEqual(segments[1]["end"],2.0)
        self.assertLess(segments[0]["speech_end"],segments[0]["end"])
        self.assertLess(segments[1]["start"],segments[1]["speech_start"])
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).hexdigest(),original)

    def test_missing_word_is_rejected_instead_of_claiming_full_coverage(self):
        with self.assertRaises(alignment.AlignmentError):
            self.result(words=self.words[:-1])

    def test_wrong_known_word_is_rejected_instead_of_replacing_original_text(self):
        changed=[dict(word) for word in self.words]
        changed[-1]["text"]="FOG"
        with self.assertRaises(alignment.AlignmentError):
            self.result(words=changed)

    def test_weak_word_or_missing_waveform_boundary_is_marked_for_review(self):
        weak=[dict(word) for word in self.words]
        weak[1]["ctc_score"]=0.1
        self.assertEqual(self.result(words=weak)["quality"]["status"],"needs_review")
        result=self.result(silences=[])
        self.assertEqual(result["quality"]["status"],"needs_review")
        self.assertEqual(result["boundary_evidence"][0]["method"],"ctc_gap_midpoint_needs_review")

    def test_unspoken_numbers_require_explicit_spoken_text(self):
        numbered={"sentences":[{"en":"I have 3 books."}]}
        with self.assertRaises(alignment.AlignmentError):
            alignment.known_words(numbered)
        numbered["sentences"][0]["spoken_text"]="I have three books."
        self.assertEqual(alignment.known_words(numbered),[["I","HAVE","THREE","BOOKS"]])

    def test_unsupported_letters_require_spoken_text_instead_of_silent_word_loss(self):
        accented={"sentences":[{"en":"We visit a café."}]}
        with self.assertRaises(alignment.AlignmentError):
            alignment.known_words(accented)
        accented["sentences"][0]["spoken_text"]="We visit a cafe."
        self.assertEqual(alignment.known_words(accented),[["WE","VISIT","A","CAFE"]])

    def test_invalid_word_times_are_rejected(self):
        changed=[dict(word) for word in self.words]
        changed[2].update(start=0.6,end=0.65)
        with self.assertRaises(alignment.AlignmentError):
            self.result(words=changed)

    def test_cli_preserves_inputs_and_does_not_serialize_absolute_paths(self):
        lesson_path=self.root/"lesson.json"
        lesson_path.write_text(json.dumps(self.lesson),encoding="utf-8")
        destination=self.root/"alignment.json"
        before=self.audio.read_bytes()
        model={"bundle":"test acoustic fixture","frame_seconds":0.02}
        with patch.object(alignment,"infer_known_words",return_value=(self.words,"A CAT A DOG",model)):
            code=alignment.main(["--audio",str(self.audio),"--lesson",str(lesson_path),"--output",str(destination),"--model-dir",str(self.root/"models")])
        self.assertEqual(code,0)
        result=json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(result["audio_file"],"approved.wav")
        self.assertEqual(result["audio_sha256"],hashlib.sha256(before).hexdigest())
        self.assertNotIn(str(self.root),destination.read_text(encoding="utf-8"))
        self.assertEqual(self.audio.read_bytes(),before)
        self.assertEqual(alignment.main(["--audio",str(self.audio),"--lesson",str(lesson_path),"--output",str(destination)]),1)

    def test_output_cannot_overwrite_approved_audio_even_with_overwrite_flag(self):
        lesson_path=self.root/"lesson.json"
        lesson_path.write_text(json.dumps(self.lesson),encoding="utf-8")
        before=self.audio.read_bytes()
        code=alignment.main(["--audio",str(self.audio),"--lesson",str(lesson_path),"--output",str(self.audio),"--overwrite"])
        self.assertEqual(code,1)
        self.assertEqual(self.audio.read_bytes(),before)


if __name__=="__main__":
    unittest.main()
