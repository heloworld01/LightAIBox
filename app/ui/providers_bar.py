"""透明悬浮 Provider 状态面板：垂直列出「已启动（运行中）」的 provider 及其用量/余量。

行为与视觉约定：
- 无边框 + 置顶 + 工具窗（不占用系统任务栏 / Alt+Tab），首次打开定位于主窗口左上角侧。
- 面板为独立顶层窗口：展示/隐藏只受主窗口上的开关控制，不因主窗口隐藏而消失。
- 背景固定为半透明深色圆角（**不跟随应用明暗主题**，视觉稳定）。
- 顶部标题条：左上角「用量统计」名称，右上角文字按钮「锁定 / 解锁」+「关闭」。
- 每个运行中的 provider 占一个色块行：上行「状态圆点 + 名称」左对齐，
  中行「模型」与下行「用量/余量」均靠右展示，用量行与色块底边保留空隙。
- 周期性轮询网关缓存，仅当「运行中集合或用量」变化时才重建 UI；文案随主界面
  中英文切换实时刷新。
"""
import ctypes
import typing

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
)
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import config
from .i18n import LanguageManager

if typing.TYPE_CHECKING:
    from ..gateway import Gateway
    from ..models import Provider

# 固定配色（不随明暗主题变化）-------------------------------------------- #
# 面板底：近黑半透明。锁定后整窗设 WS_EX_LAYERED+WS_EX_TRANSPARENT 点击穿透，
# 背景透明度会显得更低（视觉上与下层内容叠加），故解锁态再压低一档，保证
# 两种状态下可读性一致。
_BG_UNLOCK = QColor(16, 17, 22, 135)   # 解锁态背景（可拖动时）
_BG_LOCK = QColor(16, 17, 22, 175)     # 锁定态背景（点击穿透时）
_BORDER = QColor(255, 255, 255, 38)    # 面板描边：半透明白
_ROW = QColor(255, 255, 255, 26)       # 单行底色（略高于面板底，提升对比）
_TEXT = QColor(245, 246, 250)          # 名称文字
_SUB = QColor(178, 184, 200)           # 模型 / 用量等次要文字（略高于底色提升可读性）
_DOT_ON = QColor(61, 220, 132)         # 运行中圆点：绿色
_ON = QColor(61, 220, 132)             # 锁定态强调绿

# 几何常量 ---------------------------------------------------------------- #
PANEL_W = 320      # 面板固定宽度
CELL_H = 70        # 单行高度（三行文本：名称 / 模型 / 用量余量 + 底部空隙）
QUOTA_GAP = 9      # 用量行与色块底边的空隙
MODEL_BASELINE = CELL_H - QUOTA_GAP - 16   # 中行基线（紧贴用量行上方）
QUOTA_BASELINE = CELL_H - QUOTA_GAP        # 下行（用量）基线
PAD_X = 14         # 面板左右内边距
PAD_Y = 10         # 面板上下内边距
ROW_GAP = 6        # 相邻行间距
RADIUS = 16        # 面板圆角
GAP = 12           # 距可用区域底部的距离（避开系统任务栏）
REFRESH_MS = 1200  # 轮询刷新间隔
HEADER_H = 30      # 顶部标题条高度
BTN_W = 44         # 右上角文字按钮宽
BTN_H = 24         # 右上角文字按钮高
CTRL_INSET = 6     # 锁定/关闭按钮小窗距面板右上角的缩进


def _fmt(n: int) -> str:
    """把大数字简写：1234 -> 1.2k，1200000 -> 1.2M。"""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n / 1e3:.1f}k".rstrip("0").rstrip(".")
    return str(n)


