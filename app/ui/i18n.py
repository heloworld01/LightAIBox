"""国际化：中英文实时切换，QSettings 持久化当前语言。"""
from PySide6.QtCore import QSettings

ZH = "zh"
EN = "en"

LANGUAGES = {ZH: "中文", EN: "English"}

_KEY = "ui/language"


class LanguageManager:
    """语言管理单例：提供 tr(key) 按当前语言取词，切语言后由各页面 retranslate 刷新。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lang = QSettings().value(_KEY, ZH)
            if cls._instance._lang not in LANGUAGES:
                cls._instance._lang = ZH
        return cls._instance

    def current(self) -> str:
        return self._lang

    def set_language(self, lang: str) -> None:
        if lang not in LANGUAGES:
            return
        self._lang = lang
        QSettings().setValue(_KEY, lang)

    def toggle(self) -> None:
        self.set_language(EN if self._lang == ZH else ZH)

    def is_zh(self) -> bool:
        return self._lang == ZH

    def tr(self, zh: str, en: str) -> str:
        return zh if self._lang == ZH else en