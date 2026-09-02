"""透明悬浮 Provider 状态面板：垂直列出「已启动（运行中）」的 provider 及其用量/余量。

行为与视觉约定：
- 无边框 + 置顶 + 工具窗（不占用系统任务栏 / Alt+Tab），屏幕底部浮动展示。
- 面板为独立顶层窗口：展示/隐藏只受主窗口上的开关控制，不因主窗口隐藏而消失。
- 背景固定为半透明深色圆角（**不跟随应用明暗主题**，视觉稳定）。
- 每个运行中的 provider 占一行：状态圆点 + 名称 + 模型（上行），用量/余量（下行）。
- 底部右下角一个自绘挂锁按钮：锁定后禁止拖拽，解锁后才可拖动移动。
- 周期性轮询网关缓存，仅当「运行中集合」变化时才重建 UI。
"""
import ctypes
import typing

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from .. import config
from .i18n import LanguageManager

if typing.TYPE_CHECKING:
    from ..gateway import Gateway
    from ..models import Provider

# 固定配色（不随明暗主题变化）-------------------------------------------- #
_BG = QColor(16, 17, 22, 215)          # 面板底：近黑半透明
_BORDER = QColor(255, 255, 255, 38)    # 面板描边：半透明白
_ROW = QColor(255, 255, 255, 20)       # 单行底色
_TEXT = QColor(245, 246, 250)          # 名称文字
_SUB = QColor(165, 170, 185)           # 模型 / 用量等次要文字
_DOT_ON = QColor(61, 220, 132)         # 运行中圆点：绿色

# 几何常量 ---------------------------------------------------------------- #
PANEL_W = 300      # 面板固定宽度
CELL_H = 54        # 单行高度（上下两行文本）
PAD_X = 14         # 面板左右内边距
PAD_Y = 10         # 面板上下内边距
ROW_GAP = 6        # 相邻行间距
RADIUS = 16        # 面板圆角
GAP = 12           # 距可用区域底部的距离（避开系统任务栏）
REFRESH_MS = 1200  # 轮询刷新间隔
LOCK_W = 30        # 锁定按钮尺寸
LOCK_H = 30
FOOTER_H = LOCK_H  # 底部锁定按钮条高度


def _fmt(n: int) -> str:
    """把大数字简写：1234 -> 1.2k，1200000 -> 1.2M。"""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n / 1e3:.1f}k".rstrip("0").rstrip(".")
    return str(n)


class _ProviderCell(QWidget):
    """一个 provider 的竖排整行色块。

    上行：状态圆点 + 名称（粗）… 模型（右对齐，次要色）
    下行：用量 / 余量（次要色）
    """

    def __init__(self, provider: "Provider", parent=None):
        super().__init__(parent)
        self._p = provider
        self.setFixedHeight(CELL_H)
        self.setToolTip(self._tooltip())

    def _name_font(self) -> QFont:
        f = QFont()
        f.setPixelSize(14)
        f.setBold(True)
        return f

    def _sub_font(self) -> QFont:
        f = QFont()
        f.setPixelSize(12)
        return f

    def _quota_text(self) -> str:
        """用量 / 余量文本（quota_type + 配额给定时才有余量概念）。"""
        p = self._p
        tr = LanguageManager().tr
        if p.quota_type == config.QUOTA_CALLS:
            lim, used = p.quota_limit, p.used_calls
            return (f"{tr('已用', 'Used')} {used}/{lim} · "
                    f"{tr('余', 'left')} {max(0, lim - used)} {tr('次', 'calls')}")
        if p.quota_type == config.QUOTA_TOKENS:
            lim, used = p.quota_limit, p.used_tokens
            return (f"{tr('已用', 'Used')} {_fmt(used)}/{_fmt(lim)} · "
                    f"{tr('余', 'left')} {_fmt(max(0, lim - used))} tokens")
        return tr("无限制", "Unlimited")

    def _tooltip(self) -> str:
        tr = LanguageManager().tr
        protocol = {
            "openai": "OpenAI", "anthropic": "Anthropic",
            "both": "OpenAI / Anthropic",
        }.get(self._p.api_type, self._p.api_type)
        return (f"{self._p.name}  ·  {self._p.model}\n"
                f"{tr('协议', 'Protocol')}: {protocol}\n"
                f"{tr('配额', 'Quota')}: {self._quota_text()}")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 行底色
        p.setPen(QPen(QColor(255, 255, 255, 14), 1))
        p.setBrush(_ROW)
        p.drawRoundedRect(QRect(self.rect()).adjusted(0, 0, -1, -1), 10, 10)

        name = self._p.name or "?"
        model = self._p.model or ""
        fm1 = QFontMetrics(self._name_font())
        fm2 = QFontMetrics(self._sub_font())

        # 上行：圆点 + 名称 + 右对齐模型
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_DOT_ON)
        p.drawEllipse(QPoint(16, 22), 4, 4)
        p.setFont(self._name_font())
        p.setPen(_TEXT)
        p.drawText(28, 27, name)
        p.setFont(self._sub_font())
        p.setPen(_SUB)
        p.drawText(self.width() - 10 - fm2.horizontalAdvance(model), 27, model)

        # 下行：用量 / 余量
        p.setPen(_SUB)
        p.drawText(28, 46, self._quota_text())
        p.end()


