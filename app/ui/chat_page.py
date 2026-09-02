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
import html as _html
import json as _json
import os as _os
import re as _re
import time as _time
from datetime import datetime as _datetime

import markdown as _md

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..gateway import Gateway
from ..chat_session import ChatSession
from .i18n import LanguageManager
from .theme_manager import ThemeManager
from .widgets import SectionHeader

_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"


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
            "assistant_bubble": "#F5F6F8",  # 浅灰底
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


class _ChatWorker(QThread):
    """独立线程消费 Gateway.chat_stream，把增量经信号送回主线程。

    Gateway.chat_stream 在 provider 层是阻塞网络请求，不能放进主线程；
    QThread（run 里同步迭代生成器）是 PySide6 下最直接的隔离方式。
    """

    token = Signal(str)
    finished = Signal(object)  # ClientResult
    failed = Signal(str)

    def __init__(self, gateway: Gateway, messages: list,
                 provider_id=None, parent=None):
        super().__init__(parent)
        self._gateway = gateway
        self._messages = messages
        self._provider_id = provider_id
        self._stop = False

    def run(self) -> None:
        try:
            it = self._gateway.chat_stream(
                self._messages, provider_id=self._provider_id)
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
        self.session = ChatSession()

        # 聊天字体大小（QSettings 持久化，默认 13px）
        self._settings = QSettings("LightAIBox", "LightAIBox")
        self._font_size = self._settings.value("chat/font_size", 13, type=int)

        # 后台生成线程；_pending_assistant 累积当前这条助手回复的文本。
        self._worker: _ChatWorker | None = None
        # 被停止但仍未结束的线程会挂这里回收，避免运行中的 QThread 被 GC 销毁崩溃。
        self._zombie_workers: list = []
        self._zombie_timer = QTimer(self)
        self._zombie_timer.setInterval(100)
        self._zombie_timer.timeout.connect(self._on_zombie_tick)
        self._pending_assistant: list = []
        self._last_stats: str = ""  # 上次助手回复的 token 统计（气泡内显示）
        # 每个助手消息的显示格式：msg_index -> 是否显示原始 markdown（默认渲染）
        self._render_raw: dict = {}
        # 底部瞬时提示（调用失败 / 已停止），随消息流一起注入视图底部
        self._notice_html: str = ""
        # WebView 页面（含 MathJax）是否已加载完成；加载前不注入，避免
        # __renderChat 尚未定义时 runJavaScript 抛错导致首屏内容/公式丢失。
        self._page_ready = False
        # 助手头像：打包 logo.png 读成 data URI（圆形裁切展示），避免每次渲染重读盘。

        self._build_ui()
        self.refresh_providers()
        # 首次内容渲染改由 _on_page_loaded（loadFinished）触发，确保注入的
        # HTML/MathJax 在已就绪的页面里被扫描；此处不再提前 push。
        # self._reset_history_view()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # 顶行：标题（左）+ provider 选择（右），底部细分隔线
        self.header = SectionHeader(self.tr("对话", "Chat"))
        self.provider_label = QLabel(self.tr("模型", "Model"))
        self.provider_label.setProperty("class", "stats-text")
        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(200)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        # 聊天字体大小选择（12~18px，持久化到 QSettings）
        self.font_label = QLabel(self.tr("字号", "Size"))
        self.font_label.setProperty("class", "stats-text")
        self.font_combo = QComboBox()
        self.font_combo.setMinimumWidth(72)
        for size in (12, 13, 14, 15, 16, 18):
            self.font_combo.addItem(str(size) + "px", size)
        idx = self.font_combo.findData(self._font_size)
        self.font_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.font_combo.currentIndexChanged.connect(self._on_font_changed)

        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(12)
        tl.addWidget(self.header.title_label, 1)
        tl.addWidget(self.provider_label)
        tl.addWidget(self.provider_combo)
        tl.addWidget(self.font_label)
        tl.addWidget(self.font_combo)
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
        # 关键：setUrl 之前先把 Chromium 视图级底色设成当前主题 page_bg。
        # QWebEngineView 在首次可见时才创建渲染主机并提交首帧；若此时仍用默认
        # 白底，切到「对话」Tab 的瞬间会先闪一下白、等 JS 上色后才回主题色。
        # 视图级背景由合成器直接铺底，早于任何 DOM/CSS，首帧即主题色。
        self.apply_theme_colors()
        # 页面加载完成（脚本 / MathJax 就绪）后再注入首屏内容，见 _on_page_loaded
        self.view.loadFinished.connect(self._on_page_loaded)
        self.view.setUrl(QUrl.fromLocalFile(self._chat_html_path))
        root.addWidget(self.view, 1)

        # 流式刷盘合并：生成期间不要每个 token 都让 MathJax 全量重排
        self._pending_html: str | None = None
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
        iv.addWidget(self.input)

        foot = QHBoxLayout()
        foot.setSpacing(8)
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

        # 输入框内 Ctrl+Enter 发送
        self.input.installEventFilter(self)

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
        # 去掉 <pre><code>（MathJax 的 skipHtmlTags 会跳过它们），解 HTML 转义后
        # 用 \[...\] 显式定界，交给 MathJax 排版成居中式 display math。
        body = _re.sub(
            r'<pre><code class="language-(?:latex|tex|math)">(.*?)</code></pre>',
            lambda m: ('<div style="text-align:center;margin:8px 0;">\\['
                       + _html.unescape(m.group(1)).strip()
                       + '\\]</div>'),
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

    def _render_block(self, role: str, text: str, meta: str = "",
                      font_size: int = 13, msg_index: int | None = None,
                      show_time: bool = False, mtime: float | None = None) -> str:
        """把一条消息渲染为微信式气泡（圆形头像 + 带头尾气泡）。

        布局（flex）：assistant 头像在左、气泡在右；user 头像在右、气泡在左。
        两端不再用 <table align> 浮动。assistant 气泡下方带 token 统计与
        「原始/渲染」切换链接；show_time=True 时上方插入居中时间条。
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
        if role == _ROLE_ASSISTANT:
            raw = msg_index is not None and self._render_raw.get(msg_index, False)
            body = (_html.escape(text).replace("\n", "<br>") if raw
                    else self._render_markdown(text, font_size))
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

        # 原文视图给气泡加 no-mathjax 类：MathJax 的 ignoreHtmlClass 会跳过它，
        # 否则原文里的 \(...\) / \[...\] 定界符又会被 MathJax 扫回去渲染一遍。
        skip_math = ' no-mathjax' if raw else ''
        bubble = (f'<div class="{skip_math}" style="background:{bubble_bg};'
                  f'color:{text_color};'
                  f'border-radius:{radius};padding:8px 12px;max-width:100%;'
                  f'font-size:{font_size}px;line-height:1.5;white-space:normal;">'
                  f'{body}</div>')

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

    @Slot(QUrl)
    def _on_anchor_clicked(self, url):
        """处理气泡内切换链接点击，翻转对应消息的显示格式并重放。"""
        href = url.toString()
        if href.startswith("chat:raw:"):
            self._render_raw[int(href[len("chat:raw:"):])] = True
        elif href.startswith("chat:md:"):
            self._render_raw[int(href[len("chat:md:"):])] = False
        else:
            return
        self._reset_history_view(force=True)

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

    def _reset_history_view(self, force: bool = False) -> None:
        """重建整段气泡 HTML 并注入 WebView（含流式中的助手回复与底部提示）。

        流式生成期间（worker 存活）走防抖合并，避免每个 token 都触发一次
        MathJax 全量重排；用户主动动作（发送 / 清空 / 字号 / 主题 / 切换原文）
        传 force=True 立即生效。页面未加载完成前的调用会被 _push_html 拦截，
        由 _on_page_loaded 负责在就绪后补齐。
        """
        parts: list = []
        last_ts: float | None = None
        for i, m in enumerate(self.session.messages):
            role = m.get("role", _ROLE_USER)
            text = m.get("content", "")
            if isinstance(text, str) and text:
                # 助手消息携带其统计（存于消息内嵌字段，若存在）；索引用于切换链接
                meta = m.get("_meta", "")
                ts = m.get("time")
                # 首条消息或与上一条间隔 >5 分钟时显示时间分隔条
                show_time = (ts is not None and (
                    last_ts is None or ts - last_ts > 300))
                if ts is not None:
                    last_ts = ts
                parts.append(self._render_block(
                    role, text, meta, self._font_size, i,
                    show_time=show_time, mtime=ts))
        if self._pending_assistant:
            # 流式中：整条视为「新的一段时间」，仅在最新一条之后显示时间
            now = _time.time()
            show_time = not parts or (last_ts is None or now - last_ts > 300)
            parts.append(self._render_block(
                _ROLE_ASSISTANT, "".join(self._pending_assistant),
                font_size=self._font_size, show_time=show_time, mtime=now))
        if not parts:
            parts.append(self._empty_hint_html())
        html = "".join(parts) + self._notice_html

        if force or self._worker is None:
            self._pending_html = None
            self._flush_timer.stop()
            self._push_html(html)
        else:
            self._pending_html = html
            self._flush_timer.start()

    def _flush_stream(self) -> None:
        """防抖定时器触发：把攒下的最新 HTML 推给页面。"""
        if self._pending_html is None:
            return
        self._push_html(self._pending_html)
        self._pending_html = None

    def _push_html(self, html: str) -> None:
        """经 window.__renderChat 把 HTML + 当前主题底色注入 WebView 页面。

        页面未加载完成时（__renderChat 可能未定义）跳过注入，由 loadFinished
        后的 _on_page_loaded 重放当前视图补齐，避免提前 runJavaScript 抛错导致
        首屏气泡 / LaTeX 公式不显示。
        """
        if not self._page_ready:
            return
        page_bg = _chat_palette()["page_bg"]
        script = "window.__renderChat(%s, %s);" % (
            _json.dumps(html), _json.dumps(page_bg))
        self.view.page().runJavaScript(script)

    def _scroll_to_bottom(self) -> None:
        self.view.page().runJavaScript(
            "window.scrollTo(0, document.body.scrollHeight);")

    def apply_theme_colors(self) -> None:
        """把当前主题的 WebView 视图级底色同步到 Chromium 页面背景。

        用于消除「首帧白闪」：QWebEnginePage.setBackgroundColor 设定的是合成器
        铺底颜色，早于任何 DOM/CSS，因此切到「对话」Tab、WebView 首次提交渲染时
        即已是主题色。主题切换（_on_toggle_theme）时也调用此方法保持同步。

        注意：此处刻意用 ThemeManager().current() 而非 _chat_palette()——后者在
        ChatPage.__init__ 阶段可能读到尚未 apply 的默认主题（暗），导致亮色用户
        拿到暗色铺底、与 JS 上色后的 body 形成上下分色。current() 反映实际生效
        的主题，apply_theme() 之后即为真值。
        """
        if ThemeManager().current() == ThemeManager.LIGHT:
            page_bg = "#FFFFFF"
        else:
            page_bg = "#161B26"
        self.view.page().setBackgroundColor(QColor(page_bg))

    # ------------------------------------------------------------------ #
    # provider 选择
    # ------------------------------------------------------------------ #
    def _provider_id(self):
        data = self.provider_combo.currentData()
        return None if data is None else int(data)

    def refresh_providers(self):
        """重建 provider 下拉：Auto(自适应) + 所有已启用且未超配额的 provider。"""
        previous = self.provider_combo.currentData()
        providers = [p for p in self.gateway.providers.list() if p.is_available()]
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

    def _apply_input_font(self):
        """把当前字号应用到输入框，使其与气泡字号保持一致。"""
        font = QFont(self.input.font())
        font.setPointSize(self._font_size)
        self.input.setFont(font)

    def _on_font_changed(self):
        """字号切换：持久化，应用到输入框并重放气泡。"""
        data = self.font_combo.currentData()
        if data is None:
            return
        self._font_size = int(data)
        self._settings.setValue("chat/font_size", self._font_size)
        self._apply_input_font()
        self._reset_history_view(force=True)

    # ------------------------------------------------------------------ #
    # 发送 / 流式接收
    # ------------------------------------------------------------------ #
    @Slot()
    def _on_send(self):
        text = self.input.toPlainText().strip()
        if not text or self._worker is not None:
            return
        pid = self._provider_id()
        self.session.add_user(text)
        self.input.clear()

        # 渲染：重建整段（含新增用户气泡），立即生效
        self._pending_assistant = []
        self._last_stats = ""
        self._notice_html = ""
        self._reset_history_view(force=True)

        self._set_busy(True)
        self._worker = _ChatWorker(
            self.gateway, self.session.to_message_list(),
            provider_id=pid)
        self._worker.token.connect(self._on_token)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    @Slot(str)
    def _on_token(self, piece: str):
        self._pending_assistant.append(piece)
        # 整段重建：确认消息 + 流式中的助手气泡，防抖刷盘
        self._reset_history_view()

    @Slot(object)
    def _on_finished(self, result):
        full = "".join(self._pending_assistant)
        if result is not None:
            self._last_stats = self.tr(
                f"{result.total_tokens:,} tokens · "
                f"{result.elapsed_ms / 1000:.1f}s",
                f"{result.total_tokens:,} tokens · "
                f"{result.elapsed_ms / 1000:.1f}s")
        if full:
            # 将统计内嵌到消息，供 _reset_history_view 重放时显示
            self.session.add_assistant(full)
            self.session.messages[-1]["_meta"] = self._last_stats
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
        self._set_busy(False)
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
        self._pending_assistant = []
        self._notice_html = ""
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
        self.refresh_providers()  # Auto 项文案随语言刷新
        self.input.setPlaceholderText(self.tr("输入消息…", "Type a message…"))
        self.hint_label.setText(self.tr("Enter 换行 · Ctrl+Enter 发送",
                                        "Enter new line · Ctrl+Enter send"))
        self.send_btn.setText(self.tr("发送", "Send")
                              if self._worker is None else self.tr("生成中…", "Generating…"))
        self.stop_btn.setText(self.tr("停止", "Stop"))
        self.clear_btn.setText(self.tr("清空", "Clear"))
        # 重放气泡（角色名 / 统计文案随语言刷新）
        self._reset_history_view()