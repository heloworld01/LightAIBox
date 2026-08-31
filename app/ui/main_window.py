"""主窗口：QTabWidget 承载各功能页，Tab 栏右上角提供主题切换按钮。

支持「关闭 → 隐藏到系统托盘」：关闭按钮/Alt+F4 将窗口隐藏到托盘，
后台保持运行（统一 API 服务继续可用）；托盘左键点击切换显示/隐藏，
托盘右键菜单提供「显示窗口 / 退出」。
"""
import os

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitterHandle,
    QSystemTrayIcon,
    QTabBar,
    QTabWidget,
    QWidget,
)

from .. import config
from ..gateway import Gateway
from ..server import GatewayServer
from .api_page import ApiPage
from .gateway_page import GatewayPage
from .i18n import LanguageManager
from .theme_manager import ThemeManager


def _make_tray_icon() -> QIcon:
    """返回项目 logo 图标（圆角版），供托盘 / 窗口复用。

    优先加载资源目录 app/resources/logo.png（源码与打包态都经 config.
    RESOURCES_DIR 解析，spec 已把该目录随包分发）；缺失或损坏时回退到
    程序化绘制的占位图标，保证任何环境都有托盘图标可用。
    """
    logo_path = os.path.join(config.RESOURCES_DIR, "logo.png")
    if os.path.exists(logo_path):
        icon = QIcon(logo_path)
        if not icon.isNull():
            return icon
    # 回退：程序化绘制（不依赖外部文件）
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    body = QRectF(6, 10, size - 12, size - 20)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#2B6DE0"))
    p.drawRoundedRect(body, 12, 12)
    p.setBrush(QColor(255, 255, 255, 60))
    p.drawRoundedRect(QRectF(body.left() + 4, body.top() + 4,
                             body.width() - 8, body.height() * 0.18), 6, 6)
    p.setBrush(QColor("white"))
    font = QFont()
    font.setPixelSize(int(size * 0.55))
    font.setBold(True)
    p.setFont(font)
    p.drawText(body, Qt.AlignmentFlag.AlignCenter, "L")
    p.end()
    return QIcon(pix)


class WindowControlButton(QPushButton):
    """无边框窗口右上角控制按钮。

    自绘「最小化 / 最大化 / 还原 / 关闭」图标，保证各图标（尤其是最大化的
    「方框」与还原的「重叠方框」）拥有统一的外接尺寸与线宽，视觉大小一致；
    图标颜色取 palette 前景色，随 QSS 状态（normal / hover）自动变化。
    """

    KIND_MIN = "min"
    KIND_MAX = "max"
    KIND_CLOSE = "close"

    ICON_SIZE = 10.0        # 图标统一外接尺寸（px）
    LINE_WIDTH = 1.2        # 描边线宽（px）
    _RESTORE_OFFSET = 0.4   # 还原图标「后方框」相对「前方框」的偏移比例

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._maximized = False
        self.setProperty("class", "ghost")
        self.setFixedSize(30, 30)
        self.setFocusPolicy(Qt.NoFocus)

    def set_maximized(self, maximized: bool) -> None:
        """切换最大化图标为重叠方框（还原态），并触发重绘。"""
        if self._maximized != maximized:
            self._maximized = maximized
            self.update()

    def paintEvent(self, event):
        # 先让 QSS 绘制 ghost 背景/边框，再叠加自绘图标
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().color(QPalette.ColorRole.ButtonText),
                   self.LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        c = self.rect().center()
        s = self.ICON_SIZE
        r = s / 2.0
        if self._kind == self.KIND_MIN:
            p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
        elif self._kind == self.KIND_CLOSE:
            d = s * 0.45
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
        elif self._kind == self.KIND_MAX:
            if self._maximized:
                self._draw_restore(p, c, s)
            else:
                p.drawRect(QRectF(c.x() - r, c.y() - r, s, s))
        p.end()

    def _draw_restore(self, p: QPainter, c, s: float):
        """还原图标：前方框（左下）+ 后方框（右上），外接尺寸与单方框一致。"""
        off = s * self._RESTORE_OFFSET
        b = s - off
        r = s / 2.0
        p.drawRect(QRectF(c.x() - r, c.y() - r + off, b, b))      # 前方框
        p.drawRect(QRectF(c.x() - r + off, c.y() - r, b, b))      # 后方框


