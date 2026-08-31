"""LightAIBox 入口。"""
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from . import db
from .gateway import Gateway
from .server import GatewayServer, ServerConfig
from .ui.main_window import MainWindow, _make_tray_icon


def _setup_high_dpi() -> None:
    """高 DPI 适配：启用 Per-Monitor DPI 感知，避免跨屏拖拽窗口尺寸异常。

    关键：使用 RoundPreferFloor（即 Qt 默认）而非 PassThrough。
    - PassThrough 让每个屏幕保留非整数 DPR（1.25 / 1.5 等），跨屏瞬间
      窗口物理尺寸按新 DPR 跳变，无边框窗口无法正确追踪非整数缩放的几何，
      会把该跳变错误回映射为逻辑尺寸放大 → 窗口「瞬间变大」。
    - RoundPreferFloor 让每个屏幕 DPR 取整（1.0 / 2.0），跨屏重算是离散、
      干净的，逻辑尺寸保持稳定，画面清晰度略降但尺寸正确。

    配合打包时的 Per-Monitor DPI manifest，窗口在系统层即可感知每个
    显示器的 DPI，无需在纯 Windowed 环境下依赖 Qt 的坐标虚拟化。
    """
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor)


def _setup_app_user_model_id() -> None:
    """设置任务栏 AppUserModelID，修复 python.exe 身份下的任务栏图标缓存。

    通过 `python -m app.main` 启动时进程名是 python.exe，Windows 任务栏按
    (exe, AppID) 缓存图标。不设 AppID 时所有 python 进程共享同一个缓存键，
    早期实例（未设窗口图标）缓存的 python 图标会一直显示，即使新实例的
    窗口图标已经正确（WM_GETICON 返回 logo，但任务栏仍显示 python）。

    显式设置稳定 AppID 后缓存键唯一，任务栏立即取用窗口图标。
    """
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LightAIBox")
    except Exception:
        pass  # 非 Windows / 权限受限时静默跳过


def main() -> int:
    _setup_high_dpi()
    _setup_app_user_model_id()
    db.init_db()

    app = QApplication(sys.argv)
    gateway = Gateway()
    server = GatewayServer(gateway)

    # 统一 API 服务默认随应用启动，外部工具开箱即可调用。
    # 使用默认配置（host 127.0.0.1、port 8765，OpenAI / Anthropic 双协议均启用）。
    server.start(ServerConfig())

    win = MainWindow(gateway, server)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
