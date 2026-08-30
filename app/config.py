"""应用级配置与路径。"""
import os
import sys

# --------------------------------------------------------------------------- #
# 资源目录：兼容源码运行与 PyInstaller 打包两种形态。
#
# 源码运行时（开发态），资源位于项目源码树 app/resources/...
# 打包后（PyInstaller onefile/onedir），数据文件被解压到 sys._MEIPASS
# （临时目录），而 app 模块路径会指向该临时目录。这里统一用一个资源根目录：
#   - 打包态：sys._MEIPASS（PyInstaller 解压运行时目录）
#   - 源码态：项目根目录（app/ 的上级）
# --------------------------------------------------------------------------- #
if getattr(sys, "frozen", False):  # PyInstaller 打包运行
    _RESOURCE_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    _RESOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只读资源目录（QSS 等，打包时随二进制一起分发）
RESOURCES_DIR = os.path.join(_RESOURCE_ROOT, "app", "resources")

# 项目根目录（源码态用；保留以兼容可能的外部引用）
BASE_DIR = _RESOURCE_ROOT

# --------------------------------------------------------------------------- #
# 数据目录（可写，用户私有）。数据库/日志必须放在用户数据目录，
# 而不是资源根目录——否则打包态会写进只读的 _MEIPASS 临时区，
# 导致数据无法持久化（每次启动丢失）。
# --------------------------------------------------------------------------- #
def _data_dir() -> str:
    """返回可写的用户数据目录（跨平台）。

    优先使用 Qt 的 QStandardPaths（遵循各平台惯例），若 Qt 尚未初始化
    （如入口早期阶段）则回退到用户主目录下的 ~/.lightaibox。
    """
    try:
        from PySide6.QtCore import QStandardPaths
        path = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation)
        if path:
            return os.path.join(path, "LightAIBox")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".lightaibox")


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "lightbox.db")

# 调度策略常量
POLICY_LONG_FIRST = "long_first"    # 长输入优先
POLICY_SHORT_FIRST = "short_first"  # 短输入优先
POLICIES = (POLICY_LONG_FIRST, POLICY_SHORT_FIRST)

# 配额类型
QUOTA_CALLS = "calls"          # 调用次数
QUOTA_TOKENS = "tokens"        # token 数
QUOTA_UNLIMITED = "unlimited"  # 无限制
QUOTA_TYPES = (QUOTA_CALLS, QUOTA_TOKENS, QUOTA_UNLIMITED)

# API 协议类型
API_OPENAI = "openai"
API_ANTHROPIC = "anthropic"
API_BOTH = "both"  # 兼容型：同一 provider 同时支持 OpenAI 与 Anthropic 两种调用协议
API_TYPES = (API_OPENAI, API_ANTHROPIC, API_BOTH)

# 默认 Anthropic 官方 base_url（用于提示，可被用户覆盖）
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# 统一 API 服务（本地 HTTP 网关）
MODEL_AUTO = "auto"                        # 虚拟模型名：触发自适应调度
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8765