class MainWindow(QMainWindow):
    def __init__(self, gateway: Gateway, server: GatewayServer):
        super().__init__()
        self._server = server  # 用于托盘「退出」时优雅停止后台 API 服务
        self.resize(1100, 720)
        self.setMinimumSize(900, 600)
        # 无边框窗口：去除原生标题栏 + 透明背景以支持圆角。
        # 关键：额外保留 MinMaxButtonsHint，让 Windows 给窗口补上
        # WS_MINIMIZEBOX / WS_MAXIMIZEBOX 样式位；否则纯 FramelessWindowHint 窗口
        # 缺少非客户区最小化/还原标记，点击任务栏图标无法最小化 / 还原。
        self.setWindowFlags(Qt.FramelessWindowHint
                            | Qt.Window
                            | Qt.WindowSystemMenuHint
                            | Qt.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 窗口图标（任务栏 / Alt+Tab / 托盘共用圆角 logo）
        self.setWindowIcon(_make_tray_icon())

        # 应用持久化的主题（暗/亮/跟随系统）
        self.theme = ThemeManager()
        self.theme.apply_theme(self.theme.current())

        # 语言管理器（中英文切换）
        self.lang = LanguageManager()

        # 系统托盘：关闭 → 隐藏到托盘后台运行，托盘提供显示/退出。
        # 仅当系统支持托盘（Windows 正常具备）时启用；本机可用即接入。
        self._tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._setup_tray()

        # 圆角背景载体：透明窗口内需一层实色圆角容器
        self._wrap = QWidget()
        self._wrap.setObjectName("centralWrap")
        wrap_layout = QHBoxLayout(self._wrap)
        # 四周留白，让内容整体内缩，配合 centralWrap 圆角形成四角（含底部）圆角窗口
        wrap_layout.setContentsMargins(8, 8, 8, 8)
        wrap_layout.setSpacing(0)

        self.tabs = QTabWidget()
        wrap_layout.addWidget(self.tabs)
        self.setCentralWidget(self._wrap)

        self.gateway_page = GatewayPage(gateway)
        self.tabs.addTab(self.gateway_page, self.lang.tr("AI 网关", "AI Gateway"))

        self.api_page = ApiPage(gateway, server)
        self.tabs.addTab(self.api_page, self.lang.tr("统一 API", "Unified API"))

        self._build_controls()
        self._retranslate()

        # 无边框窗口需自实现拖动：按住任意非交互空白处可拖动整窗
        self._drag_offset: QPoint | None = None
        self._dragging = False
        self._resizing_cursor = False
        # 记录窗口当前所在屏的 DPR，用于仅在实际跨到不同缩放屏幕时重置拖动。
        self._last_dpr = self._current_dpr()
        # 边缘悬停需子控件开启鼠标跟踪，才能收到无按键的 MouseMove 事件
        for _w in [self._wrap] + self._wrap.findChildren(QWidget):
            _w.setMouseTracking(True)
        QApplication.instance().installEventFilter(self)

    def _current_dpr(self) -> float:
        """返回窗口当前所在屏幕的设备像素比（无原生句柄时回退 1.0）。"""
        wh = self.windowHandle()
        if wh is None or wh.screen() is None:
            return 1.0
        return wh.screen().devicePixelRatio()

    def _on_screen_changed(self):
        """仅在窗口实际跨到「不同缩放比例」的显示器时，重置拖动偏移。

        QEvent.ScreenChangeInternal 在窗口每次 move() 时都可能触发，若不加
        区分地清空偏移，会导致同一屏内拖动时窗口「跳一下」甚至被反复重算
        尺寸而变大。这里用目标屏 DPR 是否变化来过滤：DPR 未变则不动作。
        """
        new_dpr = self._current_dpr()
        if abs(new_dpr - self._last_dpr) < 1e-6:
            return
        self._last_dpr = new_dpr
        # 跨屏瞬间旧偏移基于旧屏坐标基准，已失效，直接丢弃；下次按下重新计算。
        self._dragging = False
        self._drag_offset = None

    # ------------------------------------------------------------------ #
    # 系统托盘：关闭隐藏到托盘、托盘菜单、真正退出
    # ------------------------------------------------------------------ #
    def _setup_tray(self) -> None:
        """创建托盘图标与右键菜单；左键单击切换窗口显示/隐藏。

        菜单结构：
            LightAIBox（应用名，禁用态，仅展示）
            ─────────────
            显示窗口
            ─────────────
            退出
        """
        tray = QSystemTrayIcon(_make_tray_icon(), self)
        tray.setToolTip("LightAIBox")

        # 右键菜单：标题（应用名，中文/英文跟随语言，禁用态仅作展示）
        menu = QMenu()
        self._tray_title_action = menu.addAction(
            self.lang.tr("LightAIBox · 轻量化 AI 工具箱",
                         "LightAIBox · Lightweight AI Toolbox"))
        self._tray_title_action.setEnabled(False)  # 纯展示，不可点击
        menu.addSeparator()
        self._tray_show_action = menu.addAction(
            self.lang.tr("显示窗口", "Show window"))
        self._tray_show_action.triggered.connect(self._tray_show_window)
        menu.addSeparator()
        self._tray_quit_action = menu.addAction(self.lang.tr("退出", "Quit"))
        self._tray_quit_action.triggered.connect(self._try_quit)
        tray.setContextMenu(menu)

        # 左键单击：显示/隐藏切换（右键交给系统弹出上下文菜单）
        tray.activated.connect(self._on_tray_activated)

        tray.show()
        self._tray = tray

        # 首次隐藏到托盘时给一条提示气泡，引导用户如何找回窗口
        self._tray_hint_shown = False

    def _on_tray_activated(self, reason) -> None:
        """托盘图标被点击：左键（单击）切换显示/隐藏，其余交给系统菜单。"""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self._tray_show_window()

    def _tray_show_window(self) -> None:
        """从托盘恢复到前台：显示窗口（必要时还原）并激活。"""
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _try_quit(self) -> None:
        """从托盘菜单真正退出：停止服务线程后结束进程。"""
        if self._server is not None:
            self._server.stop()
        QApplication.instance().quit()

    def closeEvent(self, event):
        """关闭按钮 / Alt+F4：弹窗让用户选择「隐藏到托盘」或「退出程序」。

        选隐藏 → 窗口缩到托盘，统一 API 服务继续在后台运行；选退出 → 真正
        结束进程（停止服务线程）。托盘不可用（极少数环境）时退化为直接退出。
        """
        if self._tray is None:
            super().closeEvent(event)
            return

        event.ignore()
        box = QMessageBox(self)
        box.setWindowTitle(self.lang.tr("退出 LightAIBox", "Exit LightAIBox"))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(self.lang.tr("关闭后要怎样处理？", "What to do after closing?"))
        box.setInformativeText(self.lang.tr(
            "隐藏到托盘：程序继续在后台运行，统一 API 服务保持可用；\n"
            "退出程序：结束进程并停止后台服务。",
            "Hide to tray: keep running in the background with the unified API "
            "still available;\nQuit: terminate the process and stop the service."))
        # 按钮顺序：隐藏到托盘（默认）在前，退出程序在后
        hide_btn = box.addButton(
            self.lang.tr("隐藏到托盘", "Hide to tray"),
            QMessageBox.ButtonRole.AcceptRole)
        quit_btn = box.addButton(
            self.lang.tr("退出程序", "Quit"),
            QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(hide_btn)
        box.exec()
        if box.clickedButton() is hide_btn:
            self.hide()
            if not getattr(self, "_tray_hint_shown", False):
                self._tray_hint_shown = True
                self._tray.showMessage(
                    "LightAIBox",
                    self.lang.tr(
                        "已最小化到系统托盘，单击托盘图标或右击托盘图标可恢复窗口。",
                        "Minimized to system tray. Click the tray icon to restore."),
                    QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            self._try_quit()

    # ------------------------------------------------------------------ #
    # 文本刷新（Click 时统一语言化）
    # ------------------------------------------------------------------ #
    def changeEvent(self, event):
        """窗口状态（最大化/还原/最小化）变化时同步最大化按钮文字。"""
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_max_btn()
        super().changeEvent(event)

    def _retranslate(self):
        """窗口标题、Tab 标题、角控件、各页面文案统一刷新。"""
        self.setWindowTitle(self.lang.tr("LightAIBox · 轻量化 AI 工具箱",
                                         "LightAIBox · Lightweight AI Toolbox"))
        self.tabs.setTabText(0, self.lang.tr("AI 网关", "AI Gateway"))
        self.tabs.setTabText(1, self.lang.tr("统一 API", "Unified API"))
        self._sync_theme_controls()
        self.gateway_page.retranslate()
        self.api_page.retranslate()
        # 托盘菜单文案跟随语言切换（含置顶的应用名标题）
        if self._tray is not None:
            self._tray_title_action.setText(
                self.lang.tr("LightAIBox · 轻量化 AI 工具箱",
                             "LightAIBox · Lightweight AI Toolbox"))
            self._tray_show_action.setText(
                self.lang.tr("显示窗口", "Show window"))
            self._tray_quit_action.setText(self.lang.tr("退出", "Quit"))

    # ------------------------------------------------------------------ #
    # 角控件（主题切换 + 语言切换 + 关闭，置于 Tab 栏右上角，与标签同行）
    # ------------------------------------------------------------------ #
    def _build_controls(self):
        """在 Tab 栏右上角放主题/语言切换按钮，不额外占用一行。"""
        corner = QWidget()
        self._corner = corner
        layout = QHBoxLayout(corner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)

        self.theme_label = QLabel()
        self.theme_label.setProperty("class", "stats-text")
        layout.addWidget(self.theme_label)

        self.theme_btn = QPushButton()
        self.theme_btn.setProperty("class", "ghost")
        self.theme_btn.setToolTip("切换暗色 / 亮色主题")
        self.theme_btn.clicked.connect(self._on_toggle_theme)
        layout.addWidget(self.theme_btn)

        self.lang_btn = QPushButton()
        self.lang_btn.setProperty("class", "ghost")
        self.lang_btn.setToolTip("Switch language / 切换语言")
        self.lang_btn.clicked.connect(self._on_toggle_language)
        layout.addWidget(self.lang_btn)

        # 无边框窗口没有原生最小化/最大化/关闭按钮，这里补一组（自绘图标，
        # 保证「方框 / 重叠方框」等图标视觉尺寸一致）
        self.min_btn = WindowControlButton(WindowControlButton.KIND_MIN)
        self.min_btn.setToolTip("最小化")
        self.min_btn.clicked.connect(self.showMinimized)
        layout.addWidget(self.min_btn)

        self.max_btn = WindowControlButton(WindowControlButton.KIND_MAX)
        self.max_btn.setToolTip("最大化")
        self.max_btn.clicked.connect(self._on_toggle_maximize)
        layout.addWidget(self.max_btn)

        self.close_btn = WindowControlButton(WindowControlButton.KIND_CLOSE)
        self.close_btn.setToolTip("关闭窗口")
        self.close_btn.clicked.connect(self.close)
        layout.addWidget(self.close_btn)

        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

    def _sync_theme_controls(self):
        """让按钮与文字反映当前主题。"""
        dark = self.theme.current() == ThemeManager.DARK
        self.theme_btn.setText(self.lang.tr("☀ 亮色", "☀ Light") if dark
                               else self.lang.tr("🌙 暗色", "🌙 Dark"))
        self.theme_label.setText(self.lang.tr("暗色", "Dark") if dark
                                 else self.lang.tr("亮色", "Light"))
        # 语言切换按钮显示「目标语言」，提示点击可切换
        self.lang_btn.setText("EN / 中文" if self.lang.is_zh() else "中文 / EN")
        self.theme_btn.setToolTip(self.lang.tr("切换暗色 / 亮色主题",
                                               "Toggle dark / light theme"))
        self.min_btn.setToolTip(self.lang.tr("最小化", "Minimize"))
        self._sync_max_btn()
        self.close_btn.setToolTip(self.lang.tr("关闭窗口", "Close window"))

    def _sync_max_btn(self):
        """让最大化按钮图标 / 提示反映当前窗口状态（最大化 → 显示还原）。"""
        maximized = self.isMaximized()
        self.max_btn.set_maximized(maximized)
        self.max_btn.setToolTip(self.lang.tr("向下还原", "Restore Down")
                                if maximized else self.lang.tr("最大化", "Maximize"))

    def _on_toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_max_btn()

    def _on_toggle_theme(self):
        self.theme.toggle()
        self.gateway_page.retranslate()
        self.api_page.retranslate()
        self._sync_theme_controls()

    def _on_toggle_language(self):
        self.lang.toggle()
        self._retranslate()

    # ------------------------------------------------------------------ #
    # 无边框窗口拖动：按住任意「非交互」空白处（Tab 栏空白、页面容器背景等）
    # 拖动整窗。按钮/输入框/表格等交互控件保持各自行为。
    # ------------------------------------------------------------------ #
    # 交互控件黑名单：命中则不拖动（一切 QWidget 都是 QWidget 子类，
    # 用「排除交互控件」而非「白名单容器」才能准确区分）
    _NO_DRAG_TYPES = (
        QAbstractButton,        # 按钮 / 勾选框 / 单选框
        QAbstractItemView,      # 表格 / 树 / 列表
        QComboBox,              # 下拉组合框（其弹层视图是独立窗口，不会被沿父链命中）
        QAbstractSpinBox,       # 数字输入框
        QHeaderView,            # 表格表头（点击排序）
        QSplitterHandle,        # 分栏拖动条
        QAbstractScrollArea,    # 滚动区 / 文本区
        QLineEdit,
        QAbstractSlider,        # 滑块
    )

    def _is_drag_target(self, global_pos: QPoint) -> bool:
        app = QApplication.instance()
        w = app.widgetAt(global_pos)
        if w is None or w.window() is not self:
            return False
        # 关键修复：点击表格时 widgetAt 返回的是 viewport（普通 QWidget），
        # 而非 QTableWidget 本身，isinstance 校验会漏判，导致点击表格行被误判为
        # 「拖动窗口」、MouseButtonPress 被吞掉，行无法选中。这里沿父链向上查找
        # 交互控件（表格/按钮/输入框等），命中即视为交互、不拖动。
        node = w
        while node is not None and node is not self:
            if isinstance(node, self._NO_DRAG_TYPES):
                return False
            if isinstance(node, QTabBar):
                # 标签栏：按在具体标签上切换 Tab（不拖动），空白处可拖动
                return node.tabAt(node.mapFromGlobal(global_pos)) == -1
            node = node.parentWidget()
        return True

    _RESIZE_MARGIN = 6  # 四边/四角缩放命中范围（px）

    def _resize_edges(self, pos: QPoint):
        """命中窗口边缘/角落时返回对应 Qt.Edges 供系统原生缩放，否则 None。"""
        g = self.frameGeometry()
        left = pos.x() - g.left() <= self._RESIZE_MARGIN
        right = g.right() - pos.x() <= self._RESIZE_MARGIN
        top = pos.y() - g.top() <= self._RESIZE_MARGIN
        bottom = g.bottom() - pos.y() <= self._RESIZE_MARGIN
        if not (left or right or top or bottom):
            return None
        edges = Qt.Edges()
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _resize_cursor(self, edges):
        """Qt.Edges -> 对应方向缩放光标；空/None 返回 None。"""
        if not edges:
            return None
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if (top and left) or (bottom and right):
            return Qt.CursorShape.SizeFDiagCursor
        if (top and right) or (bottom and left):
            return Qt.CursorShape.SizeBDiagCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.SizeHorCursor

    def _update_resize_cursor(self, pos: QPoint):
        """悬停于窗口边缘/角落时显示缩放光标，否则恢复默认。"""
        app = QApplication.instance()
        w = app.widgetAt(pos)
        in_self = w is not None and w.window() is self
        edges = self._resize_edges(pos) if (in_self and not self.isFullScreen()) else None
        cur = self._resize_cursor(edges)
        if cur is not None:
            if self._resizing_cursor:
                app.changeOverrideCursor(cur)
            else:
                app.setOverrideCursor(cur)
                self._resizing_cursor = True
        elif self._resizing_cursor:
            app.restoreOverrideCursor()
            self._resizing_cursor = False

    def _is_titlebar_area(self, pos: QPoint) -> bool:
        """是否落在顶部标题栏空白区，用于双击最大化 / 还原。

        标题栏 = Tab 栏这一整行（QTabBar + 右上角 corner 控件）。落在该行内、
        且非交互控件（按钮 / 具体 tab 标签等）的空白处即视为标题栏。
        """
        w = QApplication.instance().widgetAt(pos)
        if w is None or w.window() is not self:
            return False
        # 顶部 8px 留白（圆角内缩区）
        if w is self._wrap:
            return self._wrap.mapFromGlobal(pos).y() <= 8

        # 计算「Tab 栏这一行」的全局矩形（QTabBar 与 corner 控件的并集）
        tab_bar = self.tabs.tabBar()
        if tab_bar is None or not tab_bar.isVisible():
            return False
        tb_rect = QRect(tab_bar.mapToGlobal(QPoint(0, 0)), tab_bar.size())
        corner_rect = QRect(self._corner.mapToGlobal(QPoint(0, 0)),
                            self._corner.size())
        line_rect = tb_rect.united(corner_rect)
        if not line_rect.contains(pos):
            return False

        # 命中 QTabBar 自身：仅空白（非具体标签）算标题栏，点标签则切换 Tab
        if w is tab_bar:
            return tab_bar.tabAt(tab_bar.mapFromGlobal(pos)) == -1

        # 其余（corner 内空白 / 中间缝隙等）：排除交互控件后即视为标题栏空白
        node = w
        while node is not None and node is not self:
            if isinstance(node, self._NO_DRAG_TYPES):
                return False
            node = node.parentWidget()
        return True

    def eventFilter(self, obj, event):
        et = event.type()
        # 跨屏 DPI 突变：窗口被移动到另一台不同缩放比例的显示器时触发。
        # 在这里重置拖动状态，而非依赖 windowHandle().screenChanged，
        # 因为 windowHandle() 在窗口 show() 之前为 None，无法提前连接信号。
        if et == QEvent.Type.ScreenChangeInternal:
            self._on_screen_changed()
            return super().eventFilter(obj, event)
        if et == QEvent.Type.MouseButtonDblClick:
            # 双击自定义标题栏（Tab 栏空白 / 顶部留白）切换最大化 / 还原
            if (event.button() == Qt.LeftButton
                    and self._is_titlebar_area(event.globalPosition().toPoint())):
                self._on_toggle_maximize()
                return True
        elif et == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.LeftButton and not self.isFullScreen():
                pos = event.globalPosition().toPoint()
                # 优先：命中四边/四角 → 交给系统原生缩放窗口
                edges = self._resize_edges(pos)
                if edges:
                    if self.windowHandle() is not None:
                        self.windowHandle().startSystemResize(edges)
                        return True
                # 否则：非交互空白处拖动整窗
                if self._is_drag_target(pos):
                    # 偏移基于窗口「逻辑位置」（windowHandle().position()），
                    # 而非 frameGeometry().topLeft()：后者会被系统按窗口所在屏
                    # DPI 虚拟化，跨屏时二者坐标基准不一致，偏移会携带 DPR
                    # 误差 → 拖动瞬间窗口跳动。
                    win_pos = self.windowHandle().position() \
                        if self.windowHandle() is not None else self.pos()
                    self._drag_offset = pos - win_pos
                    self._dragging = True
                    return True
        elif et == QEvent.Type.MouseMove:
            if self._dragging:
                if event.buttons() & Qt.LeftButton:
                    # 用整型逻辑坐标移动（Qt 绑定对 QWidget.move/QWindow.
                    # setPosition 都只暴露整型重载，浮点精度在此不可用）。
                    # 关键在偏移已按同一逻辑坐标基准计算，跨屏 DPI 不会错位。
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                else:
                    self._dragging = False
                    self._drag_offset = None
                return True
            # 非拖动状态：更新边缘缩放光标
            self._update_resize_cursor(event.globalPosition().toPoint())
        elif et == QEvent.Type.MouseButtonRelease and self._dragging:
            self._dragging = False
            self._drag_offset = None
            return True
        return super().eventFilter(obj, event)