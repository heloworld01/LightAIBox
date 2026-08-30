"""统一 API 服务控制页：端口、协议开关、启动/停止、端点地址展示与复制。"""
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..gateway import Gateway
from ..server import GatewayServer, ServerConfig
from .i18n import LanguageManager
from .widgets import SectionHeader, StatusBadge


class ApiPage(QWidget):
    def __init__(self, gateway: Gateway, server: GatewayServer, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self.server = server
        self.tr = LanguageManager().tr
        self._build_ui()
        self._sync_from_server()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        self.explain_label = QLabel(
            self.tr("把本地 Gateway 暴露为可选的 OpenAI 兼容 / Anthropic 兼容接口，"
                    "供外部工具（curl / OpenAI SDK / Anthropic SDK 等）通过 base_url 调用。\n"
                    "模型名传真实模型名会匹配对应 provider；传「auto」则按调度策略自适应。",
                    "Exposes the local gateway as an optional OpenAI-compatible / "
                    "Anthropic-compatible API, callable by external tools "
                    "(curl / OpenAI SDK / Anthropic SDK, etc.) via base_url.\n"
                    "Passing a real model name matches its provider; pass "
                    "\"auto\" for adaptive scheduling."))
        self.explain_label.setWordWrap(True)
        self.explain_label.setProperty("class", "explain-text")
        root.addWidget(self.explain_label)

        # ---- 服务控制 ----
        ctrl = QFrame()
        ctrl.setProperty("class", "panel")
        cv = QVBoxLayout(ctrl)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(12)
        self.ctrl_header = SectionHeader(self.tr("服务控制", "Service Control"))
        cv.addWidget(self.ctrl_header)

        cf = QFormLayout()
        cf.setLabelAlignment(Qt.AlignRight)
        cf.setHorizontalSpacing(12)
        cf.setVerticalSpacing(14)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(config.DEFAULT_SERVER_PORT)
        self.port_spin.setFixedWidth(100)
        self.port_label = QLabel(self.tr("端口", "Port"))
        cf.addRow(self.port_label, self.port_spin)

        self.openai_check = QCheckBox(
            self.tr("启用 OpenAI 兼容接口 (/v1/chat/completions, /v1/models)",
                    "Enable OpenAI-compatible API (/v1/chat/completions, /v1/models)"))
        self.anthropic_check = QCheckBox(
            self.tr("启用 Anthropic 兼容接口 (/v1/messages)",
                    "Enable Anthropic-compatible API (/v1/messages)"))
        self.protocol_label = QLabel(self.tr("协议", "Protocol"))
        cf.addRow(self.protocol_label, self.openai_check)
        cf.addRow("", self.anthropic_check)
        self.openai_check.stateChanged.connect(self._on_protocol_changed)
        self.anthropic_check.stateChanged.connect(self._on_protocol_changed)

        btns = QHBoxLayout()
        self.start_btn = QPushButton(self.tr("启动", "Start"))
        self.start_btn.setProperty("class", "success")
        self.stop_btn = QPushButton(self.tr("停止", "Stop"))
        self.stop_btn.setProperty("class", "danger")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch()
        cf.addRow(btns)

        self.status_badge = StatusBadge(self.tr("已停止", "Stopped"), "neutral")
        self.status_label = QLabel(self.tr("状态", "Status"))
        cf.addRow(self.status_label, self.status_badge)

        cv.addLayout(cf)
        root.addWidget(ctrl)

        # ---- 端点地址 ----
        ep = QFrame()
        ep.setProperty("class", "panel")
        ev = QVBoxLayout(ep)
        ev.setContentsMargins(16, 16, 16, 16)
        ev.setSpacing(12)
        self.endpoint_header = SectionHeader(self.tr("端点地址", "Endpoints"))
        ev.addWidget(self.endpoint_header)

        ef = QFormLayout()
        ef.setLabelAlignment(Qt.AlignRight)
        ef.setHorizontalSpacing(12)
        ef.setVerticalSpacing(14)
        self._url_rows = []
        self._url_rows.append(self._url_row(
            ef, self.tr("OpenAI 对话", "OpenAI Chat"),
            "http://{host}:{port}/v1/chat/completions"))
        self._url_rows.append(self._url_row(
            ef, self.tr("OpenAI 模型列表", "OpenAI Model List"),
            "http://{host}:{port}/v1/models"))
        self._url_rows.append(self._url_row(
            ef, self.tr("Anthropic 对话", "Anthropic Chat"),
            "http://{host}:{port}/v1/messages"))
        ev.addLayout(ef)
        root.addWidget(ep)

        root.addStretch()

    def _url_row(self, form, label, template):
        label_widget = QLabel(label)
        edit = QLineEdit()
        edit.setReadOnly(True)
        copy_btn = QPushButton(self.tr("复制", "Copy"))
        copy_btn.setProperty("class", "ghost")
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(copy_btn)
        form.addRow(label_widget, row)
        copy_btn.clicked.connect(
            lambda _=False, e=edit, b=copy_btn: self._copy(e, b))
        return (label_widget, edit, template, copy_btn)

    def _copy(self, edit: QLineEdit, btn: QPushButton):
        QApplication.clipboard().setText(edit.text())
        # 复制成功的反馈：按钮文字临时变为「✓ 已复制」，1.5 秒后恢复
        original = self.tr("复制", "Copy")
        btn.setText(self.tr("✓ 已复制", "✓ Copied"))
        btn.setProperty("class", "row-action-success")
        btn.style().unpolish(btn)
        btn.style().polish(btn)

        def _restore():
            btn.setText(original)
            btn.setProperty("class", "ghost")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        QTimer.singleShot(1500, _restore)

    def _current_url(self, template: str) -> str:
        cfg = self.server.config
        return template.format(host=cfg.host, port=cfg.port)

    def _sync_from_server(self):
        cfg = self.server.config
        # 先读快照再赋值：setChecked() 会同步触发 stateChanged →
        # _on_protocol_changed（运行时），后者会写回同一个 cfg 对象。若不先
        # 快照，openai_check 的 setChecked(True) 会在 anthropic_check 尚未勾选时
        # 把 cfg.anthropic_enabled 写回 False，导致随后读到错误值。
        openai_enabled = cfg.openai_enabled
        anthropic_enabled = cfg.anthropic_enabled
        port = cfg.port
        self.port_spin.setValue(port)
        self.openai_check.setChecked(openai_enabled)
        self.anthropic_check.setChecked(anthropic_enabled)
        self._update_urls()
        self._update_running_state()

    def _update_urls(self):
        for label, edit, template, copy_btn in self._url_rows:
            edit.setText(self._current_url(template))

    def retranslate(self):
        """语言切换后统一刷新页面文案。"""
        tr = self.tr
        self.explain_label.setText(self.tr(
            "把本地 Gateway 暴露为可选的 OpenAI 兼容 / Anthropic 兼容接口，"
            "供外部工具（curl / OpenAI SDK / Anthropic SDK 等）通过 base_url 调用。\n"
            "模型名传真实模型名会匹配对应 provider；传「auto」则按调度策略自适应。",
            "Exposes the local gateway as an optional OpenAI-compatible / "
            "Anthropic-compatible API, callable by external tools "
            "(curl / OpenAI SDK / Anthropic SDK, etc.) via base_url.\n"
            "Passing a real model name matches its provider; pass "
            "\"auto\" for adaptive scheduling."))
        self.ctrl_header.title_label.setText(tr("服务控制", "Service Control"))
        self.port_label.setText(tr("端口", "Port"))
        self.protocol_label.setText(tr("协议", "Protocol"))
        self.openai_check.setText(tr(
            "启用 OpenAI 兼容接口 (/v1/chat/completions, /v1/models)",
            "Enable OpenAI-compatible API (/v1/chat/completions, /v1/models)"))
        self.anthropic_check.setText(tr(
            "启用 Anthropic 兼容接口 (/v1/messages)",
            "Enable Anthropic-compatible API (/v1/messages)"))
        self.start_btn.setText(tr("启动", "Start"))
        self.stop_btn.setText(tr("停止", "Stop"))
        self.status_label.setText(tr("状态", "Status"))
        self._update_running_state()
        self.endpoint_header.title_label.setText(tr("端点地址", "Endpoints"))
        url_titles = (tr("OpenAI 对话", "OpenAI Chat"),
                      tr("OpenAI 模型列表", "OpenAI Model List"),
                      tr("Anthropic 对话", "Anthropic Chat"))
        for title, row in zip(url_titles, self._url_rows):
            row[0].setText(title)
            row[3].setText(tr("复制", "Copy"))
        self._update_urls()

    def _update_running_state(self):
        tr = self.tr
        running = self.server.is_running
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.port_spin.setEnabled(not running)
        if running:
            self.status_badge.set_status(
                tr("运行中", "Running"), "success")
            self.status_badge.setToolTip(
                f"http://{self.server.config.host}:{self.server.config.port}")
        else:
            self.status_badge.set_status(tr("已停止", "Stopped"), "neutral")
            self.status_badge.setToolTip("")

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_protocol_changed(self):
        # 运行时勾选开关即时生效（无需重启）
        if self.server.is_running:
            self.server.config.set(
                openai_enabled=self.openai_check.isChecked(),
                anthropic_enabled=self.anthropic_check.isChecked())

    @Slot()
    def _on_start(self):
        cfg = ServerConfig(
            host=self.server.config.host,
            port=self.port_spin.value(),
            openai_enabled=self.openai_check.isChecked(),
            anthropic_enabled=self.anthropic_check.isChecked(),
        )
        self.server.config.set(
            port=cfg.port, openai_enabled=cfg.openai_enabled,
            anthropic_enabled=cfg.anthropic_enabled)
        self.server.start(cfg)
        self._update_urls()
        self._update_running_state()

    @Slot()
    def _on_stop(self):
        self.server.stop()
        self._update_running_state()