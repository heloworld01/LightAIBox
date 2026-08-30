"""LightAIBox 打包入口。

用绝对导入调用 app.main.main()。作为顶层脚本提供给 PyInstaller 打包，
避免 app/main.py 中相对导入被当作顶层模块执行时的 ImportError。

开发态仍可用 `python -m app.main` 启动（保持不变）。
"""
import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())