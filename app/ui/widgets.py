"""通用小组件：状态胶囊标签、区块标题、自定义消息框，以及状态色 / 语义映射常量。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .i18n import LanguageManager


# 状态文字（中文） -> 语义关键词（供 StatusBadge 使用）；英文语义另行判断
STATUS_SEMANTICS = {
    "运行中": "success",
    "已停用": "neutral",
    "已超配额": "warning",
    "已自动关闭": "warning",
    "已自动关闭(连接异常)": "danger",
    "已自动关闭(超配额)": "warning",
}

# 英文状态文字 -> 语义关键词
STATUS_SEMANTICS_EN = {
    "Running": "success",
    "Disabled": "neutral",
    "Quota exceeded": "warning",
    "Auto-disabled": "warning",
    "Auto-disabled (connection error)": "danger",
    "Auto-disabled (quota)": "warning",
}

# 日志状态语义映射
LOG_STATUS_SEMANTICS = {
    "成功": "success",
    "失败": "danger",
    "Success": "success",
    "Failed": "danger",
}


def status_badge_semantic(status_text: str) -> str:
    """根据状态文字返回语义关键词（success / warning / danger / neutral）。"""
    if LanguageManager().is_zh():
        return STATUS_SEMANTICS.get(status_text, "neutral")
    return STATUS_SEMANTICS_EN.get(status_text, "neutral")


def log_status_semantic(status_text: str) -> str:
    """日志状态文字 -> 语义关键词。"""
    return LOG_STATUS_SEMANTICS.get(status_text, "neutral")


class StatusBadge(QLabel):
    """状态胶囊标签：「● 文字」。

    结构为 6px 圆点 + 4px 间距 + 12px Medium 文字；背景用对应语义色 10%
    透明度、圆角 10px、内边距 2px 10px。通过 set_status(text, semantic) 更新。
    """

    _DOT_STYLES = {
        "success": "#34D399",
        "warning": "#FBBF24",
        "danger": "#F87171",
        "neutral": "#6B7280",
    }

    def __init__(self, text: str = "", semantic: str = "neutral", parent=None):
        super().__init__(parent)
        self._dot_color = "#6B7280"
        self.set_status(text, semantic)

    def set_status(self, text: str, semantic: str) -> None:
        """更新胶囊文字与语义色。

        semantic: "success" | "warning" | "danger" | "neutral"
        """
        self._dot_color = self._DOT_STYLES.get(semantic, "#6B7280")
        # 通过 property 让 QSS 按语义命中背景/文字色（见 styles/*.qss）
        self.setProperty("class", "status-badge-" + (semantic or "neutral"))
        self._restyle()
        # QLabel 无法单独给圆点上色，改用富文本拼一个彩色圆点 + 文字
        self.setText(
            f'<span style="color:{self._dot_color};">●</span>'
            f'&nbsp;&nbsp;{text}')

    def _restyle(self):
        """property 变更后强制刷新样式。"""
        self.style().unpolish(self)
        self.style().polish(self)


class SectionHeader(QWidget):
    """区块标题 + 底部分割线。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setProperty("class", "section-title")
        layout.addWidget(self.title_label)

        divider = QFrame()
        divider.setProperty("class", "section-divider")
        layout.addWidget(divider)

        # 分割线与内容间距由外部布局的 spacing 控制（12px）
        self._divider = divider


