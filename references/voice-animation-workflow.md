# 自然慢讲试听与原轨动画

## 环境与默认路线

Python 管理内容、声音与时间轴，Pillow 绘制固定字幕及独立动画层，FFmpeg/ffprobe 编码和检查 H.264/AAC。基础依赖见 `scripts/requirements.txt`；Edge 使用第三方 `edge-tts` 访问微软在线声音，英文会发送至在线服务。自然模式目前只支持 Edge，无 API 密钥，不保证在线服务永久可用；指定声音失败就停止并说明。

默认 `--speech-mode natural --tts edge --voice en-US-JennyNeural`，原生速率 `-20%`。整个故事按顺序作为完整文本送给引擎，保留原生语调、意群与停顿，单遍输出；不以 100 WPM 为目标、不逐句拼音轨、不用 atempo 或额外句间留白凑速度。书店样例约 135 WPM，包含原生停顿，实际新稿需重新测量。

## 试听后保持整轨

从技能目录运行，下列相对目录均为示例输出。每次选择独立目录，已有文件用 `--overwrite` 才能替换；输入、已认可音轨和复用目录不能作为覆盖目标。

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./audition --speech-mode natural --audio-only
```

试听 `audition/narration.wav` 或 MP3，并查看实际声线、原生速率、时长与含原生停顿的 WPM。声音比较使用同稿和同口音，不强行把不同声音拉到相同 WPM。检查自然重音、意群、清晰度、句尾与 `read` 等上下文发音。用户选定后，记录整条 WAV 的 SHA256，之后仅调整字幕与画面。

用户明确要求正常语速时：

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./audition-normal --speech-mode natural --edge-rate=+0% --audio-only
```

这仍是原生整段讲述，不是把慢速文件后期加速。改原生速率或声线需要新试听；已认可声音制片时不得再次 normalize、切句拼接、插停顿或发 TTS 请求。

## 对齐批准 WAV

`scripts/align_audio.py` 使用已知原稿的 CTC 对齐和声学静音边界为字幕定位，只读取音轨。对齐器接受单声道、16-bit PCM WAV，在模型分析时转到 16 kHz，不改原 WAV；默认自然试听导出格式可直接使用。

```powershell
python -X utf8 scripts/align_audio.py --audio ./audition/narration.wav --lesson examples/bookshop.json --output ./alignment.json
```

可加 `--model-dir <模型缓存目录>`。首次需要联网下载官方 Wav2Vec2 权重，约 378 MB；已有缓存时可用 `--offline`，不发 TTS 或收费 API 请求。对齐输出包含音频/稿件哈希、句子与单词时间点、质量信息及换句证据。质量不足时状态为 `needs_review` 并以退出码 2 停止，核查后重做；不能把失败结果当成已验收时间轴。

对齐需要兼容的 CPU PyTorch/TorchAudio 环境，可独立使用 Python 3.11。依赖锁定见 `scripts/requirements-align.txt`，当前采用 2.5.1 匹配组合；不要混装不同版本或 GPU/CPU 构建。Windows/Linux 安装示例依据[官方旧版本命令](https://pytorch.org/get-started/previous-versions/#v251)与[TorchAudio 兼容说明](https://docs.pytorch.org/audio/2.5.0/installation.html)：

```powershell
python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install numpy==1.26.4
```

在该环境中运行对齐命令；主排版环境可以独立。模型不打包进小型 Skill，缓存也不上传仓库。算法来源见[官方强制对齐教程](https://docs.pytorch.org/audio/2.5.0/tutorials/forced_alignment_tutorial.html)。CTC 分数是模型分数，不是边界精度或发音正确率；字幕继续使用审核后的原稿，不用 ASR 猜测词替换正文。

含数字、缩写等需展开读法的内容，先确定声音中实际读出的英语。对齐器遇到不能明确处理的数字时会要求显式 `spoken_text`，不能静默漏字；用于对齐的读法必须与已认可音轨一致。

## 原音轨生成动画

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./sample --approved-audio ./audition/narration.wav --alignment ./alignment.json --style paper-animation --width 1920 --height 1080 --fps 24 --validate-only
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./sample --approved-audio ./audition/narration.wav --alignment ./alignment.json --style paper-animation --width 1920 --height 1080 --fps 24
```

`--approved-audio` 分支强制单遍，验证音轨与对齐哈希、原稿和时间轴，直接将完整 WAV 输入 MP4 声音编码，不重新 TTS、normalize、变速、拼句或加停顿。已有批准 MP3 时可加 `--approved-mp3 ./audition/narration.mp3` 原样附带。只改译文、IPA、字体、尺寸或素材可继续复用同一个 WAV；改英文时停止使用旧对齐。

横屏为插画与字幕双栏，竖屏为上插画下字幕，仅支持精确 16:9 与 9:16。`paper-animation` 有雨滴、蒸汽、翻页与角色轻动作，字幕在一句内固定。素材为 `assets/bookshop-background.png`、`assets/reading-fox.png`，可用 `--assets-dir` 提供同名背景和透明角色；缺失时采用原创程序插画。新主题应换成匹配场景，字幕始终由代码绘制。`--style card` 保留旧竖屏静态卡片。

检查全部不同字幕、开始中段末尾与同句不同时间的帧；解码整片，核验声音持续时间、尺寸与帧数。原 WAV 副本应哈希一致。AAC/MP3 属有损压缩，编码延迟、尾部块补齐和不同字节不能用来断言原轨被改速；结合轨道时长和波形节奏判断。画面显示词速需注明“含自然停顿”。

## 显式保留的校准模式

仅在用户要求固定目标词速或选择旧本地引擎时使用：

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./audition-calibrated --speech-mode calibrated --tts edge --target-wpm 100 --repeats 1 --pause 0.6 --audio-only
```

`target_wpm` 仅在此模式用于校准。先在引擎内慢读，测量真实 PCM 后以 0.75–1.25 的 atempo 小幅微调；超范围调整原生速率再合成一次，仍不合格就停止。逐句声音可用 `--audio-cache`；旧 `--audio-bundle` 仅用于 calibrated 流程，它不能代替已认可整轨的 `--approved-audio`。

Kokoro ONNX 和 Windows SAPI 都需要显式 calibrated。Kokoro 用 `--tts kokoro --voice af_heart`，通过 `--kokoro-python`、`--kokoro-model`、`--kokoro-voices` 配置独立运行时，依赖见 `scripts/requirements-kokoro.txt`。使用与 voices 配套的浮点 `speed` / `duration` ONNX 导出；模型选择[官方 release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1)，核对代码、模型与 eSpeak 的许可。SAPI 用 `--tts sapi`，以本机实际可用的英语 System.Speech 声音为准。`--tts auto` 的 Edge→SAPI 回退也只用于 calibrated，不能静默替换 natural 的声线。

`runtime.local.example.json` 可复制为已忽略的 `runtime.local.json` 配置本机路径，不存密钥，不随便携包或仓库上传。eSpeak 数据路径可用 `--espeak-data-path` 指定，适配器仅在合成进程设置，不改全局配置。Azure、Google、ElevenLabs 等付费 API 尚未接入，用户选择并配置后再开发适配与费用上限。
