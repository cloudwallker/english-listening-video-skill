# 内容与制作契约

## 可编辑输入

```json
{
  "title": "街角书店",
  "title_en": "The Little Bookshop",
  "level": "A1-A2",
  "accent": "en-US",
  "width": 1080,
  "height": 1920,
  "sentences": [
    {
      "en": "I sit by the window and read a page.",
      "zh": "我坐在窗边，读一页书。",
      "ipa": "/aɪ sɪt baɪ ðə ˈwɪn.doʊ ənd riːd ə peɪdʒ/",
      "focus": [{"word": "window", "ipa": "/ˈwɪn.doʊ/", "zh": "窗户"}]
    }
  ]
}
```

每句 `en`、`zh`、`ipa` 必填，`focus` 可为空数组。`voice` 可覆盖声线，CLI 参数优先。提供源文时保留其内容、顺序和标点，拆分只改变字幕分页。

自然模式按全部句子的顺序组成整段讲述。`target_wpm`、`pause`、`repeats` 属旧校准流程；历史 JSON 的这些值不能使默认自然模式再次变速、插停顿或播放两遍。固定词速需要显式 `--speech-mode calibrated`。已认可 WAV 的视频始终单遍，重复练听可用播放器循环。

## 两种声音指标

| 模式 | 听感与时间轴 | 词速说明 |
|---|---|---|
| `natural`，默认 | 整段 Edge 原生讲述，保留原生停顿；不为 WPM 拉伸或拼句 | 总词数 ÷ 整轨真实时长，注明含自然停顿 |
| `calibrated`，显式选择 | 引擎原生慢读后测量，再做不改变音高的小幅速度校准；可按旧配置逐句安排停顿及重复 | 分别报告正文 WPM 与含新增停顿的全片 WPM |

Edge Jenny 原生 `-20%` 是自然慢讲起点，不能直接换算成某个 WPM。82 词书店示例的完整原轨为 36.312 秒，实测约 135.49 WPM，包含原生停顿。用户明确要正常语速时使用 `--edge-rate=+0%`，不要先生成慢速声音再用 atempo 加快。

## 通过试听后的声音冻结

批准音轨使用一份完整 WAV。推荐主流程产生的 48 kHz、单声道、16-bit PCM；对齐器要求单声道 16-bit PCM。记录文件 SHA256，向制片命令传入 `--approved-audio` 与对应 `--alignment`。

成片直接使用完整 WAV，不重新合成、变速、分句重组、normalize 或补句间留白。导出的原 WAV 副本应哈希相同；MP4 为兼容播放器做 AAC 编码，有损编码字节与解码尾部块补齐不代表重新配音。检查声音轨道时长、波形节奏与字幕时间轴，不能只看 MP4 文件哈希。

只改中文、IPA、字体、尺寸或插画不需要改声音。英文改变、选了新声音或新原生速率时，重新制作试听与对齐；不能把旧音轨或旧时间轴套到新稿件上。

## IPA 核对

可采用 Cambridge 美式宽式：/ɑː/、/ɔː/、/ɝː/、/ɚ/、/e/、/iː/，同一期保持符号与口音一致。句级 IPA 使用正常功能词弱读，例如 `a /ə/`、`of /əv/`；重点词可用词典独立读音。

重点核查同形异音词 `read /riːd/—/red/`、`live /lɪv/—/laɪv/`、`close /kloʊs/—/kloʊz/`，以及时态、数字与缩写。音标是教学参考，不承诺反映所有连读、闪音或逐音时间点。TTS 内部 phonemes、ASR 文本和强制对齐分数不能替代上下文与实际发音检查；ASR 输出 `read` 本身不能区分两种发音。

词级来源：[Cambridge read](https://dictionary.cambridge.org/pronunciation/english/read)、[bookshop](https://dictionary.cambridge.org/us/pronunciation/english/bookshop)、[quiet](https://dictionary.cambridge.org/us/pronunciation/english/quiet)、[window](https://dictionary.cambridge.org/pronunciation/english/window)。句级判断仍需结合全文语境。

## 制作与交付验收

- 先独立试听，再用用户已认可的完整音轨制片。用户已经选声时直接执行，不重复询问。
- 对齐结果绑定 WAV 与英文稿件哈希，字幕连续覆盖整轨；质量不够时核查后重做对齐，不动音轨去配合错误字幕。
- 所有不同字幕画面都有完整英文、IPA、中文，无裁切、方框或重叠。纸面动画只支持精确 16:9 横屏或 9:16 竖屏。
- MP4 包含 H.264/yuv420p 与 AAC，尺寸正确且完整可解码；开始、中段和结尾无缺失。视频末帧取整允许少于一帧的差值，不能裁掉声音结尾。
- 附视频、试听 WAV/MP3、SRT、内容 JSON、封面与时间轴；记录实际声音、原生速率、真实时长和词速指标。保留批准 WAV 的同哈希副本。
- 无法耳听时如实说明已完成的数据、解码与画面验证，不声称主观自然度或每个异音词已听验。

安装、运行、原轨对齐和旧模式见[配音与动画流程](voice-animation-workflow.md)。
