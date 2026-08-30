"""LightAIBox 入口。"""
import sys

from PySide6.QtWidgets import QApplication

from . import db
from .gateway import Gateway
from .server import GatewayServer, ServerConfig
from .ui.main_window import MainWindow


def main() -> int:
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
