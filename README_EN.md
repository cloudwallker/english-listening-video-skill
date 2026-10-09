# Natural English Listening Videos

### Turn short English stories into videos you can listen to again

**Native slower speech · English, Chinese and IPA captions · Reuse the approved audio**

`english-listening-video`

[中文](README.md) · English

[Features](#features) · [Example](#example) · [Quick start](#quick-start) · [Prompts](#prompts) · [Command line](#command-line)

![Original bookshop scene with English, IPA and Chinese captions](examples/bookshop/cover.png)

## About

An Agent Skill for producing English listening videos from English text, Chinese material or a topic. The agent prepares the story, translation and teaching IPA; scripts synthesize speech, align captions and render gentle animation.

The default workflow starts with a whole-paragraph Edge audition at native `-20%` rate. It preserves native phrasing, stress and pauses. Once approved, the same WAV goes into the video without resynthesis, time stretching or added pauses. WPM is measured from the recording rather than forced to 100.

## Features

| Capability | Behavior |
|---|---|
| Natural slower audition | US Jenny by default, one complete paragraph and one pass; adjustable native voice/rate, WAV and MP3 output |
| Preserve approved audio | Byte-identical WAV copy; optional byte-identical MP3 attachment |
| Three caption layers | Editable English, teaching IPA and Chinese captions, with optional vocabulary notes |
| Acoustic alignment | Official TorchAudio known-text CTC and measured silence boundaries; insufficient evidence stops production |
| Gentle layered animation | Original bookshop artwork with independent rain, steam, page and character motion; landscape and portrait |
| Legacy calibrated mode | Explicit support for target WPM, local Kokoro ONNX and Windows SAPI |

The agent checks teaching IPA; alignment does not generate IPA or prove pronunciation accuracy. The built-in scene is a bookshop. New topics need suitable artwork. Novel chapter processing, paid voice APIs and automatic animation for arbitrary scenes are not implemented.

## Example

**The Little Bookshop — 82 words, 10 sentences, about 36 seconds**

[Watch MP4](examples/bookshop/video.mp4) · [Listen to MP3](examples/bookshop/narration.mp3) · [Captions](examples/bookshop/captions.srt) · [Editable story](examples/bookshop.json)

Edge `en-US-JennyNeural`, native `-20%`, approximately **135 WPM including native pauses**. 1920×1080 at 24 fps. Original generated illustrations are composited separately from the English, IPA and Chinese text.

The included [WAV](examples/bookshop/narration.wav) and [alignment](examples/bookshop/alignment.json) let you reproduce the example without new speech requests or model downloads. AAC compresses the video audio; the separate WAV preserves the approved bytes.

## Quick start

### 1. Install the skill

Windows / PowerShell, using a destination that does not already exist:

```powershell
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$skillsDir = Join-Path $codexRoot 'skills'
New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
git clone https://github.com/cloudwallker/english-listening-video-skill.git (Join-Path $skillsDir 'english-listening-video')
Set-Location (Join-Path $skillsDir 'english-listening-video')
```

macOS / Linux:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/english-listening-video"
mkdir -p "$(dirname "$skill_dir")"
git clone https://github.com/cloudwallker/english-listening-video-skill.git "$skill_dir"
cd "$skill_dir"
```

Confirm that `english-listening-video/SKILL.md` exists, then open a new Codex conversation. When updating an existing installation, preserve local settings and avoid nesting another skill folder inside it.

### 2. Prepare dependencies

Requires Python 3.9+, FFmpeg, FFprobe and fonts covering Chinese and IPA. Both FFmpeg commands must be available on PATH.

```powershell
python -m pip install -r scripts/requirements.txt
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir sample --style paper-animation --width 1920 --height 1080 --validate-only
```

Validation checks the lesson, fonts and layout without networking, synthesis or output writes. Use `--font-en`, `--font-zh` and `--font-ipa` to supply fonts. Replace `python` with `python3` where needed.

### 3. Ask Codex

```text
Use $english-listening-video to turn this English story into a natural slower listening video.
Give me an audition first. Once approved, reuse its audio with English, Chinese and IPA captions.
```

Provide the text or topic. The agent handles content, translation, IPA checks and audition selection; scripts handle synthesis, alignment and rendering. No additional text-model API key is needed.

## Prompts

| Goal | Ask the agent |
|---|---|
| English text | Use `$english-listening-video`, preserve this English text and make a natural slower audition first. |
| Chinese text or topic | Use `$english-listening-video` to write an A1–A2 rainy bookshop story with Chinese and IPA. |
| Approved audio | This track is approved. Use `$english-listening-video` to make the video with its original speed and pauses. |
| Normal speed | Use `$english-listening-video` to make a same-script audition at native normal speed. |
| Caption changes | Use `$english-listening-video` to edit only Chinese and IPA, reusing the approved WAV and alignment. |

## Command line

Run from the skill directory and use a new output directory. Overwriting generated outputs requires `--overwrite`; inputs remain protected.

### Make an audition

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir audition --speech-mode natural --audio-only
```

Produces `narration.wav`, `narration.mp3`, `metadata.json`, `lesson.json` and `en.txt`. Use `--edge-rate=+0%` for native normal speed, or another native rate such as `--edge-rate=-15%` and audition again.

### Align the selected audio

New tracks need the optional alignment environment. Python 3.10–3.12 is recommended. First use downloads about 378 MB of official PyTorch weights; inference runs on CPU.

Windows / Linux:

```powershell
python -m venv .venv-align
# Windows; use .venv-align/bin/python on Linux
.venv-align/Scripts/python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
.venv-align/Scripts/python -m pip install numpy==1.26.4
.venv-align/Scripts/python -X utf8 scripts/align_audio.py --audio audition/narration.wav --lesson examples/bookshop.json --output alignment.json
```

On macOS, install `scripts/requirements-align.txt` from PyPI in a separate environment. `--model-dir` selects the model cache; `--offline` requires existing weights. Input audio bytes are unchanged. Low-confidence words or unreliable transitions require review before rendering.

### Render using the original track

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir sample --approved-audio audition/narration.wav --approved-mp3 audition/narration.mp3 --alignment alignment.json --style paper-animation --width 1920 --height 1080 --fps 24
```

To reproduce the bundled example, replace the three media/alignment input paths with `examples/bookshop/narration.wav`, `examples/bookshop/narration.mp3` and `examples/bookshop/alignment.json`. Use `--width 1080 --height 1920` for portrait.

### Optional calibrated workflow

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir audition-calibrated --speech-mode calibrated --tts edge --target-wpm 100 --repeats 1 --audio-only
```

This mode synthesizes sentences separately, calibrates WPM and adds sentence gaps; it may use pitch-preserving time stretching. See the [voice and animation guide](references/voice-animation-workflow.md) for Kokoro, SAPI, caches and sentence bundles.

## Outputs

| File | Purpose |
|---|---|
| `video.mp4` | H.264/AAC video |
| `narration.wav` / `.mp3` | Standalone listening audio; approved WAV bytes preserved |
| `captions.srt` | English, IPA and Chinese captions |
| `lesson.json` | Editable story, translation, IPA and vocabulary |
| `timeline.json` | Audio hashes, duration and caption timing |
| `cover.png` / `preview.png` | Cover and preview |

## Documentation and tests

[Skill entry](SKILL.md) · [Production contract](references/production-contract.md) · [Voice and animation](references/voice-animation-workflow.md) · [Artwork provenance](assets/provenance.json)

```powershell
python -X utf8 -m unittest discover -s tests -v
```

Tests use real PCM and FFmpeg with acoustic fixtures replacing external speech calls; alignment unit tests do not download models. They do not replace listening review. Runtime verification was performed on Windows; other systems need font and FFmpeg checks.

## Cost, data and licensing

Edge requires no paid API key configured by this project and sends the English text to Microsoft's service. Availability and usage terms depend on the provider. Kokoro runs locally on CPU with separately downloaded models. Paid voice APIs are not integrated.

Third-party models, fonts and local credentials are excluded. Dependencies and models retain their own licenses; original artwork provenance is recorded in `assets/provenance.json`. No open-source license has been selected for this repository; public visibility does not grant unrestricted redistribution rights.