def _hide_window_from_taskbar(widget) -> bool:
    """给顶层窗口加原生 WS_EX_TOOLWINDOW 样式，从任务栏/Alt+Tab 隐藏。

    不通过 Qt.Tool 实现（那会让窗口失去激活、点不到），而是保持普通窗口
    的可点击性，仅借 Windows 扩展样式隐藏任务栏入口。返回是否已设置。
    """
    try:
        hwnd = int(widget.winId())
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if not style & WS_EX_TOOLWINDOW:
            ctypes.windll.user32.SetWindowLongPtrW(
                hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
        return True
    except Exception:
        return False  # 非 Windows / 权限受限时静默跳过


class _ProviderCell(QWidget):
    """一个 provider 的竖排整行色块。

    上行：状态圆点 + 名称（粗）
    中行：模型（次要色，靠右展示）
    下行：用量 / 余量 —— 靠右展示，与色块底边留空隙
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

    def retranslate(self) -> None:
        """语言切换：仅刷新 tooltip 并重绘（文本在 paintEvent 里实时取词）。"""
        self.setToolTip(self._tooltip())
        self.update()

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
        w = self.width()

        # 上行：圆点 + 名称（名称过长时省略号截断，不再与模型抢同一行空间）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_DOT_ON)
        p.drawEllipse(QPoint(16, 21), 4, 4)
        p.setFont(self._name_font())
        p.setPen(_TEXT)
        name = fm1.elidedText(name, Qt.TextElideMode.ElideRight,
                              self.width() - 28 - 10)
        p.drawText(28, 26, name)

        # 中行：模型（次要色，靠右展示，过长省略号截断到剩余空间内）
        p.setFont(self._sub_font())
        fm2 = QFontMetrics(self._sub_font())
        p.setPen(_SUB)
        right_pad = 14   # 与用量行共用同一右边距，两行右缘对齐
        model_w = w - right_pad - 28   # 最左不超过名称行的缩进位
        model = fm2.elidedText(
            model, Qt.TextElideMode.ElideRight, model_w)
        p.drawText(w - right_pad - fm2.horizontalAdvance(model),
                   MODEL_BASELINE, model)

        # 下行：用量 / 余量 —— 靠右展示，底边与色块之间保留 QUOTA_GAP 空隙
        quota = self._quota_text()
        p.setPen(_SUB)
        p.drawText(w - 14 - fm2.horizontalAdvance(quota),
                   QUOTA_BASELINE, quota)
        p.end()


class _TextButton(QWidget):
    """面板右上角的文字按钮：自绘以完全掌控配色（不依赖 QSS、不随明暗主题）。

    悬停加深底色；按下发出 clicked()。
    """

    clicked = Signal()

    def __init__(self, text_getter, width: int = BTN_W,
                 color_getter=None, parent=None):
        super().__init__(parent)
        self._text_getter = text_getter   # 每次绘制调用，语言切换即时生效
        # 配色同样走回调：状态变化后 update() 即自动换色，无需手动同步颜色
        self._color_getter = color_getter or (lambda: _SUB)
        self._hover = False
        self.setFixedSize(width, BTN_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setTextGetter(self, getter) -> None:
        self._text_getter = getter
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        # 按下即消费事件：父面板的事件过滤器只在「非按钮」控件上启动拖动，
        # 若让按下冒传到面板，会误触发拖动并吞掉后续抬起事件（点击失效）。
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fg = self._color_getter()
        bg = QColor(fg.red(), fg.green(), fg.blue(),
                    52 if self._hover else 24)
        p.setPen(QPen(QColor(255, 255, 255, 14), 1))
        p.setBrush(bg)
        rect = QRect(self.rect()).adjusted(0, 0, -1, -1)
        p.drawRoundedRect(rect, 8, 8)
        f = QFont()
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(fg)
        p.drawText(rect.translated(0, 1), Qt.AlignmentFlag.AlignCenter,
                   self._text_getter())
        p.end()


class _BarController(QWidget):
    """右上角「锁定 / 关闭」按钮的独立小窗。

    锁定后主面板整窗设置 WS_EX_TRANSPARENT 点击穿透，下层内容可直接点按；
    Windows 的穿透是按窗口生效的，无法只对窗口内某一块区域豁免，故把
    两个按钮挪到这个独立小窗：它不设穿透、始终跟随主面板右上角，锁定
    状态下仍可点击（这也是锁定后唯一的操作入口）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint
                            | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._taskbar_hidden = False

    def add_widget(self, w) -> None:
        self._layout.addWidget(w)

    def paintEvent(self, event):
        # 纯容器：不画背景，只承载按钮
        pass

    def closeEvent(self, event: QCloseEvent):
        event.ignore()      # 不响应系统关闭，统一由 set_bar_visible 管理
        self.hide()


class ProvidersBar(QWidget):
    """透明悬浮面板：顶部标题条（用量统计 + 锁定/关闭）+ 运行中 provider 列表。

    解锁态：面板可拖动、可交互；锁定态：整窗点击穿透（下层内容可正常点按），
    仅右上角独立小窗里的「锁定 / 关闭」按钮仍可点击。
    """

    # 面板自行关闭时发出（供主窗口同步开关按钮状态）
    visibility_changed = Signal(bool)

    def __init__(self, gateway: "Gateway", parent=None):
        super().__init__(parent)
        self._gateway = gateway
        self._cells = []   # List[_ProviderCell]
        # 重建签名：运行中 provider 的 (id, 用量, 余量相关字段) —— ID 集合不变时
        # 若用量/配额变化也要重建，保证「用量/余量」实时准确
        self._sig = ()
        self._locked = False    # 锁定后整窗点击穿透（默认解锁）
        self._drag_off = None   # 拖动偏移（None 表示未在拖动）
        self._dragging = False
        self._user_moved = False  # 用户拖过面板后不再自动定位

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

        tr = LanguageManager().tr
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
        self._layout.setSpacing(ROW_GAP)

        # 顶部标题条：左上角面板名；右上角「锁定 / 关闭」按钮放独立小窗
        # （见 _BarController：锁定穿透后按钮仍需可点）。
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.setSpacing(6)
        self._title = QLabel(tr("用量统计", "Usage"))
        f = QFont()
        f.setPixelSize(13)
        f.setBold(True)
        self._title.setFont(f)
        self._title.setStyleSheet("color: #F5F6FA; background: transparent;")
        header.addWidget(self._title)
        header.addStretch(1)
        self._layout.addLayout(header)
        self._header_layout = header   # 供 _track_controller 计算右上角区域

        self._controller = _BarController(self)
        # 事件过滤器只装在按钮上（不装子控件）：既能拦截点击阻止面板拖动，
        # 又不会影响按钮自身的按下/抬起处理。
        self._lock_btn = _TextButton(self._lock_text,
                                     width=56, color_getter=self._lock_color)
        self._lock_btn.installEventFilter(self)
        self._lock_btn.clicked.connect(self._on_lock_clicked)
        self._close_btn = _TextButton(
            lambda: LanguageManager().tr("关闭", "Close"), width=44)
        self._close_btn.installEventFilter(self)
        self._close_btn.clicked.connect(lambda: self.set_bar_visible(False))
        self._controller.add_widget(self._lock_btn)
        self._controller.add_widget(self._close_btn)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(ROW_GAP)
        self._layout.addLayout(self._rows_layout)

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
    # 文案（跟随主界面中英文切换）
    # ------------------------------------------------------------------ #
    def _lock_text(self) -> str:
        return (LanguageManager().tr("锁定", "Lock") if not self._locked
                else LanguageManager().tr("已锁定", "Locked"))

    def _lock_color(self) -> QColor:
        """未锁定 = 中性灰；锁定后 = 绿色高亮（文字 + 底色整体变色）。"""
        return _ON if self._locked else _SUB

    def retranslate(self) -> None:
        """主界面切换语言时调用：刷新面板内全部文案。"""
        tr = LanguageManager().tr
        self._title.setText(tr("用量统计", "Usage"))
        self._lock_btn.setTextGetter(self._lock_text)   # 触发重绘
        self._lock_btn.setToolTip(
            tr("点击解锁后可拖动", "Click to unlock & drag") if self._locked
            else tr("点击锁定后不可拖动", "Click to lock"))
        # 关闭按钮文字是 lambda（每次绘制取词），retranslate 里须手动 update
        # 触发重绘，否则语言切换后文字不会刷新。
        self._close_btn.update()
        self._close_btn.setToolTip(tr("关闭悬浮面板", "Close panel"))
        for c in self._cells:
            c.retranslate()

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
        # 仅重建行区（顶部标题条不动，保留锁定状态）
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cells = [_ProviderCell(p, self) for p in running]
        for c in self._cells:
            self._rows_layout.addWidget(c)
        # 面板高度 = 上下内边距 + Σ行高 + Σ行距 + 顶部标题条
        self.setFixedHeight(PAD_Y * 2 + len(self._cells) * CELL_H
                            + len(self._cells) * ROW_GAP + HEADER_H)
        self._place_controller()   # 面板几何变了，按钮小窗重新对齐

    def _on_lock_clicked(self) -> None:
        """锁定 / 解锁：锁定后整窗点击穿透，仅右上角按钮小窗仍可点。"""
        self._locked = not self._locked
        self._apply_click_through()
        self._place_controller()
        # 只刷新锁定按钮文案/配色与 tooltip，避免整面板 retranslate
        # （那会让每个 cell 都 setToolTip+update，纯浪费）
        tr = LanguageManager().tr
        self._lock_btn.update()
        self._lock_btn.setToolTip(
            tr("点击解锁后可拖动", "Click to unlock & drag") if self._locked
            else tr("点击锁定后不可拖动", "Click to lock"))

    # ------------------------------------------------------------------ #
    # 锁定穿透（Windows 原生扩展样式）
    # ------------------------------------------------------------------ #
    def _apply_click_through(self) -> None:
        """锁定 → 给主面板加 WS_EX_TRANSPARENT：所有鼠标事件穿透到下层窗口；
        解锁 → 摘掉 WS_EX_TRANSPARENT 恢复可点击/可拖动。

        按钮小窗（_BarController）不受影响，锁定后仍可点击，是唯一的操作入口。

        **只许动 WS_EX_TRANSPARENT，绝不碰 WS_EX_LAYERED**：Qt 对
        WA_TranslucentBackground 窗口自管 WS_EX_LAYERED（backing store 经
        UpdateLayeredWindow 提交实现逐像素透明）。若在此把它清掉，Qt 的
        flush 会退化为普通 BitBlt——背景变全不透明、圆角和描边全部丢失，
        且不会自愈。穿透只需 TRANSPARENT 位（窗口本就分层）。
        """
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if self._locked:
                ctypes.windll.user32.SetWindowLongPtrW(
                    hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT)
            else:
                ctypes.windll.user32.SetWindowLongPtrW(
                    hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)
        except Exception:
            pass  # 非 Windows / 权限受限时静默跳过（退化为仅禁止拖动）

    # ------------------------------------------------------------------ #
    # 定位 / 拖动
    # ------------------------------------------------------------------ #
    def _reposition(self):
        """首次定位：主窗口左上角侧（保持 GAP 边距，避开屏幕边缘/任务栏）。

        屏幕变化（接显示器等）只对未拖过的面板重新定位；用户拖过则保持
        现有位置，不因插拔显示器被拉回。
        """
        if self._user_moved:
            self._place_controller()
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()          # 已扣除系统任务栏
        w = self.width() or PANEL_W
        h = self.height() or self.sizeHint().height()
        x = geo.x() + GAP
        y = geo.y() + GAP
        self.setGeometry(x, y, w, h)
        self._place_controller()

    def _place_controller(self):
        """把按钮小窗对齐到主面板右上角内侧（跟随移动 / 尺寸变化）。

        往左、往下各缩进 CTRL_INSET，避免按钮紧贴面板边缘/圆角。
        """
        g = self.frameGeometry()
        self._controller.resize(self._controller.sizeHint())
        self._controller.move(g.right() - self._controller.width()
                              - CTRL_INSET + 1,
                              g.top() + CTRL_INSET - 1)

    def _hide_from_taskbar(self) -> None:
        """给窗口加原生 WS_EX_TOOLWINDOW 样式，从系统任务栏/Alt+Tab 隐藏。"""
        if _hide_window_from_taskbar(self):
            self._taskbar_hidden = True

    def set_bar_visible(self, show: bool) -> None:
        """外部开关：显示/隐藏悬浮面板（含右上角按钮小窗）。"""
        if show:
            if self._needs_initial_place:
                # 仅首次显示自动定位（主窗口左上角侧）；之后恢复只保持现有位置
                self._reposition()
                self._needs_initial_place = False
            self.show()
            self.raise_()
            self._hide_from_taskbar()
            self._controller.show()
            self._controller.raise_()
            if _hide_window_from_taskbar(self._controller):
                self._controller._taskbar_hidden = True
            self._place_controller()
        else:
            # 隐藏时复位可能残留的拖动状态
            self._dragging = False
            self._drag_off = None
            self.releaseMouse()
            self._controller.hide()
            self.hide()
            # 面板自行关闭后通知外部（主窗口据此同步开关按钮状态）
            self.visibility_changed.emit(False)

    # ------------------------------------------------------------------ #
    # 拖动（事件过滤器实现，覆盖面板自身与全部子控件）
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):
        et = event.type()
        # 标题条按钮上装的是本过滤器：所有事件一律放行给按钮自身处理
        # （按下/抬起完成点击，进入/离开完成悬停高亮）。若在此消费按下，
        # 按钮收不到后续抬起事件，点击将失效；若启动拖动，鼠标移入按钮区域
        # 就会误触发整面板拖动。
        if obj is self._lock_btn or obj is self._close_btn:
            return super().eventFilter(obj, event)
        if et == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.LeftButton and not self._locked:
                self._drag_off = (event.globalPosition().toPoint()
                                  - self.frameGeometry().topLeft())
                self._dragging = True
                self.grabMouse()      # 抓取后移动事件统一派发给过滤器，拖动顺滑
                return True
        elif et == QEvent.Type.MouseMove:
            if (self._dragging and (event.buttons() & Qt.LeftButton)):
                self.move(event.globalPosition().toPoint() - self._drag_off)
                self._user_moved = True    # 拖过后不再自动定位
                self._place_controller()   # 按钮小窗跟随拖动
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
        # 锁定态背景稍深：整窗点击穿透后下层内容直接透出，加深一档保可读
        p.setBrush(_BG_LOCK if self._locked else _BG_UNLOCK)
        p.drawRoundedRect(QRect(self.rect()).adjusted(0, 0, -1, -1),
                          RADIUS, RADIUS)
        p.end()

    def shutdown(self):
        """退出前停表，回收资源（由主窗口在退出时调用）。"""
        self._timer.stop()
        self._controller.hide()
        self.hide()
