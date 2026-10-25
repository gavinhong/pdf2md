# 第三方组件与模型

应用复用第三方库，权利仍归原作者。发行目录 `licenses/` 保存当前安装包提供的许可及 NOTICE 文本；模型文件的来源、版本配置与 SHA-256 位于 `models/manifest.json`。本文件不是对上游许可的替代。

| 组件 | 来源 | 上游声明的许可证 |
| --- | --- | --- |
| RapidDoc 0.9.11 | https://github.com/RapidAI/RapidDoc/tree/v0.9.11 | Apache-2.0；保留其内嵌模块的单独声明 |
| RapidOCR 3.10.0 | https://github.com/RapidAI/RapidOCR | Apache-2.0 |
| ONNX Runtime 1.23.2 | https://github.com/microsoft/onnxruntime | MIT；内嵌组件另见其 ThirdPartyNotices |
| pywebview 6.2.1 | https://github.com/r0x0r/pywebview | BSD-3-Clause |
| pypdfium2 / PDFium | https://github.com/pypdfium2-team/pypdfium2 | Apache-2.0 / BSD-3-Clause 及 PDFium 第三方声明 |
| jdeskew 0.4.2 | https://github.com/phamquiluan/jdeskew | MIT |
| OpenCV | https://github.com/opencv/opencv | Apache-2.0 及附带第三方声明 |
| markdown-it | https://github.com/markdown-it/markdown-it | MIT |
| CodeMirror 6 / Lezer | https://github.com/codemirror/dev / https://github.com/lezer-parser | MIT；具体模块版本由 package-lock.json 固定 |
| markdown-it-texmath | https://github.com/goessner/markdown-it-texmath | MIT |
| KaTeX | https://github.com/KaTeX/KaTeX | MIT；字体声明随包保留 |
| DOMPurify | https://github.com/cure53/DOMPurify | Apache-2.0 OR MPL-2.0 |
| Lucide | https://github.com/lucide-icons/lucide | ISC，部分图标为 MIT |

版面、公式、OCR 与方向模型来自 RapidDoc / RapidAI 提供的 PaddleOCR 系列 ONNX 转换件；表格模型来自 RapidDoc 采用的 RapidTable / TableStructureRec 模型。上游原始模型项目为 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[RapidTable](https://github.com/RapidAI/RapidTable)、[TableStructureRec](https://github.com/RapidAI/TableStructureRec)。清单逐项保留实际下载来源；模型转换不会改变上游权利归属。Magika 分类器来自 [Google Magika](https://github.com/google/magika)，其声明为 Apache-2.0。

Microsoft WebView2 固定运行时从 [微软官方分发端点](https://developer.microsoft.com/microsoft-edge/api/webview2) 获取，保持原文件与附带许可。其版本、来源及归档摘要保存在 `runtime/webview2-manifest.json`。它是微软的可再分发运行时，不适用本项目或上述开源库的许可证。

开发使用的 PyInstaller 采用 GPL 并提供应用打包例外；请保留其发行包所附声明。`scripts/collect_licenses.py` 按本地安装内容收集许可，构建脚本将这些文本一并复制到发行目录。
