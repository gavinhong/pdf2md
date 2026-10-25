# One source/spec, native per-platform builds. Models stay in an ordinary sibling directory.
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
data = [(str(root / "src/pdf2md/web"), "pdf2md/web")]
data += collect_data_files("rapid_doc", excludes=["**/*.onnx", "**/*.safetensors", "**/onnx_ops/lib/*"])
data += collect_data_files("rapid_doc", includes=["model/magika/**/*.onnx"])
data += collect_data_files("rapidocr", excludes=["**/*.onnx"])
# RapidDoc's table orientation helper still uses this fixed package path.
# Reuse the verified local model; no extra model or implicit download.
data += [(str(root / "models/rapid_orientation.onnx"), "rapid_doc/resources")]
for package in ("rapid-doc", "rapidocr", "onnxruntime", "pywebview", "pypdfium2", "pdftext"):
    data += copy_metadata(package)
hidden = collect_submodules("rapid_doc", filter=lambda name: not any(part in name for part in (".api.", ".gradio", ".torch", ".pytorch")))
hidden += collect_submodules("rapidocr")
hidden += ["webview.platforms.edgechromium", "webview.platforms.winforms"] if sys.platform == "win32" else ["webview.platforms.cocoa"]
a = Analysis([str(root / "run.py")], pathex=[str(root / "src")], binaries=[], datas=data,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[],
             excludes=["pytest", "ruff", "tkinter", "torch", "torchvision", "openvino", "onnxruntime_gpu",
                       "matplotlib", "IPython", "notebook", "gradio", "fastapi", "uvicorn"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PDF2MD", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PDF2MD")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="PDF2MD.app", bundle_identifier="local.pdf2md.desktop",
                 info_plist={"NSHighResolutionCapable": True, "NSHumanReadableCopyright": "Third-party notices included in distribution."})
