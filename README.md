# PDF2MD

本地 PDF 转 Markdown 桌面工具。复用 RapidDoc、RapidOCR、ONNX Runtime 与 pywebview，支持中英文扫描件、表格、分栏阅读顺序和 LaTeX 公式。0.2.1 优先显示界面，在顶部提示历史恢复、模型预热和文档加载；预热期间可提交转换并自动排队。支持原 PDF 与 Markdown 对照核对、直接编辑保存和核对进度。界面、图片和公式渲染资源全部本地提供，不需要 API Key 或显卡。

## 使用

Windows 完整便携包：解压后双击 `PDF2MD/PDF2MD.exe`，保持同目录的 `_internal`、`models`、`runtime` 文件夹完整。建议解压到可读写的本地目录。无需安装 Python、Node.js 或单独下载模型。完整包含 Microsoft Fixed Version WebView2；macOS 使用系统 WKWebView。

当前源码工作目录已准备 Python 环境和模型，可直接双击 `run.cmd`。也可运行：

```powershell
.venv\Scripts\python.exe run.py
```

1. 点击「添加 PDF」多选文件，或把 PDF 拖入桌面窗口。
2. 选择输出目录；页码使用 PDF 物理页码，从 1 开始，例如 `1-5,8`。
3. 点击「开始转换」。可以取消当前文件或整个批次，已完成的结果会保留。
4. 选中已完成任务，切换渲染/源码预览、复制完整 Markdown、打开文件或结果目录。

每个结果目录包含同名 `.md`、`images/`、`content_list.json` 和 `conversion-report.json`；使用核对功能后另有 `review-state.json` 保存核对进度和定位。文件夹同名时自动添加编号，避免覆盖已有结果。复制或移动结果时，请一起保留这些文件和 `images/`。

默认自动判断文字页/扫描页。遇到文本层乱码或数学字体编码异常，可选择「强制 OCR」。简单表格转 Markdown，合并单元格等复杂表格保留 HTML。外部 Markdown 阅读器需要支持 HTML 表格与 KaTeX/MathJax 数学语法；本应用的预览已内置。

## 对照核对与编辑

1. 转换完成后，点击「对照核对」或任意问题提示进入核对工作区；也可以打开已有转换结果。
2. 左侧选择问题，中间查看原 PDF 对应的物理页和高亮区域，右侧对照 Markdown 源码及实时预览。续表提示可以查看上一页。
3. 直接修改源码。使用「保存」或 `Ctrl+S`（macOS 为 `Cmd+S`）保存到原 Markdown 文件；支持中文输入、撤销和重做。
4. 确认内容后点击「标记已核对」。有未保存修改时，按钮为「保存并标记已核对」。可撤销核对；修改某个已核对块会使该块的相关提示恢复为待核对。
5. 全部问题保存为已核对后，任务显示「完成 · 已核对」。原始识别警告和回退图片仍保留，供追溯。

首次修改正文时，在同目录保留 `文件名.original.md` 原始备份；后续保存不会覆盖该备份。

导出的任务报告包含已核对、待核对数量；原始 `conversion-report.json` 保留转换时的诊断。移动结果后以随附的 `review-state.json` 恢复核对状态。

问题列表提供「待核对／全部」筛选与上一项、下一项，能够访问全部提示。PDF 支持翻页、输入页码、缩放和适应宽度。窄窗口时列表可折叠，PDF 与 Markdown 上下排列。

切换任务、返回转换或退出时，未保存修改会提示保存、放弃或继续编辑。重启程序后恢复任务及已保存的核对进度；移除任务只移除列表记录，不删除源文件或结果。

旧版结果缺少精确定位时使用页级提示，不凭文本相似度猜测高亮区域；重新转换可获得完整定位。源 PDF 移动后可重新关联，缺失时仍可编辑 Markdown。检测到源文件发生变化时暂停区域定位，需关联原文件或重新转换。

若同时用其他编辑器修改同一个 Markdown，保存时会提示版本冲突并保留当前草稿；可复制草稿或重新载入。重新载入无法追踪的外部修改会将相关文档的全部提示恢复为待核对。单次编辑保存支持最多 32 MiB UTF-8 正文；超限或写入失败不覆盖原文，界面保留草稿。Markdown 已保存而核对进度未写入时会分别说明结果。

「已核对」记录用户确认，不能证明识别绝对正确。公式即使能正常渲染，仍需检查符号与数学结构；表格重点检查行列、数值、单位和续表衔接。

## 本地模型

