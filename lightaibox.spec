# -*- mode: python ; coding: utf-8 -*-
"""LightAIBox PyInstaller 打包配置。

用法:
    pyinstaller lightaibox.spec

说明:
  - 使用 onedir 模式（--onedir），启动更快、病毒扫描误报更少，
    相比 onefile 更稳定（uvicorn 守护线程 + Qt 插件加载更可靠）。
  - 资源目录 app/resources/ 随二进制一并打入，运行时经 config.RESOURCES_DIR
    在 sys._MEIPASS 下解析。
  - uvicorn 使用动态 import，显式声明 hiddenimports 避免被裁剪。
  - 数据库写入用户数据目录（config.DATA_DIR），不随包分发。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Per-Monitor DPI 感知 manifest：Windows 跨不同缩放比例屏幕拖拽窗口时，
# 告诉系统按「每个显示器自己的 DPI」处理，避免窗口尺寸异常放大。
_PROJECT_DIR = os.path.abspath(".")
_MANIFEST = os.path.join(_PROJECT_DIR, "app", "resources",
                         "lightaibox.exe.manifest")
# 打包后 exe 自身的图标（资源管理器 / 任务栏未设置窗口图标时的兜底）
_ICON = os.path.join(_PROJECT_DIR, "app", "resources", "logo.ico")

# uvicorn 及其 loop 实现、日志依赖使用运行时动态导入，需显式收集。
hiddenimports = collect_submodules("uvicorn") + \
                collect_submodules("uvicorn.loops") + \
                collect_submodules("uvicorn.protocols") + \
                collect_submodules("fastapi") + [
                    "anyio",
                    "anyio._backends",
                    "anyio._backends._asyncio",
                    "h11",
                    "websockets",
                    "httptools",
                ]

a = Analysis(
    ["run.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[
        # 只读资源（QSS 主题文件）
        ("app/resources", "app/resources"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "IPython",
        "jupyter",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LightAIBox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无控制台窗口（GUI 应用）
    disable_windowed_traceback=False,
    manifest=_MANIFEST,  # 嵌入 Per-Monitor DPI 感知 manifest
    icon=_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LightAIBox",
)