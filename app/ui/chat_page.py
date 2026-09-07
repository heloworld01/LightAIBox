"""「对话」页：在应用内直接与模型对话（多轮流式）。

仅做纯文本流式对话，暂不接桌面工具。模型调用走现有 Gateway（调度 / 配额 /
记录 / 降级等逻辑全部复用）。流式增量由后台线程经 Qt 信号送回主线程刷新，
避免阻塞 UI 事件循环。

UI 采用「气泡」式消息流：用户消息右对齐（主色），助手消息左对齐（面板色），
角色名字与内容分离。渲染经 QWebEngineView 承载真正的 HTML 页面，配合本地打包
的 MathJax 渲染 LaTeX 公式；气泡配色由 Python 端 _chat_palette() 内联注入，
跟随暗/亮主题切换。后续接入桌面 Agent 工具循环时，工具块可作为气泡内的嵌套结构
扩展。
"""
import base64 as _base64
import html as _html
import json as _json
import os as _os
import re as _re
import time as _time
from datetime import datetime as _datetime

import markdown as _md

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..db import ChatStore
from ..gateway import Gateway
from ..chat_session import ChatSession
from .i18n import LanguageManager
from .theme_manager import ThemeManager
from .widgets import MessageBox, SectionHeader
from ..approval import ApprovalCoordinator
from ..client import THINK_TAG, THINK_END_TAG
from ..agent_bridge import (TOOL_TAG, TOOL_END_TAG, SUB_OPEN, STEP_OPEN,
                            SUB_ANSWER, SUB_END)

_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"

# 智能体结构化流式：把一段 piece 切分为「带外结构 token」与普通文本。
# 结构 token：sub/step/sans 开事件（后跟 JSON，以 \x01 收尾）、/sub 收尾事件；
# 以及既有的 think/tool 完整段。文本块不属于任何 token，归属当前开启的步骤/子任务。
_AGENT_TOKEN_RE = _re.compile(
    r"(\x01sub\x01[^\x01]*\x01"
    r"|\x01step\x01[^\x01]*\x01"
    r"|\x01sans\x01[^\x01]*\x01"
    r"|\x01/sub\x01"
    r"|\x01think\x01.*?\x01/think\x01"
    r"|\x01tool\x01.*?\x01/tool\x01)")


# --------------------------------------------------------------------------- #
# 主题感知配色：气泡在 QTextEdit 富文本里用内联样式绘制（QTextEdit 的 rich
# text 支持有限 CSS 子集，故这里直接注入具体色值，跟随暗/亮主题切换）。
# --------------------------------------------------------------------------- #
def _chat_palette():
    """返回当前主题下对话气泡配色（暗/亮两套）。"""
    if ThemeManager().current() == ThemeManager.LIGHT:
        return {
            "page_bg": "#FFFFFF",
            "user_bubble": "#EEF2FF",       # 浅主色底
            "user_text": "#312E81",
            "assistant_bubble": "#E9EBEF",  # 浅灰底（略深，与白页拉开对比）
            "assistant_text": "#1A1F2E",
            "user_avatar": "#6366F1",
            "user_avatar_text": "#FFFFFF",
            "assistant_avatar": "#34D399",
            "meta": "#9BA3B5",
            "time_color": "#9BA3B5",
            "error": "#DC2626",
        }
    return {
        "page_bg": "#161B26",
        "user_bubble": "#2B3245",          # 主色偏暗底
        "user_text": "#E9ECF2",
        "assistant_bubble": "#1E2433",     # 面板色底
        "assistant_text": "#E9ECF2",
        "user_avatar": "#6366F1",
        "user_avatar_text": "#FFFFFF",
        "assistant_avatar": "#34D399",
        "meta": "#9BA3B5",
        "time_color": "#6B7486",
        "error": "#F87171",
    }


def _short_repr(obj, limit: int = 500):
    """把审批弹窗里的参数压缩成单行可读文本（截断超长内容，避免撑爆对话框）。"""
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            parts.append(f"{k}={_short_repr(v, 120)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(_short_repr(v, 80) for v in obj) + "]"
    s = str(obj)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


class _ChatWorker(QThread):
    """独立线程消费 Gateway.chat_stream 或智能体会话，把增量经信号送回主线程。

    Gateway.chat_stream 在 provider 层是阻塞网络请求，不能放进主线程；
    QThread（run 里同步迭代生成器）是 PySide6 下最直接的隔离方式。
    智能体模式（agent_mode=True）时改用 AgentChatSession.chat_stream——
    二者接口一致（yield 文本增量，StopIteration.value 为 ClientResult）。
    """

    token = Signal(str)
    finished = Signal(object)  # ClientResult
    failed = Signal(str)

    def __init__(self, gateway: Gateway, messages: list,
                 provider_id=None, thinking_enabled: bool = False,
                 agent_mode: bool = False, approval=None,
                 agent_session=None, parent=None):
        super().__init__(parent)
        self._gateway = gateway
        self._messages = messages
        self._provider_id = provider_id
        self._thinking_enabled = thinking_enabled
        self._agent_mode = agent_mode
        # 智能体写文件的运行时授权协调器（跑在主线程，跨线程经 Qt 信号弹窗）
        self._approval = approval
        # 跨轮复用的智能体会话（Fix 3）：由 ChatPage 持有并沿多轮共享同一个实例，
        # 使工具激活状态与跨轮历史上下文得以保持；为 None 时每轮新建（无记忆）。
        self._agent_session = agent_session
        self._stop = False

    def run(self) -> None:
        try:
            if self._agent_mode:
                session = self._agent_session
                if session is None:
                    from ..agent_bridge import AgentChatSession
                    session = AgentChatSession(
                        self._gateway, provider_id=self._provider_id,
                        approval=self._approval)
                it = session.chat_stream(
                    self._messages, thinking_enabled=self._thinking_enabled)
            else:
                it = self._gateway.chat_stream(
                    self._messages, provider_id=self._provider_id,
                    thinking_enabled=self._thinking_enabled)
            try:
                while not self._stop:
                    try:
                        piece = next(it)
                    except StopIteration as e:
                        result = e.value  # ClientResult
                        break
                    if piece:
                        self.token.emit(piece)
            finally:
                try:
                    it.close()
                except Exception:
                    pass
            if not self._stop:
                self.finished.emit(result)
        except Exception as exc:  # 网关/网络异常
            self.failed.emit(str(exc))

    def stop(self) -> None:
        self._stop = True


class _ChatPage(QWebEnginePage):
    """聊天 WebView 页面：拦截 toggle 链接，并阻止任何真实页面导航。

    气泡内的「原文 / 渲染」切换链接 href 编码为 chat:raw:<idx> / chat:md:<idx>。

    QWebEngine 对未知自定义 scheme（chat:）的链接点击不会触发导航、也就不经过
    acceptNavigationRequest（多次实测：程序化点击与 location.href 跳转都不触发），
    且改 fragment 属同文档导航也不经过这里。因此 chat.html 内的点击监听会把 chat:
    链接改写为「本页 file URL + ?__lightaibox__=<action>」的脚本化导航——跨文档的
    file scheme 导航才会可靠地到达此处。此处解析 query 后交回 ChatPage 翻转显示格式，
    并返回 False 阻断（不真正加载、不产生历史/滚动）。
    其余任意导航（外链、表单等）一律拒绝，WebView 只承载本地 chat.html 一个页面。
    """

    # 与 chat.html 内点击监听共用：动作的 query 标记（?__lightaibox__=raw:<idx>）
    _LINK_QUERY_MARKER = "__lightaibox__="

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        href = url.toString()
        # 优先识别 chat.html 转发的 toggle 动作（?__lightaibox__=raw:<idx> 等）
        marker = href.find(self._LINK_QUERY_MARKER)
        if marker >= 0:
            action = href[marker + len(self._LINK_QUERY_MARKER):]
            # 还原成 chat:raw:<idx> / chat:md:<idx>，交给 ChatPage 既有解析逻辑
            self._owner._on_anchor_clicked(QUrl("chat:" + action))
            return False
        # 兜底：直接以 chat: 开头的导航（历史方案，自定义 scheme 通常到不了这里）
        if href.startswith("chat:"):
            self._owner._on_anchor_clicked(QUrl(href))
            return False
        # 仅放行初始加载的本页文件（chat.html），其余文件一律拒绝
        if url.scheme() == "file":
            try:
                base = _os.path.dirname(_os.path.abspath(self._owner._chat_html_path))
                target = _os.path.abspath(url.toLocalFile())
                if _os.path.dirname(target) == base:
                    return True
            except Exception:
                return False
        return False