`models/` 已保存 12 项模型及字典，共 **809,504,980 字节（约 772 MiB）**。`models/manifest.json` 记录来源、大小和 SHA-256。首次转换会完整校验模型，推理进程禁止连接外网，不会隐式下载其他模型。

```powershell
.venv\Scripts\python.exe run.py --check-models
```

如需重新准备模型，运行 `.venv\Scripts\python.exe scripts\download_models.py`。也可设置环境变量 `PDF2MD_MODEL_DIR` 指向包含清单的其他本地模型目录。下载脚本供开发和修复使用，正常用户无需运行。

PP-FormulaNet Plus M 的权重约 566 MiB，是模型体积的主要部分。当前 CPU 版本未包含 PyTorch、CUDA、OpenVINO、Electron 或其他大模型，保留公式识别质量所需的模型。

## 开发与构建

使用 Python **3.11**、Node.js **22 或更新版本**。Windows 与 macOS 共用 `src/`、`frontend/`、模型和 PyInstaller spec；发布包必须在相应系统和 CPU 架构原生构建。Windows x64 已在当前环境执行；macOS 构建配置尚待真实 Mac 验证。

```text
python -m venv .venv
# Windows
.venv\Scripts\python.exe -m pip install -e ".[dev]"
# macOS
.venv/bin/python -m pip install -e ".[dev]"

npm ci
npm run build
```

RapidDoc 0.9.11 尚未提供 macOS wheel；`pyproject.toml` 为 macOS 固定使用该发布版本对应的源码提交，关闭仅供 CUDA 使用的原生算子。ONNX Runtime 固定为同时提供 Intel Mac、Apple Silicon 与 Windows wheel 的 1.23.2。

准备模型、打包（macOS 替换 Python 可执行路径）：

```powershell
.venv\Scripts\python.exe scripts\download_models.py
.venv\Scripts\python.exe scripts\build_portable.py --with-webview --zip
```

产物位于 `dist/`。`--with-webview` 仅在 Windows 生效，用微软官方 API 下载固定运行时；不携带它可缩小包体，但该包要求系统已有 WebView2。模型位于普通文件目录，采用 PyInstaller onedir，避免每次启动解压数百 MB 权重。macOS 构建后的签名、公证需要开发者自己的 Apple 证书；本仓库不包含证书。

```powershell
.venv\Scripts\python.exe -m pytest -q
npm run typecheck
npm run tokens:check
npm run test:ui
```

界面测试在 Windows 使用已安装的 Edge；其他平台先执行 `npx playwright install chromium`。测试 PDF 为工作目录提供的样本及其扫描派生版本，仅用于本地验证，不会放入应用发行包。

浏览器验证入口：`.venv\Scripts\python.exe run.py --web --port 8765`。文件选择、系统打开和退出桌面窗口操作需要原生窗口；浏览器入口用于开发验证。

命令行也可使用相同引擎：

```powershell
.venv\Scripts\python.exe run.py --convert "example.pdf" --pages "1-3,8" --output "output"
```

## 范围与质量

单文件上限 100 MiB，每批最多 100 个文件，每文件每次最多选择 300 页。加密或损坏文件会提示原因；先解密再导入。结果目录仅在转换完成并校验后发布，取消时清理该任务的临时文件。

印刷体是主要识别对象。模糊、遮挡、低分辨率和手写内容，以及复杂公式和特殊表格，仍需对照原文检查。识别模型可能输出语法合法但内容不准确的结果；“转换成功”表示处理流程完成，不能代替内容质量验收。跨页表格保留结构化分段，候选续表会提示核对。完整 300 页性能、所有公式类型及两种 Mac 架构尚未完成验收，详见 `docs/verification.md`。

配置和任务索引保存在系统用户配置目录，临时浏览器数据和桌面日志保存在用户缓存目录。输出报告不包含 PDF 正文；`content_list.json` 包含识别内容，`review-state.json` 包含核对进度和文档指纹，请按文档本身管理。

文档：[需求](REQUIREMENTS.md)、[技术设计](TECHNICAL_DESIGN.md)、[验证记录](docs/verification.md)、[第三方声明](THIRD_PARTY_NOTICES.md)。


启动时先显示工作区，随后恢复设置与历史任务。恢复完成即可添加文件、调整设置和开始转换；识别资源尚未就绪的任务显示“等待识别资源”，预热完成后自动执行。顶部中间显示当前文档、PDF 页或编辑器的加载阶段，右侧保留后台识别资源状态。预热失败时保留排队任务，点击顶部“重试”即可继续；取消排队任务不会停止独立的模型预热。加载与性能说明见 [docs/startup.md](docs/startup.md)。
