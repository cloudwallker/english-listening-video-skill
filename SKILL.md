---
name: english-listening-video
description: 适用于用户要求制作自然慢讲英语磨耳朵视频、同稿配音试听、带中英字幕与 IPA 音标的听力动画，或用已认可的音轨继续制片。用户只要文字时交付脚本；普通翻译、考试解题和原生视频字幕截图不触发。
---

# 自然慢讲英语磨耳朵

把内容制作成连贯、好听、可反复听的英语视频。默认先做同稿试听，声音选定后用原音轨制作带英文、中文和句级 IPA 的动画。用户已认可音轨时直接继续，不重复选声。

## 内容与声音

- 英文原文保留内容、顺序与标点，按完整句或意群分屏；中文素材按原意编写英语；主题默认写 A1–A2、70–100 词的原创生活小故事。用户指定难度、长度或受众时遵循其要求。
- 默认 `--speech-mode natural`：美式 Edge `en-US-JennyNeural`、原生 `-20%`，整段讲述、单遍，保留原生意群、重音与停顿。自然模式只支持 Edge；失败时说明情况，不偷偷切换声音。
- 自然模式不追固定 WPM，不逐句拼音频、不做后期变速、不插句间停顿。书店示例实测约 135 WPM，**包含原生停顿**；其他稿件会不同，不能承诺每篇都是 100 WPM。
- 用户明确要正常语速时用 natural 配合 `--edge-rate=+0%`，同样先试听。只有明确要求固定词速或旧本地引擎流程时才用 `--speech-mode calibrated`；详见[配音与动画流程](references/voice-animation-workflow.md)。
- 正常语速对照、跟读长留白、测验和背景音乐按用户要求添加，磨耳朵默认没有这些环节。

## 制作流程

1. 按[制作契约](references/production-contract.md)保存 `lesson.json`，每句 `en`、`zh`、`ipa` 必填。统一口音和宽式音标，核查 `read/live/close`、时态、数字、缩写及弱读。TTS phonemes 不能直接当教学 IPA，强制对齐不能证明发音正确。
2. 用 `--audio-only` 生成自然慢讲试听，直接展示 WAV 或 MP3，附声线、原生速率和含停顿的实测 WPM。比较同稿声音时保持稿件与口音一致，按自然度、清晰度、意群和句尾完整性选择；不为比较而把每个声音拉到相同 WPM。无法实际回听时说明限制。
3. **试听通过后冻结整轨**：记录 WAV 哈希，后续直接使用 `--approved-audio <wav> --alignment <json>`。此分支强制单遍，不重新 TTS、变速、拼句、normalize 或加入停顿。只改中文、IPA 或画面时保留同一个 WAV；改英文则重新制作试听并对齐。
4. 用 `scripts/align_audio.py` 将原稿对齐到已认可 WAV。对齐只分析声音，不改输入；质量不足时停止并核查，不能拿旧时间轴或按字数平均分配。完整命令、依赖和检查方法见[配音与动画流程](references/voice-animation-workflow.md)。
5. 先 `--validate-only`，再制作 `--style paper-animation`。默认可用 1080×1920 竖屏或 1920×1080 横屏。字幕由真实音轨换句，在一句内保持静止；雨滴、蒸汽、翻页和角色轻动作独立运动。新主题需匹配素材，不能把书店情节当通用背景。
6. 检查所有不同字幕画面及开始、中段、结尾，确认 EN / IPA / 中文完整；用 ffprobe 和完整解码核验音视频。批准 WAV 的导出副本应哈希一致，MP4 的 AAC 编码只改变压缩表示，不改变语速与时间轴。画面词速注明“含自然停顿”，不能沿用旧的 100 WPM 标签。

## 快速调用

从技能目录运行。三个脚本步骤使用同一份稿件与同一条试听 WAV；先完成试听选择，再执行后两步。

```powershell
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./audition --speech-mode natural --audio-only
python -X utf8 scripts/align_audio.py --audio ./audition/narration.wav --lesson examples/bookshop.json --output ./alignment.json
python -X utf8 scripts/render_video.py --lesson examples/bookshop.json --output-dir ./sample --approved-audio ./audition/narration.wav --alignment ./alignment.json --style paper-animation --width 1920 --height 1080 --fps 24 --validate-only
```

校验通过后去掉最后一条命令的 `--validate-only` 生成视频。已有批准 MP3 可加 `--approved-mp3 <mp3>` 原样附带。以各脚本 `--help` 为参数依据；每次用独立输出目录，已有成品需用 `--overwrite`，不能覆盖输入音轨或稿件。

Edge 会把英文发给微软在线服务；非公开敏感正文优先适合的本地方案或确认传输。免费优先，付费 API 尚未接入，不自动调用密钥或收费。参考视频只提取节奏和版式，第三方素材保留来源与许可。

## 交付

展示实际生成的可播放 MP4 与同稿试听，附 SRT、内容 JSON、封面和实测时间轴，说明声音、时长、词速指标及实际检查范围。不能只给提示词就声称成片完成。用户只要文字时直接交付带 IPA 的脚本。