class _LockButton(QWidget):
    """锁定/解锁开关：自绘挂锁图标，色值固定（不随明暗主题）。

    锁定后面板无法拖动；解锁后可拖动。点击切换并发出 toggled(bool)。
    """

    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(LOCK_W, LOCK_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._locked = False

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        if locked != self._locked:
            self._locked = locked
            self.update()
            self.setToolTip(self._tip())
            self.toggled.emit(locked)

    def _tip(self) -> str:
        tr = LanguageManager().tr
        return (tr("已锁定：点击解锁后可拖动", "Locked: click to unlock & drag")
                if self._locked else
                tr("未锁定：点击锁定后不可拖动", "Unlocked: click to lock"))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_locked(not self._locked)
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        locked = self._locked
        # 状态色：锁定 → 绿色调高亮；解锁 → 中性灰。色块+锁图标整体变色，
        # 一眼即可分辨状态，不依赖形状细节。
        if locked:
            accent = QColor(61, 220, 132)          # 锁定绿
            bg = QColor(61, 220, 132, 46)          # 绿色半透明底
            border = QColor(61, 220, 132, 110)
        else:
            accent = _SUB                          # 解锁灰
            bg = _ROW
            border = QColor(255, 255, 255, 14)
        p.setPen(QPen(border, 1))
        p.setBrush(bg)
        p.drawRoundedRect(QRect(self.rect()).adjusted(0, 0, -1, -1), 10, 10)
        # 挂锁：锁身 + 锁梁（闭合 / 张开）
        pen = QPen(accent, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        body = QRectF(8, 16, 14, 8)
        p.setBrush(accent)
        p.drawRoundedRect(body, 2, 2)
        p.setBrush(Qt.BrushStyle.NoBrush)
        shackle = QRectF(9, 12, 12, 12)
        if locked:
            p.drawArc(shackle, 180 * 16, 180 * 16)      # 闭合锁梁
        else:
            p.drawArc(shackle, 200 * 16, 140 * 16)      # 张开的锁梁（缺口朝下）
        p.end()


class ProvidersBar(QWidget):
    """透明悬浮面板：垂直列出所有运行中的 provider 及其用量/余量。"""

    def __init__(self, gateway: "Gateway", parent=None):
        super().__init__(parent)
        self._gateway = gateway
        self._cells = []   # List[_ProviderCell]
        # 重建签名：运行中 provider 的 (id, 用量, 余量相关字段) —— ID 集合不变时
        # 若用量/配额变化也要重建，保证「用量/余量」实时准确
        self._sig = ()
        self._locked = False    # 锁定后禁止拖动（默认解锁）
        self._drag_off = None   # 拖动偏移（None 表示未在拖动）
        self._dragging = False

        # 用普通顶层窗口（Qt.Window）而非 Qt.Tool：Qt.Tool 的无父窗口在 Windows
        # 上不参与激活/焦点，OS 会把真正的点击派发给其背后的活动窗口，导致面板
        # 点不到、拖不动。普通窗口可正常接收点击。隐藏系统任务栏改由原生
        # WS_EX_TOOLWINDOW 样式的 _hide_from_taskbar() 完成（见 set_bar_visible）。
        self.setWindowFlags(Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint
                            | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._taskbar_hidden = False
        # 仅首次显示时自动居中（底部）；之后保持用户拖过的位置，不被重置
        self._needs_initial_place = True

        # 外层：行区 + 底部锁定按钮条（锁定按钮跨刷新复用，状态保留）
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
        self._layout.setSpacing(ROW_GAP)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(ROW_GAP)
        self._layout.addLayout(self._rows_layout)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(0)
        foot.addStretch(1)
        self._lock_btn = _LockButton()
        self._lock_btn.toggled.connect(self._on_lock_toggled)
        foot.addWidget(self._lock_btn)
        self._layout.addLayout(foot)

        self.setFixedWidth(PANEL_W)

        # 拖动采用事件过滤器：装在本面板上，可拦截「面板自身 + 全部子控件（每行）」
        # 的鼠标事件，拖动无需依赖事件向父级传播，与主窗口的自实现拖动同理，最可靠。
        self.installEventFilter(self)

        # 初始数据 + 定位（主屏底部居中，自动避开系统任务栏）
        self._refresh()
        self._reposition()
        ag = QGuiApplication.instance()
        for sig in (ag.screenAdded, ag.screenRemoved, ag.primaryScreenChanged):
            sig.connect(self._reposition)

        # 轮询刷新（记录读取廉价，且不引入 DB 信号依赖）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)

    # ------------------------------------------------------------------ #
    # 数据刷新
    # ------------------------------------------------------------------ #
    def _refresh(self):
        running = [p for p in self._gateway.providers.list() if p.is_available()]
        sig = tuple((p.id, p.used_calls, p.used_tokens, p.quota_type,
                     p.quota_limit) for p in running)
        if sig == self._sig:
            return
        self._sig = sig
        # 仅重建行区（底部锁定条不动，保留锁定状态）
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cells = [_ProviderCell(p, self) for p in running]
        for c in self._cells:
            self._rows_layout.addWidget(c)
        # 面板高度 = 上下内边距 + Σ行高 + Σ行距 + 底部锁定条
        self.setFixedHeight(PAD_Y * 2 + len(self._cells) * CELL_H
                            + len(self._cells) * ROW_GAP + FOOTER_H)

    def _on_lock_toggled(self, locked: bool) -> None:
        """锁定 / 解锁：锁定后禁止拖动。"""
        self._locked = locked

    # ------------------------------------------------------------------ #
    # 定位 / 拖动
    # ------------------------------------------------------------------ #
    def _reposition(self):
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()          # 已扣除系统任务栏
        w = self.width() or PANEL_W
        h = self.height() or self.sizeHint().height()
        x = geo.x() + max(0, (geo.width() - w) // 2)
        y = geo.y() + geo.height() - GAP - h
        self.setGeometry(x, y, w, h)

    def _hide_from_taskbar(self) -> None:
        """给窗口加原生 WS_EX_TOOLWINDOW 样式，从系统任务栏/Alt+Tab 隐藏。

        不通过 Qt.Tool 实现（那会让窗口失去激活、点不到），而是保持普通窗口
        的可点击性，仅借 Windows 扩展样式隐藏任务栏入口。
        """
        if self._taskbar_hidden:
            return
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongPtrW(
                hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
            self._taskbar_hidden = True
        except Exception:
            pass  # 非 Windows / 权限受限时静默跳过

    def set_bar_visible(self, show: bool) -> None:
        """外部开关：显示/隐藏悬浮面板。"""
        if show:
            if self._needs_initial_place:
                # 仅首次显示自动居中底部；之后恢复/隐藏只保持现有位置
                self._reposition()
                self._needs_initial_place = False
            self.show()
            self.raise_()
            self._hide_from_taskbar()
        else:
            # 隐藏时复位可能残留的拖动状态
            self._dragging = False
            self._drag_off = None
            self.releaseMouse()
            self.hide()

    # ------------------------------------------------------------------ #
    # 拖动（事件过滤器实现，覆盖面板自身与全部子控件）
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            # 排除锁定按钮（它自身处理点击切换锁定）；锁定态也不启动拖动
            if (event.button() == Qt.LeftButton and not self._locked
                    and obj is not self._lock_btn):
                self._drag_off = (event.globalPosition().toPoint()
                                  - self.frameGeometry().topLeft())
                self._dragging = True
                self.grabMouse()      # 抓取后移动事件统一派发给过滤器，拖动顺滑
                return True
        elif et == QEvent.Type.MouseMove:
            if (self._dragging and (event.buttons() & Qt.LeftButton)):
                self.move(event.globalPosition().toPoint() - self._drag_off)
                return True
        elif et == QEvent.Type.MouseButtonRelease:
            if self._dragging and event.button() == Qt.LeftButton:
                self._dragging = False
                self._drag_off = None
                self.releaseMouse()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ #
    # 背景绘制：整体半透明深色圆角
    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(_BORDER, 1))
        p.setBrush(_BG)
        p.drawRoundedRect(QRect(self.rect()).adjusted(0, 0, -1, -1),
                          RADIUS, RADIUS)
        p.end()

    def shutdown(self):
        """退出前停表，回收资源（由主窗口在退出时调用）。"""
        self._timer.stop()
        self.hide()