class MessageBox(QDialog):
    """无边框 + 四角圆角的消息框，接口对齐 QMessageBox 常用静态方法。

    原生 QMessageBox 使用系统标题栏，无法圆角；这里自绘标题栏和内容，
    返回 QMessageBox.StandardButton 以便调用方沿用 `== QMessageBox.Yes` 判断。
    提供 information / warning / question 三个静态方法（question 返回 Yes/No）。
    """

    def __init__(self, icon, title: str, text: str, buttons,
                 parent=None, close_value=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._result = None
        # 点右上角 ✕ 时返回的值：调用方可传 QMessageBox.Cancel 等哨兵值，
        # 表示「仅关闭弹窗、不执行任何动作」（默认 Ok，沿用 QMessageBox 语义）
        self._close_value = close_value if close_value is not None else QMessageBox.Ok
        self.setMinimumWidth(420)

        # 圆角背景载体
        bg = QFrame()
        bg.setProperty("class", "dialog-card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(bg)

        root = QVBoxLayout(bg)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自绘标题栏
        hdr = QFrame()
        hdr.setProperty("class", "dialog-header")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(20, 12, 12, 8)
        hl.setSpacing(8)
        title_label = QLabel(title)
        title_label.setProperty("class", "dialog-title")
        hl.addWidget(title_label)
        hl.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setProperty("class", "ghost")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(self._on_close)
        hl.addWidget(close_btn)
        root.addWidget(hdr)

        # 内容：图标 + 文本
        body = QHBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(12)
        if icon is not None:
            icon_label = QLabel(self._icon_text(icon))
            icon_label.setProperty("class", "msg-icon")
            icon_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            body.addWidget(icon_label)
        self.text_label = QLabel(text)
        self.text_label.setWordWrap(True)
        self.text_label.setProperty("class", "msg-text")
        body.addWidget(self.text_label, 1)
        root.addLayout(body)

        # 按钮
        bl = QHBoxLayout()
        bl.setContentsMargins(20, 6, 20, 20)
        bl.setSpacing(8)
        bl.addStretch()
        primaries = {QMessageBox.Ok, QMessageBox.Yes, QMessageBox.Save}
        for b in buttons:
            # 元组 (StandardButton, "文字")：身份取第一个元素，文字走 _button_text
            value = b[0] if isinstance(b, tuple) else b
            btn = QPushButton(self._button_text(b))
            btn.setProperty("class", "primary" if value in primaries else "ghost")
            btn.clicked.connect(lambda _=False, val=value: self._accept(val))
            bl.addWidget(btn)
        root.addLayout(bl)

    # ------------------------------------------------------------------ #
    # 结果与交互
    # ------------------------------------------------------------------ #
    def _accept(self, value):
        self._result = value
        self.accept()

    def _on_close(self):
        # 关闭等价于取消：question 场景返回 No，其余返回 Ok
        self._result = self._close_value
        self.reject()

    def result(self):
        return self._result

    @staticmethod
    def _icon_text(icon) -> str:
        mapping = {QMessageBox.Information: "ℹ",
                   QMessageBox.Warning: "⚠",
                   QMessageBox.Question: "?"}
        return mapping.get(icon, "ℹ")

    @staticmethod
    def _button_text(b) -> str:
        # 支持 (StandardButton, "自定义文字") 元组：文字用自定义值，
        # 按钮身份取元组第一个元素（供调用方区分选择）
        if isinstance(b, tuple):
            return b[1]
        mapping = {QMessageBox.Ok: "OK", QMessageBox.Yes: "是/Yes",
                   QMessageBox.No: "否/No", QMessageBox.Cancel: "取消/Cancel"}
        return mapping.get(b, "OK")

    # ------------------------------------------------------------------ #
    # 静态入口
    # ------------------------------------------------------------------ #
    @staticmethod
    def information(parent, title, text):
        dlg = MessageBox(QMessageBox.Information, title, text,
                         [QMessageBox.Ok], parent)
        dlg.exec()
        return dlg.result()

    @staticmethod
    def warning(parent, title, text):
        dlg = MessageBox(QMessageBox.Warning, title, text,
                         [QMessageBox.Ok], parent)
        dlg.exec()
        return dlg.result()

    @staticmethod
    def question(parent, title, text):
        dlg = MessageBox(QMessageBox.Question, title, text,
                         [QMessageBox.Yes, QMessageBox.No], parent)
        dlg._close_value = QMessageBox.No
        dlg.exec()
        return dlg.result()

    # 无边框拖动
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and hasattr(self, "_drag_pos"):
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if hasattr(self, "_drag_pos"):
            del self._drag_pos