"""主题管理：加载并应用暗/亮主题 QSS，QSettings 持久化用户选择。"""
import os

from PySide6.QtCore import QSettings
from PySide6.QtGui import QGuiApplication

from .. import config

_STYLES_DIR = os.path.join(config.RESOURCES_DIR, "styles")

# 主题名 -> QSS 文件映射
THEME_FILES = {
    "dark": os.path.join(_STYLES_DIR, "dark.qss"),
    "light": os.path.join(_STYLES_DIR, "light.qss"),
}

_SETTINGS_KEY = "ui/theme"


class ThemeManager:
    """主题管理单例。

    支持暗色（默认）/ 亮色 / 跟随系统。切换时重新加载对应 .qss 文件，
    无需重启应用。
    """

    DARK = "dark"
    LIGHT = "light"
    SYSTEM = "system"

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_singleton()
        return cls._instance

    def _init_singleton(self):
        self._theme = self._load_theme()

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #
    def apply_theme(self, theme: str) -> None:
        """加载并应用指定主题的 QSS。"""
        if theme not in (self.DARK, self.LIGHT):
            theme = self.DARK
        qss_path = THEME_FILES[theme]
        with open(qss_path, "r", encoding="utf-8") as f:
            qss = f.read()
        QGuiApplication.instance().setStyleSheet(qss)
        self._theme = theme
        self._save_theme(theme)

    def toggle(self) -> None:
        """切换暗/亮主题（当前为暗则切亮，否则切暗）。"""
        self.apply_theme(self.LIGHT if self._theme == self.DARK else self.DARK)

    def current(self) -> str:
        """返回当前实际主题名（dark / light）。"""
        return self._theme

    # ------------------------------------------------------------------ #
    # 持久化与系统主题解析
    # ------------------------------------------------------------------ #
    def _resolve_preference(self) -> str:
        """把用户偏好（含 system）解析为实际主题名（dark / light）。"""
        pref = QSettings().value(_SETTINGS_KEY, self.DARK)
        if pref == self.SYSTEM:
            return self._system_theme()
        if pref in (self.DARK, self.LIGHT):
            return pref
        return self.DARK

    def _system_theme(self) -> str:
        """根据系统配色方案返回 dark / light。"""
        try:
            scheme = QGuiApplication.styleHints().colorScheme()
            from PySide6.QtCore import Qt
            if scheme == Qt.ColorScheme.Dark:
                return self.DARK
            return self.LIGHT
        except Exception:
            return self.DARK

    def _load_theme(self) -> str:
        return self._resolve_preference()

    def _save_theme(self, theme: str) -> None:
        QSettings().setValue(_SETTINGS_KEY, theme)