class ChatPage(QWidget):
    def __init__(self, gateway: Gateway, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self.tr = LanguageManager().tr
        self._settings = QSettings("LightAIBox", "LightAIBox")
        # 多会话持久化存储（SQLite）；self.session 为当前活动会话
        self._store = ChatStore()
        self.session = ChatSession()
        # 侧栏展示用的会话元信息列表（来自 store.list_sessions()）
        self._sessions: list = []
        # 上下文预算（QSettings 覆盖默认）：发给模型的对话超此值自动裁最旧
        self._context_tokens = int(self._settings.value(
            "chat/context_tokens", config.CHAT_CONTEXT_TOKENS))

        # 聊天字体大小（QSettings 持久化，默认 13px）
        self._font_size = self._settings.value("chat/font_size", 13, type=int)

        # 后台生成线程；_pending_assistant 累积当前这条助手回复的文本。
        self._worker: _ChatWorker | None = None
        # 智能体模式的跨轮复用会话（Fix 3）：沿当前对话多轮共享，使工具激活状态与
        # 跨轮历史记忆得以保持；切换/新建/清空会话时置 None 重建（新对话=新记忆）。
        self._agent_session = None
        self._agent_session_sid: int | None = None
        # 被停止但仍未结束的线程会挂这里回收，避免运行中的 QThread 被 GC 销毁崩溃。
        self._zombie_workers: list = []
        self._zombie_timer = QTimer(self)
        self._zombie_timer.setInterval(100)
        self._zombie_timer.timeout.connect(self._on_zombie_tick)
        self._pending_assistant: list = []
        self._last_stats: str = ""  # 上次助手回复的 token 统计（气泡内显示）
        # provider 下拉的数据签名：仅当可用集合变化时才重建下拉，避免轮询
        # 反复 clear() 把用户正在展开的下拉强制关闭（与 gateway_page 的
        # _provider_filter_sig 同思路）。
        self._provider_sig: tuple | None = None
        # 每个助手消息的显示格式：msg_index -> 是否显示原始 markdown（默认渲染）
        self._render_raw: dict = {}
        # 思考模式开关（仅当次会话生效，QSettings 持久化记忆偏好）
        self._thinking_on = self._settings.value(
            "chat/thinking", False, type=bool)
        # 智能体模式开关（QSettings 持久化记忆偏好）
        self._agent_on = self._settings.value(
            "chat/agent", False, type=bool)
        # 每封助手消息的思考内容展开状态：msg_index -> bool（默认折叠）
        self._think_open: dict = {}
        # 流式中正在累积的思考内容
        self._pending_thinking: list = []
        # 智能体模式：本轮已发生的工具活动（按发生顺序的 (name, kind, ok, args/text)）
        self._pending_tools: list = []
        # 智能体「结构化步骤」的流式累积状态：已收尾的子任务块、当前正开的子任务/
        # 步骤、以及子任务外的根文本（多子任务的 LLM 汇总）。流式期间据此分块渲染，
        # 使各步骤的回答与其步骤同步出现，而非等整轮结束才归并。
        self._pending_blocks: list = []
        self._cur_sub: dict | None = None
        self._cur_step: dict | None = None
        self._root: list = []
        self._structured: bool = False
        # 智能体写文件的运行时授权：工具线程请求 → 主线程弹确认框 → 写回结果。
        self._approval = ApprovalCoordinator(self)
        self._approval.approval_requested.connect(self._on_approval_requested)
        # 待发送的图片（data URL 列表）：用户点「图片」按钮加入，随下一条消息发出
        self._pending_images: list = []
        # 底部瞬时提示（调用失败 / 已停止），随消息流一起注入视图底部
        self._notice_html: str = ""
        # WebView 页面（含 MathJax）是否已加载完成；加载前不注入，避免
        # __renderChat 尚未定义时 runJavaScript 抛错导致首屏内容/公式丢失。
        self._page_ready = False
        # 助手头像：打包 logo.png 读成 data URI（圆形裁切展示），避免每次渲染重读盘。

        self._build_ui()
        # 会话侧栏显隐（QSettings 记忆偏好，默认显示）
        self._sidebar_visible = self._settings.value(
            "chat/sidebar_visible", True, type=bool)
        self._apply_sidebar_visible()
        # 载入默认会话（最近会话或新建）并填充侧栏
        self._init_current_session()
        self.refresh_sidebar()
        self._update_think_btn_text()
        self._update_agent_btn_text()
        self.refresh_providers()
        # 轮询刷新模型下拉：AI 网关页增删/启停 provider、配额用尽自动关闭等
        # 状态变化不会通知对话页，这里用与 ProvidersBar 相同的定时轮询方式
        # 保持下拉与网关数据源实时同步（签名去重见 refresh_providers）。
        self._provider_timer = QTimer(self)
        self._provider_timer.setInterval(1200)
        self._provider_timer.timeout.connect(self.refresh_providers)
        self._provider_timer.start()
        # 首次内容渲染改由 _on_page_loaded（loadFinished）触发，确保注入的
        # HTML/MathJax 在已就绪的页面里被扫描；此处不再提前 push。
        # self._reset_history_view()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        # 左右结构：左侧会话边栏 + 右侧聊天（头部/记录/输入）
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 16, 12, 16)
        outer.setSpacing(12)
        self._sidebar = self._build_sidebar()
        outer.addWidget(self._sidebar)
        _right = QWidget()
        root = QVBoxLayout(_right)
        root.setContentsMargins(4, 0, 4, 0)
        root.setSpacing(12)

        # 顶行：标题（左）+ provider 选择（右），底部细分隔线
        self.header = SectionHeader(self.tr("对话", "Chat"))
        self.provider_label = QLabel(self.tr("模型", "Model"))
        self.provider_label.setProperty("class", "stats-text")
        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(200)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        # 聊天字体大小选择（大/中/小 三档，持久化到 QSettings；内部存像素值）
        self.font_label = QLabel(self.tr("字号", "Size"))
        self.font_label.setProperty("class", "stats-text")
        # 档位：小=12 / 中=13（默认）/ 大=16。以 px 为唯一单位，三区域（问答气泡 /
        # 输入框 / 会话列表）经 _font_size 统一，观感一致。
        self._FONT_PRESETS = [("小", "S", 12), ("中", "M", 13), ("大", "L", 16)]
        self.font_combo = QComboBox()
        self.font_combo.setMinimumWidth(72)
        for zh, en, size in self._FONT_PRESETS:
            self.font_combo.addItem(self.tr(zh, en), size)
        idx = self.font_combo.findData(self._font_size)
        self.font_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.font_combo.currentIndexChanged.connect(self._on_font_changed)

        # 思考模式开关（可勾选按钮），持久化偏好
        self.think_btn = QPushButton()
        self.think_btn.setCheckable(True)
        self.think_btn.setChecked(self._thinking_on)
        self.think_btn.setProperty("class", "ghost")
        self.think_btn.clicked.connect(self._on_toggle_thinking)

        # 智能体模式开关：接入 LightAgents 智能体框架问答（可勾选按钮），持久化偏好
        self.agent_btn = QPushButton()
        self.agent_btn.setCheckable(True)
        self.agent_btn.setChecked(self._agent_on)
        self.agent_btn.setProperty("class", "ghost")
        self.agent_btn.setToolTip(self.tr(
            "开启后走 LightAgents 智能体框架（多协议 / 多模态）生成回复",
            "Route replies through the LightAgents agent framework"))
        self.agent_btn.clicked.connect(self._on_toggle_agent)

        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(12)
        # 会话侧栏显隐切换（状态持久化）
        self.toggle_sidebar_btn = QPushButton("☰")
        self.toggle_sidebar_btn.setProperty("class", "ghost")
        self.toggle_sidebar_btn.setFixedWidth(36)
        self.toggle_sidebar_btn.clicked.connect(self._on_toggle_sidebar)
        tl.addWidget(self.toggle_sidebar_btn)
        tl.addWidget(self.header.title_label, 1)
        tl.addWidget(self.provider_label)
        tl.addWidget(self.provider_combo)
        tl.addWidget(self.font_label)
        tl.addWidget(self.font_combo)
        tl.addWidget(self.think_btn)
        tl.addWidget(self.agent_btn)
        root.addWidget(top)

        # 对话记录：QWebEngineView 承载 chat.html（MathJax 渲染公式）。
        # 页面只读展示；导航被 _ChatPage.acceptNavigationRequest 拦截。
        self._chat_html_path = _os.path.join(
            config.RESOURCES_DIR, "chat", "chat.html")
        self.view = QWebEngineView()
        self.view.setObjectName("chatView")
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        chat_settings = self.view.settings()
        # 允许本地页面访问本地文件，使 MathJax 能读取打包的字体/扩展
        chat_settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        # 禁止 Js 弹窗与桌面通知，避免模型文本诱导页面弹窗
        chat_settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        self.view.setPage(_ChatPage(self, self.view))
        # 对话页整体透明：Chromium 视图级底色设为 Qt.transparent，让页面透出底下
        # 跟随 QSS 的父容器/窗口底色。切主题时外层换 QSS 即时生效，WebView 同步显新
        # 底色，不再有「中间区滞后一帧 / 上下分色」。必须在 setUrl 前设置，使首帧
        # 即为透明（否则渲染主机会先按默认白底提交一帧）。
        self.apply_theme_colors()
        # 页面加载完成（脚本 / MathJax 就绪）后再注入首屏内容，见 _on_page_loaded
        self.view.loadFinished.connect(self._on_page_loaded)
        self.view.setUrl(QUrl.fromLocalFile(self._chat_html_path))
        root.addWidget(self.view, 1)

        # 流式刷盘合并：生成期间不要每个 token 都让 MathJax 全量重排
        self._pending_html: str | None = None
        self._pending_stick: bool = True
        self._pending_force_pin: bool = False
        # 增量流式：只更新「最后一条进行中气泡」的 HTML（走 __appendStreaming）
        self._pending_stream_html: str | None = None
        self._pending_stream_cursor: bool = True
        # 自适应刷盘节奏：按最近 token 到达间隔调整合并窗
        self._gap_ema: float | None = None
        self._last_token_ts: float | None = None
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(120)
        self._flush_timer.timeout.connect(self._flush_stream)

        # 输入栏：多行输入 + 底部工具条（发送/停止/清空 + 提示）
        input_panel = QFrame()
        input_panel.setProperty("class", "panel")
        iv = QVBoxLayout(input_panel)
        iv.setContentsMargins(12, 12, 12, 10)
        iv.setSpacing(8)

        self.input = QTextEdit()
        self.input.setPlaceholderText(
            self.tr("输入消息…", "Type a message…"))
        self.input.setFixedHeight(76)
        self.input.setAcceptRichText(False)
        self.input.setFrameShape(QFrame.NoFrame)
        self.input.setObjectName("chatInput")
        self._apply_input_font()
        self._apply_sidebar_font()
        iv.addWidget(self.input)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        # 图片上传按钮：选择本地图片，读成 base64 data URL 暂存，随下次发送一并提交
        self.image_btn = QPushButton(self.tr("🖼 图片", "🖼 Image"))
        self.image_btn.setProperty("class", "ghost")
        self.image_btn.setToolTip(self.tr(
            "添加图片（多模态对话），图片随下一条消息一并发送",
            "Add an image (multimodal), sent with the next message"))
        self.image_btn.clicked.connect(self._on_add_image)
        foot.addWidget(self.image_btn)

        self.hint_label = QLabel(self.tr("Enter 换行 · Ctrl+Enter 发送",
                                         "Enter new line · Ctrl+Enter send"))
        self.hint_label.setProperty("class", "stats-text")
        foot.addWidget(self.hint_label)
        foot.addStretch()

        self.clear_btn = QPushButton(self.tr("清空", "Clear"))
        self.clear_btn.setProperty("class", "ghost")
        self.clear_btn.clicked.connect(self._on_clear)
        foot.addWidget(self.clear_btn)

        self.stop_btn = QPushButton(self.tr("停止", "Stop"))
        self.stop_btn.setProperty("class", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        foot.addWidget(self.stop_btn)

        self.send_btn = QPushButton(self.tr("发送", "Send"))
        self.send_btn.setProperty("class", "primary")
        self.send_btn.setMinimumWidth(96)
        self.send_btn.clicked.connect(self._on_send)
        foot.addWidget(self.send_btn)

        iv.addLayout(foot)
        root.addWidget(input_panel)
        outer.addWidget(_right, 1)

        # 输入框内 Ctrl+Enter 发送
        self.input.installEventFilter(self)

    # ------------------------------------------------------------------ #
    # 会话侧边栏
    # ------------------------------------------------------------------ #
    def _build_sidebar(self) -> QFrame:
        """左侧会话边栏：新建按钮 + 会话列表（右键重命名/删除）。"""
        panel = QFrame()
        panel.setProperty("class", "panel")
        panel.setFixedWidth(200)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(8)

        self.new_btn = QPushButton(self.tr("＋ 新建会话", "＋ New"))
        self.new_btn.setProperty("class", "primary")
        self.new_btn.setToolTip(
            self.tr("开始一段新对话", "Start a new conversation"))
        self.new_btn.clicked.connect(self._on_new_session)
        pl.addWidget(self.new_btn)

        head = QLabel(self.tr("会话", "Sessions"))
        head.setProperty("class", "stats-text")
        pl.addWidget(head)

        self.sess_list = QListWidget()
        self.sess_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.sess_list.customContextMenuRequested.connect(self._on_session_menu)
        self.sess_list.itemClicked.connect(self._on_session_clicked)
        pl.addWidget(self.sess_list, 1)
        return panel

    def _apply_sidebar_visible(self) -> None:
        """按持久化状态应用侧栏显隐，并同步切换按钮外观。"""
        self._sidebar.setVisible(self._sidebar_visible)
        # 展开态显示收起方向箭头，折叠态显示汉堡图标，提示点击动作
        self.toggle_sidebar_btn.setText("❮" if self._sidebar_visible else "☰")
        self.toggle_sidebar_btn.setToolTip(
            self.tr("显示/隐藏会话列表", "Show/Hide session list"))

    @Slot()
    def _on_toggle_sidebar(self) -> None:
        """切换会话侧栏显隐（偏好持久化）。"""
        self._sidebar_visible = not self._sidebar_visible
        self._settings.setValue("chat/sidebar_visible", self._sidebar_visible)
        self._apply_sidebar_visible()

    def _init_current_session(self) -> None:
        """启动时选出默认会话：有历史取最近会话，否则新建一个。"""
        self._sessions = self._store.list_sessions()
        if self._sessions:
            s = self._sessions[0]
            self.session = ChatSession(
                session_id=s["id"], title=s["title"], summary=s["summary"])
            self.session.load_messages_from(
                self._store.load_messages(s["id"]))
        else:
            sid = self._store.create_session()
            self.session = ChatSession(session_id=sid, title="新对话")

    def refresh_sidebar(self) -> None:
        """从库重载会话列表并刷新侧栏（保持当前会话高亮）。"""
        self._sessions = self._store.list_sessions()
        self.sess_list.clear()
        for s in self._sessions:
            item = QListWidgetItem(self._session_label(s))
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            item.setToolTip(s["title"] or "新对话")
            self.sess_list.addItem(item)
        if self.session.id is not None:
            self._select_session_in_list(self.session.id)

    def _select_session_in_list(self, sid: int) -> None:
        for i in range(self.sess_list.count()):
            it = self.sess_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == sid:
                self.sess_list.setCurrentItem(it)
                return

    def _session_label(self, s: dict) -> str:
        title = s["title"] or "新对话"
        rel = self._fmt_rel_time(s.get("updated_at") or "")
        return f"{title}  ·  {rel}" if rel else title

    def _fmt_rel_time(self, ts_text: str) -> str:
        """把 'YYYY-MM-DD HH:MM:SS' 格式成中文相对时间。"""
        try:
            import datetime as _dt
            dt = _dt.datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
        now = _dt.datetime.now()
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        if (now.date() - dt.date()).days == 1:
            return self.tr("昨天", "yesterday")
        if dt.year == now.year:
            return dt.strftime("%m-%d")
        return dt.strftime("%Y-%m-%d")

    def _on_new_session(self) -> None:
        if self._worker is not None:
            self._flash_notice(
                self.tr("正在生成，无法新建会话", "Still generating, try later"))
            return
        sid = self._store.create_session()
        self.session = ChatSession(session_id=sid, title="新对话")
        self._reset_session_view()
        self.refresh_sidebar()

    def _set_active_session(self, sid: int, force: bool = True) -> None:
        """切换到指定会话（载入其历史与摘要）。

        生成进行中禁止切换：_on_send 已把用户消息写进当前会话，_on_finished 会按
        self.session.id 落库——切走会让回复写进错误会话。
        """
        if self._worker is not None:
            self._flash_notice(
                self.tr("正在生成，无法切换会话", "Still generating, try later"))
            return
        for s in self._sessions:
            if s["id"] != sid:
                continue
            self.session = ChatSession(
                session_id=sid, title=s["title"], summary=s["summary"])
            self.session.load_messages_from(
                self._store.load_messages(sid))
            self._reset_session_view()
            return

    def _on_session_clicked(self, item: QListWidgetItem) -> None:
        sid = item.data(Qt.ItemDataRole.UserRole)
        if sid != self.session.id:
            self._set_active_session(sid)

    def _reset_session_view(self, force: bool = True) -> None:
        """切换/新建会话后：清空流式中间态并整段重渲染。"""
        self._pending_assistant = []
        self._pending_thinking = []
        self._pending_tools = []
        self._pending_blocks = []
        self._cur_sub = None
        self._cur_step = None
        self._root = []
        self._structured = False
        self._notice_html = ""
        self._reset_history_view(force=force)

    def _on_session_menu(self, pos) -> None:
        item = self.sess_list.itemAt(pos)
        if item is None or self._worker is not None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        rename_act = menu.addAction(self.tr("重命名…", "Rename…"))
        delete_act = menu.addAction(self.tr("删除会话", "Delete"))
        act = menu.exec(self.sess_list.mapToGlobal(pos))
        if act is rename_act:
            self._rename_session(sid, item)
        elif act is delete_act:
            self._delete_session(sid)

    def _rename_session(self, sid: int, item: QListWidgetItem) -> None:
        old = item.text().split("  ·  ")[0]
        text, ok = QInputDialog.getText(
            self, self.tr("重命名", "Rename"),
            self.tr("会话名称", "Session name"), text=old)
        if ok and text.strip():
            self._store.rename_session(sid, text.strip())
            if sid == self.session.id:
                self.session.title = text.strip()
            self.refresh_sidebar()

    def _delete_session(self, sid: int) -> None:
        if QMessageBox.question(
                self, self.tr("删除会话", "Delete session"),
                self.tr("删除该会话及其全部消息？", "Delete this session and all its messages?"),
        ) != QMessageBox.StandardButton.Yes:
            return
        self._store.delete_session(sid)
        self._sessions = self._store.list_sessions()
        if not self._sessions:
            ns = self._store.create_session()
            self.session = ChatSession(session_id=ns, title="新对话")
        else:
            # 删除的是当前会话时跳回最近会话
            if sid == self.session.id:
                t = self._sessions[0]
                self.session = ChatSession(
                    session_id=t["id"], title=t["title"], summary=t["summary"])
                self.session.load_messages_from(
                    self._store.load_messages(t["id"]))
        self.refresh_sidebar()
        self._reset_session_view()

    # ------------------------------------------------------------------ #
    # 上下文构建（三合一）：自动裁剪 + 摘要注入 + 清空
    # ------------------------------------------------------------------ #
    def _build_context_messages(self) -> dict:
        """计算发给模型的对话：摘要(若有) + 预算内最近尾部。

        展示历史始终完整保留在 session.messages / DB；这里只决定「发给模型」
        的部分——最旧的超出预算即被自动裁掉，保证上下文窗口不撑爆。
        """
        budget = max(int(self._context_tokens), 200)
        summary = self.session.summary or ""
        reserve = len(summary)
        tail = []
        used = 0
        for m in reversed(self.session.messages):
            t = self.session.estimate_tokens([m])
            if tail and reserve + used + t > budget:
                break
            tail.append(m)
            used += t
        tail.reverse()
        sent = []
        if summary:
            sent.append({"role": "system", "content": summary})
        sent.extend(tail)
        return sent

    def _flash_notice(self, text: str) -> None:
        pal = _chat_palette()
        self._notice_html = (
            f'<div style="margin:6px 0;color:{pal["meta"]};font-size:13px;">'
            f'{_html.escape(text)}</div>')
        self._reset_history_view(force=True)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ #
    # 气泡渲染（主题感知内联样式）
    # ------------------------------------------------------------------ #
    def _render_markdown(self, text: str, font_size: int) -> str:
        """把助手回复的 markdown 渲染为主题感知的安全 HTML。

        流程：markdown 库渲染（关闭反斜杠转义，保留 LaTeX 定界符）-> 剔除危险
        标签/事件/javascript: -> 给代码块/表格/标题注入内联样式。最终 HTML 进入
        QWebEngineView，由本地打包的 MathJax 扫描 \(...\) / \[...\] 渲染公式。
        """
        pal = _chat_palette()
        fs = str(font_size) + "px"
        fg = pal["assistant_text"]

        # 关键：Python-Markdown 默认把 \\ 反斜杠转义成字面字符（\\( → (），
        # 会毁掉 MathJax 的行内/块级定界符并吃掉 \times / \frac 等宏。停用
        # 'escape' inline processor 后，反斜杠原样保留，交给浏览器里的 MathJax
        # 处理（MathJax 的 processEscapes 负责 \$ 之类的转义语义）。
        _rm_markdown_instance = _md.Markdown(
            extensions=["fenced_code", "tables"])
        try:
            _rm_markdown_instance.inlinePatterns.deregister("escape")
        except (KeyError, ValueError):
            pass
        body = _rm_markdown_instance.convert(text)

        # 危险标签（成对 + 自闭合）
        body = _re.sub(
            r"<(script|iframe|object|embed|style|meta|link)\b[^>]*>.*?</\1>",
            "", body, flags=_re.IGNORECASE | _re.DOTALL)
        body = _re.sub(
            r"<(script|iframe|object|embed|style|meta|link)\b[^>]*/?>",
            "", body, flags=_re.IGNORECASE)
        # 事件属性 + javascript: 协议
        body = _re.sub(
            r"\b(on\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
            "", body, flags=_re.IGNORECASE)
        body = _re.sub(
            r"\b(href|src)\s*=\s*[\"']?javascript:[^\"'>\s]*[\"']?",
            "", body, flags=_re.IGNORECASE)

        # 代码围栏里显式声明 latex/tex/math 的块当作「渲染公式」而非代码：
        # 去掉 <pre><code>（MathJax 的 skipHtmlTags 会跳过它们），剥离 % 注释行后
        # 交给 MathJax 渲染成居中式公式。
        #   1) 内容自带定界符（$ $、\( \)、\[ \]、$$）：不再外包一层 \[...\]（否则
        #      MathJax 会在数学模式里撞到 \[ 报 "Undefined control sequence \["），
        #      直接把转义后的内容放进居中 div，由 MathJax 扫描其自带的定界符渲染。
        #   2) 否则为「裸公式体」：剥注释、清空行后用 \[...\] 显式定界排版成 display
        #      math（多行公式之间不留空行，避免 MathJax 报 Blank Line）。
        # 注入内容一律 HTML 转义：公式里的 < > & 会被浏览器回解为文本、MathJax 仍能
        # 识别，同时不会把它们当标签解析，杜绝注入。
        def _render_latex_block(raw):
            lines = [_re.sub(r'(?<!\\)%.*$', '', ln) for ln in raw.splitlines()]
            cleaned = '\n'.join(ln for ln in lines if ln.strip()).strip()
            if not cleaned:
                return ''
            stem = _html.escape(cleaned)
            if _re.search(r'\\(?:\(|\)|\[|\])|\$', cleaned):
                # 自带定界符：不外包，直接渲染
                return ('<div style="text-align:center;margin:8px 0;">'
                        + stem + '</div>')
            return ('<div style="text-align:center;margin:8px 0;">\\['
                    + stem + '\\]</div>')

        body = _re.sub(
            r'<pre><code class="language-(?:latex|tex|math)">(.*?)</code></pre>',
            lambda m: _render_latex_block(_html.unescape(m.group(1))),
            body, flags=_re.IGNORECASE | _re.DOTALL)

        # mermaid 围栏：包成 <pre class="mermaid">(内容保持 HTML 转义)，由前端
        # mermaid.run 读 textContent 渲染成 SVG。必须赶在下方通用代码块替换之前，
        # 否则会被当普通等宽代码块。securityLevel 在前端设 strict 防注入。
        body = _re.sub(
            r'<pre><code class="language-mermaid">(.*?)</code></pre>',
            lambda m: ('<pre class="mermaid">'
                       + m.group(1).strip()
                       + '</pre>'),
            body, flags=_re.IGNORECASE | _re.DOTALL)

        # 代码块：等宽字体，透明底色（去除单独背景，统一随气泡/页面色）。
        # 保留 code 上的 language-* class；padding/border-radius 仍留着，
        # 无底色时不影响布局，仅不再绘制独立色块。
        body = _re.sub(
            r"<pre><code([^>]*)>",
            lambda m: ('<pre style="display:block;background:transparent;'
                       ';padding:8px;border-radius:6px;font-size:' + fs +
                       ';margin:6px 0;white-space:pre-wrap;">'
                       '<code' + m.group(1) +
                       ' style="font-family:Consolas,\'Courier New\',monospace;'
                       'font-size:' + fs + ';color:' + fg + ';">'),
            body)

        # 标题：随字号等比缩放，避免 h1 过大
        base = font_size
        for lv, mul in ((1, 1.30), (2, 1.20), (3, 1.10), (4, 1.05)):
            px = str(int(round(base * mul))) + "px"
            body = _re.sub(
                r"<h" + str(lv) + r">",
                lambda m, p=px: ('<h' + str(lv) + ' style="margin:8px 0 4px;'
                                 'color:' + fg + ';font-size:' + p + ';">'),
                body)

        # 表格：细边框，表头透明底色（去除单独背景）
        body = body.replace("<table>",
                            '<table style="border-collapse:collapse;margin:6px 0;">')
        body = body.replace("<th>",
                            '<th style="background:transparent;'
                            ';padding:4px 8px;border:1px solid ' + pal["meta"] + ';">')
        body = body.replace("<td>",
                            '<td style="padding:4px 8px;border:1px solid ' +
                            pal["meta"] + ';">')
        return body

    @staticmethod
    def _fmt_time(ts: float) -> str:
        """把时间戳格式化为消息时间条文案：当天 HH:MM，跨天 MM/DD HH:MM。"""
        dt = _datetime.fromtimestamp(ts)
        if dt.date() == _datetime.now().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m/%d %H:%M")

    def _time_chip_html(self, ts: float) -> str:
        """微信风格的时间分隔条：居中、小号灰字。"""
        pal = _chat_palette()
        return (f'<div style="text-align:center;color:{pal["time_color"]};'
                f'font-size:12px;margin:6px 0 2px;">'
                f'{_html.escape(self._fmt_time(ts))}</div>')

    def _avatar_html(self, role: str) -> str:
        """圆角矩形头像。助手：主色块 + 「AI」；你：主色块 + 角色首字。"""
        pal = _chat_palette()
        base = ('width:36px;height:36px;border-radius:8px;flex-shrink:0;'
                'background:{bg};color:{fg};display:flex;'
                'align-items:center;justify-content:center;font-size:14px;'
                'font-weight:600;')
        if role == _ROLE_ASSISTANT:
            label = self.tr("AI", "AI")
        else:
            label = self.tr("我", "Me")
        style = base.format(bg=pal["user_avatar"],
                            fg=pal["user_avatar_text"])
        return (f'<div style="{style}">'
                f'{_html.escape(label)}</div>')

    def _bubble_group_html(self, role: str, bubble_html: str, pal: dict,
                           bubble_bg: str) -> str:
        """气泡 + 指向头像的尾巴。assistant 尾朝左、user 尾朝右。"""
        tail_svg = ('<svg width="8" height="12" viewBox="0 0 8 12" '
                    'style="flex-shrink:0;margin-top:9px;">')
        if role == _ROLE_ASSISTANT:
            path = '<path d="M0 6 L8 0 L8 12 Z" fill="{bg}"/></svg>'
            tail = (tail_svg + path + bubble_html)
            style = "display:flex;align-items:flex-start;"
        else:
            path = '<path d="M8 6 L0 0 L0 12 Z" fill="{bg}"/></svg>'
            # 用户：从左到右 [气泡][尾巴]，尾朝右指向头像；两端用 flex 行即可
            tail = (bubble_html + tail_svg + path)
            style = "display:flex;align-items:flex-start;"
        # 用 replace 而不是 .format：tail 里还拼着 bubble_html，可能含 LaTeX /
        # 用户文本的 {…}，.format 会把它们当占位符而抛 KeyError。
        tail = tail.replace("{bg}", bubble_bg)
        return f'<div style="{style}">{tail}</div>'

    def _split_content(self, content) -> tuple:
        """把消息 content 规整为 (text, images)。

        字符串 content → 纯文本、无图；块列表 content 抽取 text 块的文本与
        image_url / image 块的 data URL。用于气泡渲染（与模型透传的口径一致）。
        """
        if isinstance(content, str):
            return content, []
        text_parts: list = []
        images: list = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", ""))
            elif bt in ("image_url", "input_image"):
                inner = b.get("image_url") or b.get("input_image") or {}
                url = inner.get("url", "") if isinstance(inner, dict) else ""
                if url:
                    images.append(url)
            elif bt == "image":
                src = b.get("source") or {}
                if isinstance(src, dict):
                    data = src.get("data", "")
                    if data:
                        # Anthropic 的 base64 裸数据：拼成 data URL 供 <img> 展示
                        mtype = src.get("media_type", "image/png")
                        images.append(f"data:{mtype};base64,{data}")
        return "".join(text_parts), images

    def _render_block(self, role: str, text: str, meta: str = "",
                      font_size: int = 13, msg_index: int | None = None,
                      show_time: bool = False, mtime: float | None = None,
                      thinking: str = "", images: list | None = None,
                      tools: list | None = None,
                      agent_blocks: list | None = None) -> str:
        """把一条消息渲染为微信式气泡（圆形头像 + 带头尾气泡）。

        布局（flex）：assistant 头像在左、气泡在右；user 头像在右、气泡在左。
        两端不再用 <table align> 浮动。assistant 气泡下方带 token 统计与
        「原始/渲染」切换链接；show_time=True 时上方插入居中时间条。
        thinking 非空时（assistant），在气泡正文上方插入可折叠的思考块。

        智能体模式（agent_blocks 非空）：正文渲染为「结构化步骤」视图——
        每个子任务一块，其内部各 ReAct 步骤（模型正文 + 思考 + 工具活动）与
        该步骤放在一起展示；多子任务时的 LLM 汇总作为最终回答置顶/置尾。
        此时消息级的 thinking/tools 已被并入步骤块，不再单独渲染，避免重复。
        """
        pal = _chat_palette()
        text = str(text)
        if role == _ROLE_USER:
            bubble_bg = pal["user_bubble"]
            text_color = pal["user_text"]
            reverse = True          # 头像靠右
            radius = "12px 6px 12px 12px"   # 头像侧(右下)大圆角
        else:
            bubble_bg = pal["assistant_bubble"]
            text_color = pal["assistant_text"]
            reverse = False         # 头像靠左
            radius = "6px 12px 12px 12px"   # 头像侧(左上)大圆角

        raw = False
        under = ""
        think_html = ""
        tools_html = ""
        if role == _ROLE_ASSISTANT:
            raw = msg_index is not None and self._render_raw.get(msg_index, False)
            if agent_blocks:
                # 结构化步骤视图：正文即各子任务块 + 汇总；原文视图仍显示整段 markdown
                body = (_html.escape(text).replace("\n", "<br>") if raw
                        else self._agent_sections_html(agent_blocks, font_size))
            else:
                body = (_html.escape(text).replace("\n", "<br>") if raw
                        else self._render_markdown(text, font_size))
            # 思考块 / 工具活动：结构化视图下已并入各步骤块，不再顶层重复渲染
            if thinking and not agent_blocks:
                think_html = self._thinking_html(msg_index, thinking, font_size)
            if tools and not agent_blocks:
                tools_html = self._tools_html(tools, font_size)
            bits = []
            if meta:
                bits.append(f'<span>{_html.escape(meta)}</span>')
            if msg_index is not None:
                bits.append(self._raw_toggle_html(msg_index, raw))
            if bits:
                sep = "&nbsp;&nbsp;·&nbsp;&nbsp;"
                under = (f'<div style="font-size:11px;color:{pal["meta"]};'
                         f'margin-top:4px;">{sep.join(bits)}</div>')
        else:
            body = _html.escape(text).replace("\n", "<br>")
            # 用户消息携带的图片：以缩略图形式内联展示（base64 data URL，无需额外资源）
            if images:
                imgs = "".join(
                    f'<img src="{i}" style="max-width:220px;max-height:220px;'
                    f'border-radius:8px;display:block;margin:6px 0;" />'
                    for i in images)
                body = imgs + body

        # 气泡统一带 .bubble 类（供 chat.html 归零首尾块级边距）；原文视图再加
        # no-mathjax 类：MathJax 的 ignoreHtmlClass 会跳过它，否则原文里的
        # \(...\) / \[...\] 定界符又会被 MathJax 扫回去渲染一遍。
        classes = "bubble" + (" no-mathjax" if raw else "")
        bubble = (f'<div class="{classes}" style="background:{bubble_bg};'
                  f'color:{text_color};'
                  f'border-radius:{radius};padding:8px 12px;max-width:100%;'
                  f'font-size:{font_size}px;line-height:1.5;white-space:normal;">'
                  f'{tools_html}{think_html}{body}</div>')

        bubble_group = self._bubble_group_html(role, bubble, pal, bubble_bg)

        col = (f'<div style="display:flex;flex-direction:column;'
               f'align-items:{"flex-start" if not reverse else "flex-end"};'
               f'max-width:80%;min-width:0;">'
               f'{bubble_group}{under}</div>')

        row = (f'<div style="display:flex;align-items:flex-start;'
               f'gap:8px;margin:12px 0;'
               f'flex-direction:{"row-reverse" if reverse else "row"};">'
               f'{self._avatar_html(role)}{col}</div>')

        time_html = self._time_chip_html(mtime) if (show_time and mtime) else ""
        return time_html + row

    def _raw_toggle_html(self, msg_index: int, raw: bool) -> str:
        """生成 assistant 气泡下方的「原始/渲染」切换链接。

        raw=True 时当前显示为原始 markdown，链接文案「渲染」可切回渲染视图；
        反之文案「原文」可切到原始视图。href 用 chat:raw:<idx> / chat:md:<idx>
        编码目标，点击后经 acceptNavigationRequest 送到 _on_anchor_clicked。
        """
        pal = _chat_palette()
        if raw:
            target, label = "chat:md:" + str(msg_index), self.tr("渲染", "Render")
        else:
            target, label = "chat:raw:" + str(msg_index), self.tr("原文", "Raw")
        return (f'<a href="{target}" style="color:{pal["meta"]};'
                f'text-decoration:none;">{_html.escape(label)}</a>')

    def _thinking_html(self, msg_index: int | None, thinking: str,
                       font_size: int) -> str:
        """生成助手气泡内可折叠的「思考」块（默认折叠，点击标题展开/关闭）。

        href 用 chat:think:<idx> 编码，点击经 acceptNavigationRequest 转到
        _on_anchor_clicked 翻转 _think_open 状态后原地重放。思考文本按 markdown
        渲染（仅保留正文子集，代码/公式可正常显示）；样式与气泡正文区分：
        左侧浅色竖线 + 略浅的文字色，一眼可辨「思考过程」与「最终回答」。
        """
        pal = _chat_palette()
        opened = msg_index is not None and self._think_open.get(msg_index, False)
        tr = self.tr
        if opened:
            label = tr("收起思考", "Collapse thinking")
        else:
            label = tr("展开思考", "Expand thinking")
        # 折叠时只显示标题行；展开时渲染思考内容并包进带左边线的容器
        if not opened:
            inner = ""
        else:
            inner = (f'<div style="margin-top:4px;padding-left:8px;'
                     f'border-left:2px solid {pal["meta"]};'
                     f'color:{pal["meta"]};'
                     f'font-size:{font_size - 1}px;line-height:1.5;">'
                     f'{self._render_markdown(thinking, font_size - 1)}'
                     f'</div>')
        href = f'chat:think:{msg_index}'
        return (f'<div style="margin:2px 0 6px;font-size:12px;">'
                f'<a href="{href}" style="color:{pal["meta"]};'
                f'text-decoration:none;font-weight:600;">'
                f'{_html.escape(label)}</a>'
                f'{inner}</div>')

    def _tools_html(self, tools: list, font_size: int) -> str:
        """生成助手气泡内「工具活动」状态行列表（智能体模式）。

        tools 为 (name, kind, ok, args_text) 元组列表：kind=call 表示发起调用、
        kind=result 表示已返回。流式期间 call 先出现（pending 态），result 到达
        后合并为完成态；重放时两者成对出现，只渲染完成态行。样式与思考块一致：
        浅色小字，前缀 🔧 / ✓ / ⚠。
        """
        pal = _chat_palette()

        def _open_path(text: str):
            """从写文件工具的结果文本里抽出绝对路径（'已写入：<path>（大小）'）。"""
            if "已写入：" in text:
                seg = text.split("已写入：", 1)[1]
                return seg.split("（", 1)[0].strip()
            return None

        # 合并成对事件：name -> [done?, ok?, args, open_path]
        merged: "dict[str, list]" = {}
        order: "list[str]" = []
        for name, kind, ok, detail in tools:
            if kind == "call":
                if name not in merged:
                    merged[name] = [False, True, detail, None]
                    order.append(name)
            elif kind == "result":
                if name in merged:
                    merged[name][0] = True
                    merged[name][1] = ok
                    merged[name][3] = _open_path(detail)
                else:
                    merged[name] = [True, ok, "", _open_path(detail)]
                    order.append(name)
        rows = []
        for name in order:
            done, ok, args_text, open_path = merged[name]
            icon = ("✓" if ok else "⚠") if done else "…"
            title = _html.escape(name)
            if args_text and len(args_text) > 80:
                args_text = args_text[:80] + "…"
            detail = f' <span style="color:{pal["meta"]};">{_html.escape(args_text)}</span>' if args_text else ""
            row = f'<div style="font-size:{font_size - 1}px;' \
                  f'color:{pal["meta"]};margin:1px 0;">' \
                  f'🔧 {icon} {title}{detail}'
            if open_path:
                import base64 as _b64
                enc = _b64.urlsafe_b64encode(
                    open_path.encode("utf-8")).decode("utf-8").rstrip("=")
                row += (f' <a href="chat:open:{enc}" '
                        f'style="color:{pal["user_avatar"]};text-decoration:underline;">'
                        f'🔗 打开：{_html.escape(_os.path.basename(open_path))}</a>')
            row += "</div>"
            rows.append(row)
        if not rows:
            return ""
        return ('<div style="margin:2px 0 6px;padding-left:8px;'
                f'border-left:2px solid {pal["meta"]};">' + "".join(rows) + "</div>")

    # 子代理类型的友好中文标签（回退原文）
    _AGENT_TYPE_LABELS = {
        "react": ("行动", "Act"),
        "plan": ("规划", "Plan"),
        "reflection": ("反思", "Reflect"),
        "simple": ("简单", "Simple"),
    }

    def _agent_sections_html(self, blocks: list, font_size: int) -> str:
        """把智能体的结构化产出渲染成「分块步骤」HTML（子任务 + 各步骤 + 汇总）。

        blocks 为 gateway_llm.StreamingSuperAgent.blocks 的结构：
        - subtask 块：标题（子任务 N · 类型）+ 内部 steps（每步 = 模型正文 + 思考 +
          工具活动，与步骤放在一起）+ 可选 answer；
        - summary 块：多子任务时 LLM 汇总的最终回答（按普通 markdown 渲染）。
        这是「各步骤的回答和各步骤放在一起」的渲染落点。
        """
        pal = _chat_palette()
        tr = self.tr
        parts: list = []
        # 子任务块总数（排除 summary）。仅多子任务时显示「子任务 N」编号标题；
        # 单子任务（最常见）直接渲染各步骤，避免冗余的「⚙ 子任务 1」前缀。
        subtask_count = sum(1 for b in blocks if b.get("kind") != "summary")
        for idx, b in enumerate(blocks):
            kind = b.get("kind")
            if kind == "summary":
                text = (b.get("text") or "").strip()
                if text:
                    parts.append(self._render_markdown(text, font_size))
                continue
            # ---- 子任务块 ----
            if subtask_count > 1:
                n = b.get("index", idx + 1)
                atype = str(b.get("agent_type", "") or "")
                label = self._AGENT_TYPE_LABELS.get(atype)
                atype_txt = (tr(label[0], label[1]) if label else atype)
                title = f"{tr('子任务', 'Subtask')} {n}"
                if atype_txt:
                    title += f" · {_html.escape(atype_txt)}"
                # 折叠标题行：默认展开（内联 on* 事件在本模块清洗后不会出现，这里只用
                # href 链接固定为展开态，不依赖 JS 交互，保证从头到尾可见）。
                parts.append(
                    f'<div style="margin:10px 0 4px;padding:2px 8px;'
                    f'font-weight:600;color:{pal["user_avatar"]};'
                    f'font-size:{font_size}px;border-left:3px solid '
                    f'{pal["user_avatar"]};">'
                    f'⚙ {_html.escape(title)}</div>')

            steps = b.get("steps") or []
            step_htmls: list = []
            step_text_all: list = []
            for st in steps:
                st_h = self._agent_step_html(st, font_size)
                if not st_h:
                    continue
                step_htmls.append(st_h)
                step_text_all.append(st.get("text", ""))
            # 各步骤：模型正文 + 思考 + 工具活动，与步骤号放在一起
            if step_htmls:
                parts.append(
                    '<div style="padding-left:6px;border-left:2px solid '
                    f'{pal["meta"]};">' + "".join(step_htmls) + "</div>")

            # 子任务的最终答案：与已逐 token 流过 / 收尾补充的结果一致；若已被某步
            # 正文整段覆盖（常见于「无工具」的单步反应），则不重复展示。
            answer = (b.get("answer") or "").strip()
            if answer and answer not in "".join(step_text_all):
                parts.append(
                    '<div style="margin:8px 0 2px;font-weight:600;'
                    f'color:{pal["assistant_text"]};font-size:{font_size}px;">'
                    + _html.escape(tr("✅ 结论", "✅ Conclusion")) + "</div>")
                parts.append(self._render_markdown(answer, font_size))
        return "".join(parts)

    def _agent_step_html(self, st: dict, font_size: int) -> str:
        """把子任务内的单个 ReAct 步骤渲染为「步骤号 + 模型正文 + 思考 + 工具」小单元。"""
        pal = _chat_palette()
        n = st.get("n")
        text = (st.get("text") or "").strip()
        thinking = (st.get("thinking") or "").strip()
        tools = st.get("tools") or []
        if not (text or thinking or tools):
            return ""
        bits: list = []
        label = (self.tr(f"第 {n} 步", f"Step {n}") if n
                 else "")
        if label:
            bits.append(
                f'<div style="font-size:{font_size - 1}px;font-weight:600;'
                f'color:{pal["meta"]};margin:6px 0 2px;">'
                f'{_html.escape(label)}</div>')
        if thinking:
            bits.append(
                f'<div style="color:{pal["meta"]};font-size:{font_size - 1}px;'
                f'margin:2px 0;padding-left:8px;border-left:2px solid '
                f'{pal["meta"]};">{self._render_markdown(thinking, font_size - 1)}</div>')
        if text:
            bits.append(self._render_markdown(text, font_size))
        if tools:
            bits.append(self._tools_html(tools, font_size))
        return "".join(bits)

    @Slot(QUrl)
    def _on_anchor_clicked(self, url):
        """处理气泡内切换链接点击，翻转对应消息的显示格式并重放。"""
        href = url.toString()
        if href.startswith("chat:raw:"):
            self._render_raw[int(href[len("chat:raw:"):])] = True
        elif href.startswith("chat:md:"):
            self._render_raw[int(href[len("chat:md:"):])] = False
        elif href.startswith("chat:think:"):
            idx = int(href[len("chat:think:"):])
            self._think_open[idx] = not self._think_open.get(idx, False)
        elif href.startswith("chat:open:"):
            # 打开智能体产出的文件：href 内是 urlsafe_base64 编码的绝对路径（避免
            # Windows 反斜杠 / 空格在 URL 中歧义）。打开后用系统默认应用即可，不重绘。
            b64 = href[len("chat:open:"):]
            try:
                import base64 as _b64
                pad = "=" * ((4 - len(b64) % 4) % 4)
                raw = _b64.urlsafe_b64decode(b64 + pad)
                path = raw.decode("utf-8")
            except Exception:
                return
            if _os.path.isfile(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        else:
            return
        # 原地重绘：翻转该条显示格式，不应把视图弹到底/改变当前阅读位置
        self._reset_history_view(force=True, stick=False)

    @Slot(object)
    def _on_approval_requested(self, proposal):
        """（主线程）智能体要写文件时弹出确认框；把 Yes/No 写回协调器唤醒工具线程。

        proposal：{action, kind, path, display, size}，由工具线程经 approval_requested
        Signal 跨线程投递。模态弹窗会阻塞主线程事件循环——但工具线程此刻正阻塞在
        Event.wait() 上，两者互不占用对方，不会死锁。
        """
        # SSH 远程执行：proposal 携带 {action:"ssh", host, command}。远端代码执行属高危，
        # 单独弹「允许 SSH 执行？」确认框，明示主机与命令。
        if proposal.get("action") == "ssh":
            title = self.tr("允许 SSH 执行？", "Allow SSH execution?")
            text = self.tr(
                "智能体请求通过 SSH 在远端执行命令：\n\n"
                "主机：{host}\n"
                "命令：{command}\n\n"
                "是否允许执行？",
                "The agent requests to run a command over SSH:\n\n"
                "Host: {host}\nCommand: {command}\n\nAllow it?").format(
                host=proposal.get("host", ""), command=proposal.get("command", ""))
            yes = MessageBox.question(self, title, text) == QMessageBox.Yes
            self._approval.resolve(proposal, yes)
            return

        # 浏览器动作：proposal 携带 {action:"browser", browser_action, params}。会改页面
        # 状态的动作（goto/click/fill 等）需人工确认，明示动作与关键参数。
        if proposal.get("action") == "browser":
            browser_action = proposal.get("browser_action", "")
            params = proposal.get("params") or {}
            detail = _html.escape(_short_repr(params))
            title = self.tr("允许浏览器执行动作？", "Allow browser action?")
            text = self.tr(
                "智能体请求在浏览器中执行动作：\n\n"
                "动作：{action}\n"
                "参数：{params}\n\n"
                "是否允许？",
                "The agent requests a browser action:\n\n"
                "Action: {action}\nParams: {params}\n\nAllow it?").format(
                action=browser_action, params=detail)
            yes = MessageBox.question(self, title, text) == QMessageBox.Yes
            self._approval.resolve(proposal, yes)
            return

        def _human(n: int) -> str:
            if n < 1024:
                return f"{n} 字节"
            if n < 1024 * 1024:
                return f"{n / 1024:.1f} KB"
            return f"{n / 1024 / 1024:.1f} MB"

        kind_map = {"txt": "文本", "docx": "Word 文档", "xlsx": "Excel 表格"}
        kind = kind_map.get(proposal.get("kind", ""), proposal.get("kind", ""))
        size_txt = _human(int(proposal.get("size", 0)))
        title = self.tr("允许写入文件？", "Allow file write?")
        text = self.tr(
            "智能体请求生成以下文件：\n\n"
            "类型：{kind}\n"
            "文件：{display}\n"
            "位置：{path}\n"
            "大小：{size_txt}\n\n"
            "是否允许写入？",
            "The agent requests to create:\n\nType: {kind}\nFile: {display}\n"
            "Location: {path}\nSize: {size_txt}\n\nAllow writing?").format(
            kind=kind, display=proposal.get("display", ""),
            path=proposal.get("path", ""), size_txt=size_txt)
        yes = MessageBox.question(self, title, text) == QMessageBox.Yes
        self._approval.resolve(proposal, yes)

    def _empty_hint_html(self) -> str:
        pal = _chat_palette()
        hint = self.tr("向 AI 提问，开始对话。", "Ask AI something to start.")
        escaped = _html.escape(hint)
        return ('<div style="margin-top:48px;text-align:center;'
                'color:' + pal["meta"] + ';">' + escaped + '</div>')

    @Slot(bool)
    def _on_page_loaded(self, ok: bool) -> None:
        """WebView 页面（含 MathJax）加载完成后标记就绪，并补齐首次内容渲染。

        __init__ 期间页面尚未加载，runJavaScript 里的 __renderChat 可能还未定义，
        于是首次渲染延迟到此刻执行；之后 _push_html 才会真正向页面注入。

        注意：只把 _page_ready 置 True、绝不因 ok=False 置回 False。因为每次
        「原文/渲染」切换都会触发一次被 acceptNavigationRequest 取消的
        ?__lightaibox__= 导航，Qt 会随之补发 loadFinished(False)；若据此刻回
        _page_ready，会让下一次切换的 _push_html 被拦截、视图停在原状态。
        （页面只会加载一次 chat.html，之后没有需要重新等待就绪的真实导航。）
        """
        if ok:
            self._page_ready = True
            self._reset_history_view(force=True)

    def _reset_history_view(self, force: bool = False, stick: bool = True,
                            force_pin: bool = False) -> None:
        """重建整段气泡 HTML 并注入 WebView（含流式中的助手回复与底部提示）。

        流式生成期间（worker 存活）走防抖合并，避免每个 token 都触发一次
        MathJax 全量重排；用户主动动作（发送 / 清空 / 字号 / 主题 / 切换原文）
        传 force=True 立即生效。页面未加载完成前的调用会被 _push_html 拦截，
        由 _on_page_loaded 负责在就绪后补齐。
        stick=False 保持原滚动位置（换肤、原文切换等原地重绘）；
        stick=True 追加渲染，是否贴底由页面按用户滚动位置决定；
        force_pin=True 强制贴底（用户主动发送时）。
        """
        parts: list = []
        last_ts: float | None = None
        for i, m in enumerate(self.session.messages):
            role = m.get("role", _ROLE_USER)
            content = m.get("content", "")
            text, images = self._split_content(content)
            if text or images:
                # 助手消息携带其统计（存于消息内嵌字段，若存在）；索引用于切换链接
                meta = m.get("_meta", "")
                thinking = m.get("_thinking", "")
                ts = m.get("time")
                # 首条消息或与上一条间隔 >5 分钟时显示时间分隔条
                show_time = (ts is not None and (
                    last_ts is None or ts - last_ts > 300))
                if ts is not None:
                    last_ts = ts
                parts.append(self._render_block(
                    role, text, meta, self._font_size, i,
                    show_time=show_time, mtime=ts, thinking=thinking,
                    images=images, tools=m.get("_tools"),
                    agent_blocks=m.get("_agent_blocks")))
        if self._pending_assistant:
            # 流式中：整条视为「新的一段时间」，仅在最新一条之后显示时间
            now = _time.time()
            show_time = not parts or (last_ts is None or now - last_ts > 300)
            parts.append(self._render_streaming_bubble(show_time=show_time, mtime=now))
        if not parts:
            parts.append(self._empty_hint_html())
        html = "".join(parts) + self._notice_html

        if force or self._worker is None:
            self._pending_html = None
            self._flush_timer.stop()
            self._push_html(html, stick=stick, force_pin=force_pin)
        else:
            self._pending_html = html
            self._pending_stick = stick
            self._pending_force_pin = force_pin
            self._flush_timer.start()

    def _render_streaming_bubble(self, show_time: bool = False,
                                 mtime: float | None = None) -> str:
        """构建「流式中最后一条助手气泡」的 HTML（不含标题/历史，仅该气泡）。

        增量流式只重建这条气泡；确认的历史消息已渲染在 DOM 里、不再参与每次刷盘。
        """
        text = "".join(self._pending_assistant)
        thinking = "".join(self._pending_thinking)
        tools = list(self._pending_tools) or None
        now = mtime if mtime is not None else _time.time()
        show_time = show_time or not (len(self.session.messages) > 0)
        # 智能体模式结构化流式：步骤按子任务/步骤分组随流渲染（每 token 都刷新），
        # 不混入正文；正文仍由 _pending_assistant 保留为原文（原文/渲染切换用）。
        if self._structured:
            blocks = self._agent_live_blocks()
            if not blocks:
                return ""
            return self._render_block(
                _ROLE_ASSISTANT, text,
                font_size=self._font_size, show_time=show_time, mtime=now,
                thinking=thinking, tools=tools,
                agent_blocks=blocks)
        # 正文为空但有思考/工具活动时也要渲染（智能体工具循环阶段能看到状态行）
        if not (text or thinking or tools):
            return ""
        return self._render_block(
            _ROLE_ASSISTANT, text,
            font_size=self._font_size, show_time=show_time, mtime=now,
            thinking=thinking, tools=tools)

    def _refresh_streaming(self, show_cursor: bool = True) -> None:
        """流式增量：只重建并刷入最后一条进行中气泡（受防抖合并）。

        仅当 worker 存在（真在流式）时合并刷盘；worker 已停则直接即时推流式节点
        （罕见，作为兜底）。
        """
        html = self._render_streaming_bubble()
        if not html:
            return
        if self._worker is None:
            self._pending_stream_html = None
            self._flush_timer.stop()
            self._push_streaming(html, show_cursor)
            return
        self._pending_stream_html = html
        self._pending_stream_cursor = show_cursor
        # 自适应合并窗：token 快速到达（gaps 小）时放宽到 ~180ms 批量合并，
        # 慢节奏（思考/工具等待的静默缝隙）时收紧到 ~90ms，让画面尽快跟上。
        now = _time.monotonic()
        if self._last_token_ts is not None:
            gap = now - self._last_token_ts
            self._gap_ema = gap if self._gap_ema is None \
                else 0.8 * self._gap_ema + 0.2 * gap
        self._last_token_ts = now
        ema = self._gap_ema if self._gap_ema is not None else 0.03
        interval = int(max(90, min(200, 120 + (0.020 - ema) * 4000)))
        if self._flush_timer.interval() != interval:
            self._flush_timer.setInterval(interval)
        self._flush_timer.start()

    def _push_streaming(self, html: str, show_cursor: bool = True) -> None:
        """经 window.__appendStreaming 只更新流式气泡节点（仅对该节点重排公式/图）。"""
        if not self._page_ready:
            return
        mermaid_theme = ("dark" if ThemeManager().current() == ThemeManager.DARK
                         else "default")
        script = "window.__appendStreaming(%s, %s, %s);" % (
            _json.dumps(html), _json.dumps(mermaid_theme),
            "true" if show_cursor else "false")
        self.view.page().runJavaScript(script)

    def _view_stop_streaming(self) -> None:
        """请求页面移除增量流式节点与光标（结束/停止/清空时调用）。"""
        if not self._page_ready:
            return
        self.view.page().runJavaScript("window.__streamStop && window.__streamStop();")

    def _flush_stream(self) -> None:
        """防抖定时器触发：优先刷增量流式节点，否则刷整段历史。"""
        if self._pending_stream_html is not None:
            self._push_streaming(self._pending_stream_html,
                                 self._pending_stream_cursor)
            self._pending_stream_html = None
            return
        if self._pending_html is None:
            return
        self._push_html(self._pending_html,
                        stick=getattr(self, "_pending_stick", True),
                        force_pin=getattr(self, "_pending_force_pin", False))
        self._pending_html = None

    def _push_html(self, html: str, stick: bool = True, force_pin: bool = False) -> None:
        """经 window.__renderChat 把消息 HTML 注入 WebView 页面。

        页面未加载完成时（__renderChat 可能未定义）跳过注入，由 loadFinished
        后的 _on_page_loaded 重放当前视图补齐，避免提前 runJavaScript 抛错导致
        首屏气泡 / LaTeX 公式不显示。页面底色已透明，不再传 pageBg。
        stick=True 追加渲染：是否贴底由页面按用户滚动是否已在底部决定（避免流式
        把已上翻的视图拽回底部）；False 保持原滚动位置（原地重绘用）。
        force_pin=True 强制贴底并复位跟贴（用户主动发送新消息时）。
        """
        if not self._page_ready:
            return
        # mermaid 主题跟随当前界面主题：亮色 'default'、暗色 'dark'，让 SVG 配色一致
        mermaid_theme = ("dark" if ThemeManager().current() == ThemeManager.DARK
                         else "default")
        script = "window.__renderChat(%s, null, %s, %s, %s);" % (
            _json.dumps(html), "true" if stick else "false",
            _json.dumps(mermaid_theme), "true" if force_pin else "false")
        self.view.page().runJavaScript(script)

    def _scroll_to_bottom(self) -> None:
        self.view.page().runJavaScript(
            "window.scrollTo(0, document.body.scrollHeight);")

    def apply_theme_colors(self) -> None:
        """把 WebView 视图级底色设为透明，让页面透出跟随 QSS 的父容器底色。

        这是「对话区换肤滞后」的根因修复：原先这里按主题铺实色（#161B26/#FFFFFF），
        切主题时要等 JS(__renderChat) 重涂才变、且与外层 QSS 之间易出现上下分色。
        改为透明后，外层 apply_theme(QSS) 一换、本区域即时显新底色，无需 JS 参与。
        setBackgroundColor(Qt.transparent) 是 Qt 支持的稳定组合（DOM 透明时透出
        QWidget 父色，实测无白闪）。保留此方法名以兼容 main_window 的切换调用。
        """
        self.view.page().setBackgroundColor(Qt.GlobalColor.transparent)

    # ------------------------------------------------------------------ #
    # provider 选择
    # ------------------------------------------------------------------ #
    def _provider_id(self):
        data = self.provider_combo.currentData()
        return None if data is None else int(data)

    def refresh_providers(self):
        """重建 provider 下拉：Auto(自适应) + 所有已启用且未超配额的 provider。

        由构造时、语言切换、以及 1.2s 轮询定时器调用。provider 可用集合未变化时
        直接跳过，避免高频轮询反复 clear() 下拉框，把用户正在展开的下拉强制关闭。
        """
        providers = [p for p in self.gateway.providers.list() if p.is_available()]
        sig = tuple((p.id, p.name, p.model) for p in providers)
        if sig == self._provider_sig:
            return
        self._provider_sig = sig
        previous = self.provider_combo.currentData()
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        self.provider_combo.addItem(
            self.tr("自动（自适应调度）", "Auto (adaptive)"), None)
        for p in providers:
            label = f"{p.name} · {p.model}" if p.model else p.name
            self.provider_combo.addItem(label, p.id)
        idx = self.provider_combo.findData(previous)
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.provider_combo.blockSignals(False)

    def _on_provider_changed(self):
        pass  # 选择在发送时生效

    def _update_think_btn_text(self):
        """思考按钮文案随状态/语言切换：开启→「思考：开」，关闭→「思考：关」。"""
        if self._thinking_on:
            self.think_btn.setText(self.tr("思考：开", "Thinking: on"))
        else:
            self.think_btn.setText(self.tr("思考：关", "Thinking: off"))
        self.think_btn.setToolTip(self.tr(
            "开启后，模型会在回答前先给出思考过程（支持折叠展开）",
            "When on, the model shows its reasoning before the answer"))

    def _apply_input_font(self):
        """把当前字号应用到输入框，使其与气泡字号保持一致（统一用像素 px）。"""
        font = QFont(self.input.font())
        font.setPixelSize(self._font_size)
        self.input.setFont(font)

    def _apply_sidebar_font(self):
        """把当前字号应用到会话列表侧边栏，与气泡/输入框观感一致（统一 px）。"""
        font = QFont(self.sess_list.font())
        font.setPixelSize(self._font_size)
        self.sess_list.setFont(font)

    def _on_font_changed(self):
        """字号切换：持久化，应用到输入框/侧边栏并重放气泡。"""
        data = self.font_combo.currentData()
        if data is None:
            return
        self._font_size = int(data)
        self._settings.setValue("chat/font_size", self._font_size)
        self._apply_input_font()
        self._apply_sidebar_font()
        self._reset_history_view(force=True)

    def _on_toggle_thinking(self):
        """思考模式开关：持久化偏好并刷新按钮文案。"""
        self._thinking_on = self.think_btn.isChecked()
        self._settings.setValue("chat/thinking", self._thinking_on)
        self._update_think_btn_text()

    def _on_toggle_agent(self):
        """智能体模式开关：持久化偏好并刷新按钮文案。"""
        self._agent_on = self.agent_btn.isChecked()
        self._settings.setValue("chat/agent", self._agent_on)
        self._update_agent_btn_text()

    def _update_agent_btn_text(self):
        """智能体按钮文案随状态/语言切换。"""
        if self._agent_on:
            self.agent_btn.setText(self.tr("智能体：开", "Agent: on"))
        else:
            self.agent_btn.setText(self.tr("智能体：关", "Agent: off"))

    # ------------------------------------------------------------------ #
    # 发送 / 流式接收
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_send(self):
        text = self.input.toPlainText().strip()
        images = self._pending_images
        self._pending_images = []
        if (not text and not images) or self._worker is not None:
            return
        pid = self._provider_id()
        self.session.add_user(text, images=images if images else None)
        # 用户消息写透（seq 对其 DB 列；meta 暂无）
        if self.session.id is not None:
            self._store.add_message(
                self.session.id, self.session._seq, "user",
                self.session.messages[-1]["content"], {})
        self.input.clear()

        # 渲染：重建整段（含新增用户气泡），立即生效。用户主动发送：强制贴底
        # 并复位跟贴（force_pin），避免此前上翻状态导致新消息也停在旧位置。
        self._pending_assistant = []
        self._pending_thinking = []
        self._pending_tools = []
        self._last_stats = ""
        self._notice_html = ""
        self._reset_history_view(force=True, force_pin=True)

        self._set_busy(True)
        # 走上下文构建：摘要(若有) + 预算内最近尾部（自动裁剪最旧），而非全量历史
        self._worker = _ChatWorker(
            self.gateway, self._build_context_messages(),
            provider_id=pid, thinking_enabled=self._thinking_on,
            agent_mode=self._agent_on, approval=self._approval,
            agent_session=self._ensure_agent_session())
        self._worker.token.connect(self._on_token)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _ensure_agent_session(self):
        """返回智能体模式的跨轮复用会话（Fix 3）。

        会话 id 变化（新建/切换/删除）时重建，使新对话从干净记忆开始；同一对话内
        （agent 模式）沿多轮共享同一实例，令工具激活状态与跨轮历史得以保持。
        """
        if not self._agent_on:
            return None
        sid = self.session.id
        if (self._agent_session is None
                or self._agent_session_sid != sid):
            from ..agent_bridge import AgentChatSession
            self._agent_session = AgentChatSession(
                self.gateway, provider_id=self._provider_id(),
                approval=self._approval)
            self._agent_session_sid = sid
        return self._agent_session

    @Slot()
    def _on_add_image(self):
        """选择本地图片，读成 base64 data URL 暂存，随下一条消息一并发送。

        选中的图片仅存于内存（_pending_images），不立即发送；用户可继续输入文字，
        点「发送」后图片与文字一起作为一条多模态消息发出。发送或清空后即时清空。
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self, self.tr("选择图片", "Select images"), "",
            self.tr("图片文件 (*.png *.jpg *.jpeg *.gif *.webp)", "Images (*.png *.jpg *.jpeg *.gif *.webp)"))
        if not paths:
            return
        for path in paths:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                # 依据扩展名推断 MIME（data URL 需要），缺省按 png
                ext = _os.path.splitext(path)[1].lower().lstrip(".")
                mime = {
                    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "webp": "image/webp",
                }.get(ext, "image/png")
                url = f"data:{mime};base64," + _base64.b64encode(raw).decode("ascii")
                self._pending_images.append(url)
            except OSError:
                continue
        self._update_image_btn()

    def _update_image_btn(self):
        """图片按钮文案反映当前待发送图片数量。"""
        n = len(self._pending_images)
        self.image_btn.setText(
            self.tr(f"🖼 图片 ({n})", f"🖼 Image ({n})") if n
            else self.tr("🖼 图片", "🖼 Image"))

    @Slot(str)
    def _on_token(self, piece: str):
        # 统一消费一段增量：拆分带外 token（think/tool/sub/step/sans），普通文本
        # 归入当前步骤 / 子任务 / 根汇总。思考、工具、结构化边界都在这一层被剥离，
        # 不混入助手正文（正文只保留各步骤/汇总的模型文本）。
        self._consume_agent_piece(piece)
        # 增量流式：只重建并刷入最后一条进行中气泡（历史已渲染，不再每次全量重排）。
        # 结束时（on_finished）再走整段 _reset_history_view 固化全部。
        self._refresh_streaming()

    def _consume_agent_piece(self, piece: str) -> None:
        """把一段流式增量切成 token 与文本，分发给结构化步骤状态或既有累积器。

        所有带外 token 都是 yield 侧整段给出的（不跨 piece），故按 piece 切分安全。
        文本块一律进 _pending_assistant（作为消息原文 / 原文切换视图），同时按当前
        开启的步骤 → 子任务 → 根汇总归属到结构化状态。
        """
        for seg in _AGENT_TOKEN_RE.split(piece):
            if not seg:
                continue
            if seg[0] == "\x01":
                self._apply_structure_token(seg)
            else:
                self._route_text(seg)

    def _route_text(self, seg: str) -> None:
        """一段普通模型文本：进原文累积，并按当前结构归属。"""
        self._pending_assistant.append(seg)
        if self._cur_step is not None:
            self._cur_step["text"].append(seg)
        elif self._cur_sub is not None:
            self._cur_sub["text"].append(seg)
        else:
            self._root.append(seg)

    def _apply_structure_token(self, seg: str) -> None:
        """处理一个带外结构 token（sub/step/sans /sub、think、tool）。"""
        if seg.startswith(SUB_OPEN):
            data = self._token_json(seg[len(SUB_OPEN):-1])
            # 新子任务：把上一个子任务收尾落入已完成块，开启当前块
            self._flush_current_sub()
            self._structured = True
            self._cur_sub = {
                "kind": "subtask",
                "index": int((data or {}).get("index", 0) or 0),
                "agent_type": str((data or {}).get("agent_type", "") or ""),
                "steps": [], "answer": "", "text": [], "tools": [],
            }
        elif seg.startswith(STEP_OPEN):
            data = self._token_json(seg[len(STEP_OPEN):-1])
            # 新一步：把上一步收尾，开启当前步（其正文/思考/工具都归这一步）
            self._flush_current_step()
            self._cur_step = {
                "n": int((data or {}).get("n", 0) or 0),
                "text": [], "thinking": [], "tools": [],
            }
        elif seg.startswith(SUB_ANSWER):
            data = self._token_json(seg[len(SUB_ANSWER):-1])
            if self._cur_sub is not None:
                self._cur_sub["answer"] = str((data or {}).get("answer", "") or "")
        elif seg.startswith(SUB_END):
            self._flush_current_sub()
        elif seg.startswith(THINK_TAG):
            inner = seg[len(THINK_TAG):]
            think = inner[:-len(THINK_END_TAG)] if inner.endswith(THINK_END_TAG) else inner
            if think:
                if self._cur_step is not None:
                    self._cur_step["thinking"].append(think)
                elif self._cur_sub is not None:
                    self._cur_sub["text"].append(think)
                else:
                    self._pending_thinking.append(think)
        elif seg.startswith(TOOL_TAG):
            inner = seg[len(TOOL_TAG):]
            evt = inner[:-len(TOOL_END_TAG)] if inner.endswith(TOOL_END_TAG) else inner
            try:
                data = _json.loads(evt)
                # call 事件第四位存参数摘要；result 事件存返回文本（含产出路径，
                # 供 _tools_html 渲染「🔗 打开」链接）。
                if data.get("kind") == "call":
                    detail = str(data.get("args", "") or "")
                else:
                    detail = str(data.get("text", "") or "")
                tup = (str(data.get("name", "?")), str(data.get("kind", "")),
                       bool(data.get("ok", True)), detail)
                # 顶层也记录（作为非结构化回退 / 消息级 _tools 备份）
                self._pending_tools.append(tup)
                if self._cur_step is not None:
                    self._cur_step["tools"].append(tup)
                elif self._cur_sub is not None:
                    self._cur_sub["text"].append("")  # 占位避免空；下面存 tools
                    self._cur_sub["tools"].append(tup)
            except Exception:
                pass

    @staticmethod
    def _token_json(payload: str):
        """解析 token 负载 JSON；解析失败返回 None（整段丢弃）。"""
        try:
            return _json.loads(payload)
        except Exception:
            return None

    def _flush_current_step(self) -> None:
        """收尾当前步骤（有内容且处于某子任务内则落入该子任务的 steps）。"""
        if self._cur_step is not None and self._cur_sub is not None:
            st = self._cur_step
            if st["text"] or st["thinking"] or st["tools"]:
                self._cur_sub["steps"].append({
                    "n": st["n"],
                    "text": "".join(st["text"]),
                    "thinking": "".join(st["thinking"]).strip(),
                    "tools": list(st["tools"]),
                })
        self._cur_step = None

    def _flush_current_sub(self) -> None:
        """收尾当前子任务：把步骤与新出现的前导文本落入块并加入已完成列表。"""
        sub = self._cur_sub
        if sub is None:
            self._cur_step = None
            return
        self._flush_current_step()
        # 子任务正文若早于首个步骤出现（罕见），折叠成一步
        if not sub["steps"]:
            lead = "".join(sub["text"]).strip()
            tools = sub.get("tools") or []
            if lead or tools:
                sub["steps"] = [{"n": 0, "text": lead,
                                 "thinking": "", "tools": list(tools)}]
        sub.pop("text", None)
        sub.pop("tools", None)
        self._pending_blocks.append(sub)
        self._cur_sub = None
        self._cur_step = None

    def _agent_live_blocks(self) -> list:
        """返回当前用于流式渲染的可见块（含正在进行的子任务 / 根汇总，不破坏状态）。"""
        blocks = list(self._pending_blocks)
        if self._cur_sub is not None:
            steps = list(self._cur_sub["steps"])
            if self._cur_step is not None and (
                    self._cur_step["text"] or self._cur_step["thinking"]
                    or self._cur_step["tools"]):
                steps = steps + [{
                    "n": self._cur_step["n"],
                    "text": "".join(self._cur_step["text"]),
                    "thinking": "".join(self._cur_step["thinking"]).strip(),
                    "tools": list(self._cur_step["tools"]),
                }]
            # 尚未出现任何步骤时（如 simple 智能体无 STEP 事件直接流文本），把
            # 子任务级待定正文/工具折叠成第 0 步，直播期间也能看到内容。
            if not steps:
                lead = "".join(self._cur_sub["text"]).strip()
                sub_tools = [t for t in self._cur_sub.get("tools", [])
                             if t not in (self._cur_step or {}).get("tools", [])]
                if lead or sub_tools:
                    steps = [{"n": 0, "text": lead, "thinking": "",
                              "tools": list(sub_tools)}]
            live = dict(self._cur_sub)
            live["steps"] = steps
            live.pop("text", None)
            live.pop("tools", None)
            blocks.append(live)
        root = "".join(self._root).strip()
        if root:
            blocks.append({"kind": "summary", "text": root})
        return blocks

    def _finalize_agent_blocks(self) -> list:
        """流式结束：收尾当前子任务，返回完整块列表（含根汇总）并清空状态。"""
        self._flush_current_sub()
        blocks = list(self._pending_blocks)
        root = "".join(self._root).strip()
        if root:
            blocks.append({"kind": "summary", "text": root})
        return blocks

    @Slot(object)
    def _on_finished(self, result):
        full = "".join(self._pending_assistant)
        if result is not None:
            # 智能体模式（多次底层调用）展示调用次数与提示/输出拆分，使总 token
            # 数值可解释、可在「调用记录」页逐笔核对；普通模式维持单一总数。
            if getattr(result, "calls", 0) > 1 or self._structured:
                en = (f"{result.calls} calls · "
                      f"prompt {result.prompt_tokens:,} + output "
                      f"{result.completion_tokens:,} · "
                      f"{result.elapsed_ms / 1000:.1f}s")
                zh = (f"{result.calls} 次调用 · "
                      f"提示 {result.prompt_tokens:,} + 输出 "
                      f"{result.completion_tokens:,} · "
                      f"{result.elapsed_ms / 1000:.1f}s")
            else:
                en = (f"{result.total_tokens:,} tokens · "
                      f"{result.elapsed_ms / 1000:.1f}s")
                zh = (f"{result.total_tokens:,} tokens · "
                      f"{result.elapsed_ms / 1000:.1f}s")
            self._last_stats = self.tr(zh, en)
        if full:
            # 将统计与思考内容内嵌到消息，供 _reset_history_view 重放时显示
            self.session.add_assistant(full)
            self.session.messages[-1]["_meta"] = self._last_stats
        if self._pending_thinking:
            thinking = "".join(self._pending_thinking).strip()
            if thinking:
                # 思考内容存到当前（或上一条）assistant 消息的 _thinking 字段
                self.session.messages[-1]["_thinking"] = thinking
        # 智能体模式：把本轮工具活动存进这条助手消息，重放时仍可展示
        if self._pending_tools:
            self.session.messages[-1]["_tools"] = list(self._pending_tools)
        # 智能体模式：结构化步骤（子任务 → 各步骤 → 文本/工具）持久化到消息，供
        # _reset_history_view 分块渲染；有结构时正文以步骤视图展示，消息级
        # thinking/tools 已并入步骤块、不再重复渲染。优先用流式期间就地构建的块
        #（与直播渲染分块一致），无流式结构时回退到 result.agent_blocks。
        blocks = self._finalize_agent_blocks() if self._structured else []
        if result is not None and getattr(result, "agent_blocks", None):
            blocks = blocks or list(result.agent_blocks)
        if blocks:
            self.session.messages[-1]["_agent_blocks"] = list(blocks)
        # 助手消息写透（含统计/思考/工具/结构化块 meta）；更新会话活跃时间与侧栏排序
        if self.session.id is not None:
            last = self.session.messages[-1]
            meta = {
                k: last[k] for k in ("_meta", "_thinking", "_tools", "_agent_blocks")
                if k in last
            }
            self._store.add_message(
                self.session.id, self.session._seq, "assistant",
                last["content"], meta)
            self._store.touch(self.session.id)
            self.refresh_sidebar()
        self._cleanup_worker()
        self._reset_history_view()
        self._scroll_to_bottom()

    @Slot(str)
    def _on_failed(self, err: str):
        pal = _chat_palette()
        self._notice_html = (
            f'<div style="margin:8px 0;color:{pal["error"]};'
            f'font-size:13px;">{_html.escape(self.tr("⚠ 调用失败：", "⚠ Error: ") + err)}</div>')
        self._cleanup_worker()
        self._reset_history_view(force=True)

    def _cleanup_worker(self):
        w = self._worker
        self._worker = None
        self._pending_assistant = []
        self._pending_thinking = []
        self._pending_tools = []
        # 重置智能体流式结构状态（下轮从干净状态开始）
        self._pending_blocks = []
        self._cur_sub = None
        self._cur_step = None
        self._root = []
        self._structured = False
        self._set_busy(False)
        # 流式结束/停止：移除页面里的增量流式节点与光标，避免残留。
        self._pending_stream_html = None
        self._view_stop_streaming()
        if w is not None and w.isRunning():
            # 线程仍在运行（点了「停止/清空」）。不能直接丢引用——
            # 否则运行中的 QThread 被 GC 销毁时会触发
            # "QThread: Destroyed while thread is still running" 导致崩溃。
            self._reap_worker(w)

    def _reap_worker(self, w: _ChatWorker) -> None:
        """把仍在线程中运行的 worker 挂到回收队列，由定时器轮询等它结束。"""
        w.stop()
        w.token.disconnect()
        w.finished.disconnect()
        w.failed.disconnect()
        self._zombie_workers.append(w)
        self._zombie_timer.start()

    @Slot()
    def _on_zombie_tick(self) -> None:
        stayed = []
        for w in self._zombie_workers:
            if w.isFinished():
                w.deleteLater()   # 线程已收尾，安全销毁
            else:
                stayed.append(w)
        self._zombie_workers = stayed
        if not stayed:
            self._zombie_timer.stop()

    def shutdown(self) -> None:
        """进程/窗口退出前阻塞式回收所有生成线程，防止运行中线程随对象销毁崩溃。

        Qt 不允许销毁仍在运行的 QThread（会触发 abort）。这里的 wait() 会等到
        网络流返回/报错为止，确保线程真正结束后再销毁对象。
        """
        for w in ([self._worker] if self._worker else []) + self._zombie_workers:
            if w is None:
                continue
            w.stop()
            if w.isRunning():
                w.wait()
            w.deleteLater()
        self._worker = None
        self._zombie_workers = []
        self._zombie_timer.stop()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    @Slot()
    def _on_stop(self):
        if self._worker is not None:
            self._worker.stop()
            self._cleanup_worker()
            pal = _chat_palette()
            self._notice_html = (
                f'<div style="margin:8px 0;color:{pal["meta"]};'
                f'font-size:13px;">{_html.escape(self.tr("（已停止）", " (stopped)"))}</div>')
            self._reset_history_view(force=True)

    @Slot()
    def _on_clear(self):
        if self._worker is not None:
            self._worker.stop()
            self._cleanup_worker()
        self.session.clear()
        # 清空上下文：同时丢弃智能体的跨轮记忆（Fix 3），下次发送从干净状态开始
        self._agent_session = None
        self._agent_session_sid = None
        # 同步清掉库里该会话的消息与摘要（展示与库保持一致）
        if self.session.id is not None:
            self._store.delete_range(self.session.id, 0)
            self._store.set_summary(self.session.id, "")
        self._pending_assistant = []
        self._pending_thinking = []
        self._pending_tools = []
        self._pending_images = []
        self._think_open = {}
        self._notice_html = ""
        self._update_image_btn()
        self._reset_history_view(force=True)

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.send_btn.setText(self.tr("发送", "Send") if not busy
                              else self.tr("生成中…", "Generating…"))
        self.stop_btn.setEnabled(busy)
        self.clear_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)

    # ------------------------------------------------------------------ #
    # 语言切换
    # ------------------------------------------------------------------ #
    def retranslate(self):
        self.header.title_label.setText(self.tr("对话", "Chat"))
        self.provider_label.setText(self.tr("模型", "Model"))
        self.font_label.setText(self.tr("字号", "Size"))
        # 字号档位文案随语言切换：重设 大/中/小（英文 S/M/L）的项文本
        for i, (zh, en, _size) in enumerate(self._FONT_PRESETS):
            if i < self.font_combo.count():
                self.font_combo.setItemText(i, self.tr(zh, en))
        # Auto 项文案随语言刷新：签名去重会因 provider 集合未变而跳过，故先
        # 重置签名强制重建，刷新首项「自动（自适应调度）」的新语言文案。
        self._provider_sig = None
        self.refresh_providers()
        self.input.setPlaceholderText(self.tr("输入消息…", "Type a message…"))
        self.hint_label.setText(self.tr("Enter 换行 · Ctrl+Enter 发送",
                                        "Enter new line · Ctrl+Enter send"))
        self.send_btn.setText(self.tr("发送", "Send")
                              if self._worker is None else self.tr("生成中…", "Generating…"))
        self.stop_btn.setText(self.tr("停止", "Stop"))
        self.clear_btn.setText(self.tr("清空", "Clear"))
        self._update_think_btn_text()
        self._update_agent_btn_text()
        self._update_image_btn()
        # 重放气泡（角色名 / 统计文案随语言/换肤刷新）。属原地重绘，保持滚动位置，
        # 不再每次切主题都把对话弹到最底部。
        self._reset_history_view(stick=False)