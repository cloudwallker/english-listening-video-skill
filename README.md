# 自然慢讲英语视频

### 把英文短文，做成能反复听的英语视频

**自然慢讲 · 中英字幕与 IPA · 试听通过后复用原音轨**

**[▶ 播放 36 秒演示视频](https://github.com/cloudwallker/english-listening-video-skill/blob/main/examples/bookshop/video.mp4)** · [试听配音](examples/bookshop/narration.mp3)

`english-listening-video`

中文 · [English](README_EN.md)

[了解能力](#了解能力) · [成品案例](#成品案例) · [快速开始](#快速开始) · [一句话调用](#一句话调用) · [命令行](#命令行)

[![点击封面播放演示视频：街角书店，带英文、IPA、中文字幕](examples/bookshop/cover.png)](https://github.com/cloudwallker/english-listening-video-skill/blob/main/examples/bookshop/video.mp4)

点击上方封面或“播放演示视频”，进入 GitHub 视频播放器。

## 项目简介

一个用于制作英语磨耳朵视频的 Agent Skill。提供英文短文、中文素材或主题，由 Agent 整理英文、翻译和教学音标，脚本生成配音、对齐字幕并合成轻动画视频。

默认先做自然慢讲试听：整段交给 Edge 神经声音，以原生 `-20%` 速率讲述，保留意群、重音和停顿。声音通过后，同一条 WAV 直接用于视频，不重新配音、不拉伸音轨、不追加停顿。实际词速由录音决定，不强行凑到 100 WPM。

## 了解能力

| 能力 | 实际行为 |
|---|---|
| 自然慢讲试听 | 默认美式 Jenny、整段单遍；支持原生速率与声音选择，输出 WAV、MP3 和实测数据 |
| 冻结已通过的音轨 | WAV 原样复制，按原轨合成视频；可附带原样 MP3，哈希不变 |
| 英文、中文和 IPA | 固定三层字幕区，句级教学 IPA 可编辑；内容 JSON 可保留重点词释义 |
| 根据真实声音换句 | 官方 TorchAudio known-text CTC 对齐，再结合真实静音确定换句点；质量不足时停止 |
| 分层轻动画 | 原创书店插画与透明角色，雨滴、茶汽、翻页和轻动作独立运动；支持横屏与竖屏 |
| 保留旧配音路线 | 显式 `calibrated` 模式支持 Kokoro ONNX、本地 Windows SAPI 和目标词速校准 |

IPA 由 Agent 按稿件和口音核查，是教学参考。对齐脚本不会自动生成 IPA，也不能据此证明每个音发得正确。新主题需要匹配的插画素材；当前内置场景是书店。小说分章、付费配音 API 和自动生成任意主题动画尚未接入。

## 成品案例

**街角书店｜82 词、10 句、约 36 秒**

[播放视频](examples/bookshop/video.mp4) · [试听 MP3](examples/bookshop/narration.mp3) · [查看字幕](examples/bookshop/captions.srt) · [编辑稿件](examples/bookshop.json)

示例使用 Edge `en-US-JennyNeural`，原生 `-20%`，实测约 **135 WPM（含自然停顿）**。1920×1080、24 fps，带英文、中文字幕与 IPA。插画为本项目原创生成素材，文字由代码独立绘制。

配套 [WAV](examples/bookshop/narration.wav) 和[对齐结果](examples/bookshop/alignment.json) 可直接用于本地复现，不必重新配音或下载对齐模型。MP4 使用 AAC 压缩声音，独立 WAV 保留已通过音轨的原始字节。

## 快速开始

### 1. 安装 Skill

Windows / PowerShell，克隆到一个尚不存在的技能目录：

```powershell
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$skillsDir = Join-Path $codexRoot 'skills'
New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
git clone https://github.com/cloudwallker/english-listening-video-skill.git (Join-Path $skillsDir 'english-listening-video')
Set-Location (Join-Path $skillsDir 'english-listening-video')
```

macOS / Linux：

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/english-listening-video"
mkdir -p "$(dirname "$skill_dir")"
git clone https://github.com/cloudwallker/english-listening-video-skill.git "$skill_dir"
cd "$skill_dir"
```

安装后确认 `english-listening-video/SKILL.md` 存在，新开 Codex 会话。已有同名技能时先保留本机配置，再更新该目录；不要把新文件夹嵌套进旧文件夹。

### 2. 准备依赖

需要 Python 3.9+、FFmpeg、FFprobe，以及覆盖中文和 IPA 的字体。FFmpeg 与 FFprobe 需能在终端直接调用。

```powershell
python -m pip install -r scripts/requirements.txt
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir sample --style paper-animation --width 1920 --height 1080 --validate-only
```

该校验只检查稿件、字体与版面，不联网、不合成、不写输出。Windows 默认尝试系统字体；其他环境可用 `--font-en`、`--font-zh`、`--font-ipa` 指定字体。macOS / Linux 没有 `python` 命令时改用 `python3`。

### 3. 在 Codex 中说一句话

```text
使用 $english-listening-video，把这段英文做成自然慢讲磨耳朵视频。
先给我同稿试听，通过后复用原音轨，带英文、中文和 IPA 字幕。
```

随后给出正文或主题。Agent 负责内容理解、翻译、音标核查和试听选择；Python 脚本负责真实配音、对齐与渲染，无需另配文本模型 API 密钥。

## 一句话调用

| 场景 | 对 Agent 说 |
|---|---|
| 给英文短文 | 使用 `$english-listening-video`，保留这段英文，先生成自然慢讲试听。 |
| 给中文或主题 | 使用 `$english-listening-video`，围绕雨天书店写 A1–A2 小故事，附中文和 IPA。 |
| 已通过声音 | 这个音轨通过，使用 `$english-listening-video` 直接输出适配视频，保留原语速与停顿。 |
| 正常语速 | 使用 `$english-listening-video`，用原生正常语速做同稿试听。 |
| 修改字幕 | 使用 `$english-listening-video`，只调整中文和音标，复用已通过的 WAV 与对齐结果。 |

## 命令行

以下命令在技能目录执行，输出目录使用新名字。`--overwrite` 才允许覆盖已有成品；不允许覆盖输入文件。

### 先做自然慢讲试听

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir audition --speech-mode natural --audio-only
```

输出 `narration.wav`、`narration.mp3`、`metadata.json`、`lesson.json` 和 `en.txt`。正常语速加 `--edge-rate=+0%`；调整自然慢讲可使用 `--edge-rate=-15%` 等原生速率，重新试听再选。

### 对齐选定音轨

新音轨需要独立的可选对齐环境。推荐 Python 3.10–3.12；首次运行从 PyTorch 官方地址下载约 378 MB 权重，CPU 即可运行，不需要 GPU。

Windows / Linux：

```powershell
python -m venv .venv-align
# Windows；Linux 改为 .venv-align/bin/python
.venv-align/Scripts/python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
.venv-align/Scripts/python -m pip install numpy==1.26.4
.venv-align/Scripts/python -X utf8 scripts/align_audio.py --audio audition/narration.wav --lesson examples/bookshop.json --output alignment.json
```

macOS 可在独立环境中从 PyPI 安装 `scripts/requirements-align.txt`。模型用 `--model-dir` 指定缓存位置，已有权重时可加 `--offline`。脚本只分析音轨，输入 WAV 的哈希保持不变；低置信度或缺少可靠换句证据时返回失败，应核查声音或稿件后再制作视频。

### 用原音轨合成视频

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir sample --approved-audio audition/narration.wav --approved-mp3 audition/narration.mp3 --alignment alignment.json --style paper-animation --width 1920 --height 1080 --fps 24
```

想直接复现示例，把上面三条输入路径替换为 `examples/bookshop/narration.wav`、`examples/bookshop/narration.mp3` 和 `examples/bookshop/alignment.json`。竖屏用 `--width 1080 --height 1920`。

### 可选：旧目标词速流程

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir audition-calibrated --speech-mode calibrated --tts edge --target-wpm 100 --repeats 1 --audio-only
```

此模式按句配音、校准词速并追加句间停顿，可能使用保调变速；听感与自然模式不同。Kokoro、SAPI、缓存和逐句 bundle 复用见[配音与动画流程](references/voice-animation-workflow.md)。

## 输出文件

| 文件 | 用途 |
|---|---|
| `video.mp4` | 有声 H.264/AAC 成片 |
| `narration.wav` / `.mp3` | 独立试听；批准 WAV 原样保留 |
| `captions.srt` | 英文、IPA、中文三层字幕 |
| `lesson.json` | 可编辑英文、中文、IPA 与重点词 |
| `timeline.json` | 实际音频哈希、时长与字幕时间轴 |
| `cover.png` / `preview.png` | 封面和成片预览 |

## 文档与测试

[Skill 入口](SKILL.md) · [内容与音轨契约](references/production-contract.md) · [配音与动画流程](references/voice-animation-workflow.md) · [素材来源](assets/provenance.json)

```powershell
python -X utf8 -m unittest discover -s tests -v
```

基本测试使用真实 PCM 和 FFmpeg，外部 TTS 以声学夹具替代；对齐算法测试不下载模型。测试不替代主观试听。当前运行验证环境为 Windows，其他系统仍需检查字体和 FFmpeg 配置。

## 费用、数据与许可

Edge 在线配音不需要本项目配置付费 API 密钥，英文会发送至微软服务；可用性与使用条件以服务提供方为准。Kokoro 可本地 CPU 运行，模型需自行下载。付费配音服务尚未接入。

本仓库不包含第三方模型、字体或本机凭据。依赖与模型遵循各自许可证，原创插画来源见 `assets/provenance.json`。仓库当前未指定开源许可证，公开可见不等于授予任意再分发许可。

