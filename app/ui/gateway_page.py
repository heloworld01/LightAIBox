"""AI 网关页：Provider 管理 + 调用记录。"""
import datetime

from PySide6.QtCore import QMutex, QRect, QThread, QTimer, Qt, QWaitCondition, Signal, Slot
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..gateway import Gateway, GatewayError
from .i18n import LanguageManager
from .widgets import SectionHeader, StatusBadge, MessageBox, log_status_semantic, status_badge_semantic


def _setup_table_style(table: QTableWidget) -> None:
    """给表格套用统一行为：无网格、隐藏行号、去焦点框、整行选中。"""
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    # 行高需容纳行内操作按钮（min-height 26px + 上下各 6px 留白），
    # 否则按钮会被裁剪、显示不全。
    table.verticalHeader().setDefaultSectionSize(42)
    table.setWordWrap(False)
    table.setFocusPolicy(Qt.NoFocus)
    # 禁止从表格区域直接双击编辑内容（仅通过行/列按钮编辑）
    table.setEditTriggers(QTableWidget.NoEditTriggers)


class _RowIndicatorDelegate(QStyledItemDelegate):
    """整行选中时，只在最左列（column 0）画一条选中指示条。

    默认 QSS 里 QTableWidget::item:selected 的 border-left 会对每一个被选中
    的单元格生效，导致每一列最左侧都出现色条。改用 delegate 在绘制时判断：
    仅当当前单元格是第一列且处于选中状态时，才在左边缘画一条竖线，从而
    实现"只在整行最左端显示一条指示条"。
    """

    def __init__(self, color: QColor, parent=None):
        super().__init__(parent)
        self._color = color

    def paint(self, painter, option, index):
        # 先按默认样式绘制单元格（含选中背景），保证文字、对齐等不受影响
        super().paint(painter, option, index)
        # 仅第一列 + 选中状态下补画左侧指示条
        if index.column() == 0 and (option.state & QStyle.State_Selected):
            painter.save()
            pen = QPen(self._color)
            pen.setWidth(3)
            painter.setPen(pen)
            r = option.rect
            painter.drawLine(r.left() + 1, r.top() + 2, r.left() + 1, r.bottom() - 2)
            painter.restore()


# --------------------------------------------------------------------------- #
# Provider 编辑对话框
# --------------------------------------------------------------------------- #
class ProviderDialog(QDialog):
    """新增 / 编辑单个 provider。exec() 后通过 result() 取回填好的 Provider。"""

    def __init__(self, provider=None, providers=None, parent=None):
        super().__init__(parent)
        self._provider = provider
        self._providers = providers or []  # 全部 provider，用于名称查重
        self._result = None
        self.tr = LanguageManager().tr
        self.setWindowTitle(self.tr("编辑 Provider", "Edit Provider")
                            if provider else self.tr("新增 Provider", "Add Provider"))
        # 无边框 + 透明背景，配合圆角容器实现四角圆角，去除原生标题栏
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, self.height())  # 增大默认宽度，避免字段拥挤
        self._build_ui()
        if provider:
            self._load(provider)

    def _build_ui(self):
        tr = self.tr
        # 圆角背景载体：透明窗口内一层实色圆角容器
        bg = QFrame()
        bg.setProperty("class", "dialog-card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(bg)

        root = QVBoxLayout(bg)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自绘标题栏：标题 + 关闭按钮（无边框对话框需自带拖动 / 关闭能力）
        hdr = QFrame()
        hdr.setProperty("class", "dialog-header")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(24, 12, 12, 8)
        hl.setSpacing(8)
        self._title_label = QLabel(self.windowTitle())
        self._title_label.setProperty("class", "dialog-title")
        hl.addWidget(self._title_label)
        hl.addStretch()
        self._dlg_close_btn = QPushButton("✕")
        self._dlg_close_btn.setProperty("class", "ghost")
        self._dlg_close_btn.setFixedSize(24, 24)
        self._dlg_close_btn.setToolTip(tr("关闭", "Close"))
        self._dlg_close_btn.clicked.connect(self.close)
        hl.addWidget(self._dlg_close_btn)
        root.addWidget(hdr)

        form = QFormLayout()
        form.setContentsMargins(24, 8, 24, 20)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        root.addLayout(form)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("例如：官方 GPT-4o", "e.g. Official GPT-4o"))
        form.addRow(tr("名称 *", "Name *"), self.name_edit)

        self.api_type_combo = QComboBox()
        self.api_type_combo.addItem("OpenAI 兼容", config.API_OPENAI)
        self.api_type_combo.addItem("Anthropic", config.API_ANTHROPIC)
        self.api_type_combo.addItem(tr("兼容(OpenAI+Anthropic)", "Both (OpenAI+Anthropic)"), config.API_BOTH)
        form.addRow(tr("协议类型", "API Type"), self.api_type_combo)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText(
            tr("留空用官方默认；可填中转地址，末尾 /v1 可省略(自动补全)",
               "Leave blank for official default; relay URL allowed, /v1 auto-appended"))
        form.addRow("Base URL", self.base_url_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        form.addRow(tr("API Key *", "API Key *"), self.api_key_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如 gpt-4o / claude-sonnet-5")
        form.addRow(tr("模型 *", "Model *"), self.model_edit)

        # 最小输入 token 门槛：仅自适应调度（不指定模型）时，输入字符数达到该值才调用
        self.min_input_spin = QSpinBox()
        self.min_input_spin.setRange(0, 2_147_483_647)
        self.min_input_spin.setSpecialValueText(tr("0（不限制）", "0 (unlimited)"))
        self.min_input_spin.setSuffix(tr(" 字符", " chars"))
        form.addRow(tr("最小输入 token", "Min input tokens"), self.min_input_spin)

        # 启用开关
        self.enabled_check = QCheckBox(tr("启用该 Provider", "Enable this provider"))
        self.enabled_check.setChecked(True)
        form.addRow(tr("状态", "Status"), self.enabled_check)

        # 配额类型
        self.quota_combo = QComboBox()
        self.quota_combo.addItem(tr("无限制", "Unlimited"), config.QUOTA_UNLIMITED)
        self.quota_combo.addItem(tr("按调用次数", "By calls"), config.QUOTA_CALLS)
        self.quota_combo.addItem(tr("按 token 数", "By tokens"), config.QUOTA_TOKENS)
        self.quota_combo.currentIndexChanged.connect(self._on_quota_changed)
        form.addRow(tr("配额类型", "Quota type"), self.quota_combo)

        self.quota_spin = QSpinBox()
        self.quota_spin.setRange(0, 2_147_483_647)  # int32 上限，QSpinBox 限制
        self.quota_spin.setEnabled(False)
        form.addRow(tr("配额上限", "Quota limit"), self.quota_spin)

        btns = QHBoxLayout()
        self.save_btn = QPushButton(tr("保存", "Save"))
        self.save_btn.setProperty("class", "primary")
        self.cancel_btn = QPushButton(tr("取消", "Cancel"))
        self.cancel_btn.setProperty("class", "ghost")
        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.close)
        btns.addStretch()
        btns.addWidget(self.save_btn)
        btns.addWidget(self.cancel_btn)
        form.addRow(btns)

    def _load(self, p):
        self.name_edit.setText(p.name)
        idx = self.api_type_combo.findData(p.api_type)
        if idx >= 0:
            self.api_type_combo.setCurrentIndex(idx)
        self.base_url_edit.setText(p.base_url)
        self.api_key_edit.setText(p.api_key)
        self.model_edit.setText(p.model)
        self.min_input_spin.setValue(p.min_input_tokens)
        self.enabled_check.setChecked(p.enabled)
        idx = self.quota_combo.findData(p.quota_type)
        if idx >= 0:
            self.quota_combo.setCurrentIndex(idx)
        self.quota_spin.setValue(p.quota_limit)
        self._on_quota_changed()

    def _on_quota_changed(self):
        limited = self.quota_combo.currentData() != config.QUOTA_UNLIMITED
        self.quota_spin.setEnabled(limited)

    # ------------------------------------------------------------------ #
    # 无边框对话框拖动：按住自绘标题栏拖动整窗
    # ------------------------------------------------------------------ #
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

    @Slot()
    def _on_save(self):
        from ..models import Provider

        name = self.name_edit.text().strip()
        api_key = self.api_key_edit.text().strip()
        model = self.model_edit.text().strip()
        if not name or not api_key or not model:
            MessageBox.warning(self, self.tr("提示", "Notice"),
                               self.tr("名称、API Key、模型为必填项",
                                       "Name, API Key and Model are required"))
            return
        # 名称唯一：不得与其它 provider 重名（编辑时排除自身）
        current_id = self._provider.id if self._provider is not None else None
        if any(q.id != current_id and q.name == name for q in self._providers):
            MessageBox.warning(
                self, self.tr("提示", "Notice"),
                self.tr(f"Provider 名称「{name}」已存在，请更换名称",
                        f"Provider name \"{name}\" already exists, please use another"))
            return

        quota_type = self.quota_combo.currentData()
        quota_limit = self.quota_spin.value() if quota_type != config.QUOTA_UNLIMITED else 0

        p = Provider(
            name=name,
            api_type=self.api_type_combo.currentData(),
            base_url=self.base_url_edit.text().strip(),
            api_key=api_key,
            model=model,
            enabled=self.enabled_check.isChecked(),
            min_input_tokens=self.min_input_spin.value(),
            quota_type=quota_type,
            quota_limit=quota_limit,
        )
        if self._provider is not None:
            p.id = self._provider.id
            p.used_calls = self._provider.used_calls
            p.used_tokens = self._provider.used_tokens
            p.last_tokens_per_sec = self._provider.last_tokens_per_sec
            p.last_call_at = self._provider.last_call_at
            p.auto_disabled = self._provider.auto_disabled
            p.disable_reason = self._provider.disable_reason
        self._result = p
        self.accept()  # accept() 使 exec() 返回真值，保存/新增才能生效

    def result(self):
        return self._result


# --------------------------------------------------------------------------- #
# 后台测连线程
# --------------------------------------------------------------------------- #
class _TestWorker(QThread):
    """后台线程执行测连，通过信号回传结果，不阻塞 UI、不弹窗。"""
    done = Signal(int, object)  # (provider_id, result dict)

    def __init__(self, gateway: Gateway, pid: int, parent=None):
        super().__init__(parent)
        self._gateway = gateway
        self._pid = pid

    def run(self):
        try:
            result = self._gateway.test_provider(self._pid)
        except Exception as exc:  # 兜底：test_provider 内部已捕获，避免线程异常外泄
            result = {"ok": False, "elapsed_ms": 0, "error": str(exc)}
        self.done.emit(self._pid, result)


# --------------------------------------------------------------------------- #
# 后台自动刷新线程
# --------------------------------------------------------------------------- #
class _RefreshWorker(QThread):
    """后台执行「签名检测 → 数据查询」，避免 UI 线程做 SQLite 往返阻塞界面。

    主线程调用 request(start, end, provider_id) 唤醒一轮刷新；线程在后台查询
    provider 列表、最近日志签名、完整日志与统计，若数据有变化（含过滤参数变化）
    则通过 results 信号回传。期间新的 request 会被合并，查询不堆积。
    """

    results = Signal(object)  # dict{providers, logs, stats, start, end, provider_id}

    def __init__(self, gateway: Gateway, parent=None):
        super().__init__(parent)
        self._gateway = gateway
        self._mutex = QMutex()
        self._cond = QWaitCondition()
        self._pending = False   # 是否有未处理的刷新请求
        self._run = True
        self._sig_key = None    # 上次结果的数据签名（含过滤参数）
        self._start = None
        self._end = None
        self._provider_id = None

    def request(self, start, end, provider_id):
        """记录过滤参数并唤醒一轮刷新（线程安全，可在主线程调用）。"""
        self._mutex.lock()
        self._start = start
        self._end = end
        self._provider_id = provider_id
        self._pending = True
        self._cond.wakeOne()
        self._mutex.unlock()

    def stop(self):
        """请求线程退出（应用退出前调用）。"""
        self._mutex.lock()
        self._run = False
        self._cond.wakeOne()
        self._mutex.unlock()

    def run(self):
        while True:
            self._mutex.lock()
            while not self._pending and self._run:
                self._cond.wait(self._mutex)
            if not self._run:
                self._mutex.unlock()
                return
            self._pending = False
            start = self._start
            end = self._end
            provider_id = self._provider_id
            self._mutex.unlock()

            # —— 以下查询均在后台线程，不阻塞 UI ——
            providers = self._gateway.providers.list()
            recent = self._gateway.logs.list(limit=3)  # 全局最近签名，检测任意新日志
            sig_key = (
                start, end, provider_id,
                tuple((p.id, p.enabled, p.auto_disabled, p.disable_reason,
                       round(p.last_tokens_per_sec, 2), p.used_calls, p.used_tokens)
                      for p in providers),
                tuple((lg.id, lg.status, lg.created_at) for lg in recent),
            )
            if sig_key == self._sig_key:
                continue
            self._sig_key = sig_key

            logs = self._gateway.logs.list(
                limit=200, provider_id=provider_id, start=start, end=end)
            st = self._gateway.logs.stats(
                provider_id=provider_id, start=start, end=end)
            self.results.emit({
                "providers": providers, "logs": logs, "stats": st,
                "start": start, "end": end, "provider_id": provider_id,
            })


# --------------------------------------------------------------------------- #
# 网关页
# --------------------------------------------------------------------------- #
class GatewayPage(QWidget):
    def __init__(self, gateway: Gateway, parent=None):
        super().__init__(parent)
        self.gateway = gateway
        self.tr = LanguageManager().tr
        self._test_state = {}  # pid -> 后台测连状态：running / ok / error
        self._provider_filter_sig = None  # provider 过滤下拉集合签名，用于跳过无谓重建
        self._build_ui()
        self.refresh_providers()
        self.refresh_logs()
        self._start_autorefresh()

    def _start_autorefresh(self):
        """统一 API / 后台线程写入的记录不会主动通知 UI，这里定时触发后台刷新。

        查询（SQLite 往返：provider 列表 / 日志 / 统计）全部放到 _RefreshWorker
        后台线程，UI 线程只做渲染，避免高频刷新阻塞界面。
        """
        self._refresh_worker = _RefreshWorker(self.gateway, parent=self)
        self._refresh_worker.results.connect(self._on_refresh_data)
        self._refresh_worker.start()
        # 应用退出前停止后台线程，避免 QThread 在线程运行中被销毁
        QApplication.instance().aboutToQuit.connect(self._refresh_worker.stop)

        timer = QTimer(self)
        timer.setInterval(3000)
        timer.timeout.connect(self._request_refresh)
        timer.start()
        self._autorefresh = timer

    def _request_refresh(self):
        """把当前过滤参数快照交给后台线程，请求一轮数据刷新。"""
        start, end = self._log_range()
        pid = self.log_provider_combo.currentData()
        self._refresh_worker.request(start, end, pid)

    @Slot(object)
    def _on_refresh_data(self, data: dict):
        """后台查询结果回传，校验过滤参数未过期后渲染到 UI。"""
        start, end = self._log_range()
        pid = self.log_provider_combo.currentData()
        if (data["start"], data["end"], data["provider_id"]) != (start, end, pid):
            # 期间用户切换了过滤条件，本次结果已过期，重新请求
            self._refresh_worker.request(start, end, pid)
            return
        providers = data["providers"]
        names = {p.id: p.name for p in providers}
        self._render_providers(providers)
        self._render_logs(data["logs"], names, data["stats"])

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _accent_color(self) -> QColor:
        """当前主题的主色（选中指示条颜色）：暗色 #6366F1，亮色 #4F46E5。"""
        from .theme_manager import ThemeManager
        if ThemeManager().current() == ThemeManager.LIGHT:
            return QColor("#4F46E5")
        return QColor("#6366F1")

    def _build_ui(self):
        root = QVBoxLayout(self)

        # 左右分栏：左=Provider 列表，右=调用入口 + 日志
        splitter = QSplitter(Qt.Vertical)

        # ---- 左：Provider 列表 ----
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(12)
        self.provider_header = SectionHeader(self.tr("Provider 列表", "Providers"))
        lv.addWidget(self.provider_header)

        self.provider_table = QTableWidget(0, 8)
        self.provider_table.setHorizontalHeaderLabels(self._provider_headers())
        hdr = self.provider_table.horizontalHeader()
        # 各列宽度自适应：短内容列按内容自适应，可变长文本列弹性占满剩余，
        # 状态列（StatusBadge）固定宽度以保证胶囊完整展示，
        # 操作列（按钮容器）固定宽度避免因按钮状态变化而跳动。
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)   # 名称（可变长文本）
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)   # 模型（可变长文本）
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)     # 状态（胶囊标签）
        self.provider_table.setColumnWidth(3, 120)
        hdr.setSectionResizeMode(7, QHeaderView.Fixed)     # 操作（按钮容器）
        self.provider_table.setColumnWidth(7, 150)
        self.provider_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.provider_table.setSelectionMode(QTableWidget.SingleSelection)
        _setup_table_style(self.provider_table)
        # 整行选中时只在最左列画指示条（默认 QSS 会对每个选中单元格都画边框）
        self.provider_table.setItemDelegate(_RowIndicatorDelegate(self._accent_color()))
        lv.addWidget(self.provider_table)

        pb = QHBoxLayout()
        self.add_btn = QPushButton(self.tr("新增", "Add"))
        self.add_btn.setProperty("class", "primary")
        self.edit_btn = QPushButton(self.tr("编辑", "Edit"))
        self.edit_btn.setProperty("class", "secondary")
        self.copy_btn = QPushButton(self.tr("复制", "Copy"))
        self.copy_btn.setProperty("class", "secondary")
        self.del_btn = QPushButton(self.tr("删除", "Delete"))
        self.del_btn.setProperty("class", "danger")
        self.reset_btn = QPushButton(self.tr("重置配额", "Reset quota"))
        self.reset_btn.setProperty("class", "secondary")
        pb.addWidget(self.add_btn)
        pb.addWidget(self.edit_btn)
        pb.addWidget(self.copy_btn)
        pb.addWidget(self.del_btn)
        pb.addWidget(self.reset_btn)
        pb.addSpacing(12)
        self._toolbar_sep = QFrame()
        self._toolbar_sep.setProperty("class", "v-separator")
        self._toolbar_sep.setFrameShape(QFrame.VLine)
        pb.addWidget(self._toolbar_sep)
        self.up_btn = QPushButton(self.tr("↑ 上移", "↑ Up"))
        self.up_btn.setProperty("class", "ghost")
        self.down_btn = QPushButton(self.tr("↓ 下移", "↓ Down"))
        self.down_btn.setProperty("class", "ghost")
        pb.addWidget(self.up_btn)
        pb.addWidget(self.down_btn)
        pb.addStretch()
        lv.addLayout(pb)

        self.add_btn.clicked.connect(self._on_add_provider)
        self.edit_btn.clicked.connect(self._on_edit_provider)
        self.copy_btn.clicked.connect(self._on_copy_provider)
        self.del_btn.clicked.connect(self._on_delete_provider)
        self.reset_btn.clicked.connect(self._on_reset_quota)
        self.up_btn.clicked.connect(lambda: self._on_move(-1))
        self.down_btn.clicked.connect(lambda: self._on_move(1))

        # ---- 右：调用记录 ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(12)

        log_box = QFrame()
        log_box.setProperty("class", "panel")
        lf = QVBoxLayout(log_box)
        lf.setContentsMargins(16, 16, 16, 16)
        lf.setSpacing(12)
        self.log_header = SectionHeader(self.tr("调用记录（最近）", "Call Logs (Recent)"))
        lf.addWidget(self.log_header)
        # 过滤条：列表与统计均可按日期 / provider 过滤
        fbar = QHBoxLayout()
        self.log_date_label = QLabel(self.tr("日期:", "Date:"))
        fbar.addWidget(self.log_date_label)
        self.log_date_combo = QComboBox()
        self._rebuild_date_items()
        self.log_date_combo.setFixedWidth(130)
        fbar.addWidget(self.log_date_combo)
        self.log_provider_label = QLabel("Provider:")
        fbar.addWidget(self.log_provider_label)
        self.log_provider_combo = QComboBox()
        self.log_provider_combo.addItem(self.tr("全部", "All"), None)
        self.log_provider_combo.setFixedWidth(150)
        fbar.addWidget(self.log_provider_combo)
        fbar.addStretch()
        lf.addLayout(fbar)
        self.log_date_combo.currentIndexChanged.connect(self.refresh_logs)
        self.log_provider_combo.currentIndexChanged.connect(self.refresh_logs)
        # token 用量统计行（随日期 / provider 过滤联动）
        self.log_stats_label = QLabel()
        self.log_stats_label.setProperty("class", "stats-text")
        lf.addWidget(self.log_stats_label)
        self.log_table = QTableWidget(0, 9)
        self.log_table.setHorizontalHeaderLabels(self._log_headers())
        hdr = self.log_table.horizontalHeader()
        # 各列宽度自适应：短内容列按内容自适应，可变长文本列弹性占满剩余，
        # 状态列（StatusBadge）固定宽度以保证胶囊完整展示，
        # 操作列（删除按钮）固定宽度。
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)   # 名称（可变长文本）
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)   # 模型（可变长文本）
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)     # 状态（胶囊标签）
        self.log_table.setColumnWidth(3, 120)
        hdr.setSectionResizeMode(8, QHeaderView.Fixed)     # 操作（删除按钮）
        self.log_table.setColumnWidth(8, 70)
        _setup_table_style(self.log_table)
        lf.addWidget(self.log_table)

        log_btns = QHBoxLayout()
        self.clear_logs_btn = QPushButton(self.tr("清空全部", "Clear All"))
        self.clear_logs_btn.setProperty("class", "danger")
        self.clear_logs_btn.clicked.connect(self._on_clear_logs)
        log_btns.addStretch()
        log_btns.addWidget(self.clear_logs_btn)
        lf.addLayout(log_btns)

        rv.addWidget(log_box)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 5)
        root.addWidget(splitter)

    # ------------------------------------------------------------------ #
    # 文案（i18n）：表头 / 日期预设 / 整页刷新
    # ------------------------------------------------------------------ #
    def _provider_headers(self) -> list:
        tr = self.tr
        return [tr("名称", "Name"), tr("协议", "Type"), tr("模型", "Model"),
                tr("状态", "Status"), tr("配额", "Quota"),
                tr("最小输入", "Min input"), tr("最近调用", "Last call"),
                tr("操作", "Actions")]

    def _log_headers(self) -> list:
        tr = self.tr
        return [tr("时间", "Time"), tr("名称", "Name"), tr("模型", "Model"),
                tr("状态", "Status"), tr("输入", "Input"), tr("输出", "Output"),
                tr("合计", "Total"), tr("速度", "Speed"), tr("操作", "Actions")]

    def _rebuild_date_items(self):
        """重建日期预设下拉项（文案随语言切换），保持原选中项。"""
        tr = self.tr
        current = self.log_date_combo.currentData()
        self.log_date_combo.blockSignals(True)
        self.log_date_combo.clear()
        for text, key in ((tr("全部日期", "All dates"), "all"),
                          (tr("今天", "Today"), "today"),
                          (tr("昨天", "Yesterday"), "yesterday"),
                          (tr("近 7 天", "Last 7 days"), "7d"),
                          (tr("本周", "This week"), "week"),
                          (tr("本月", "This month"), "month")):
            self.log_date_combo.addItem(text, key)
        idx = self.log_date_combo.findData(current)
        self.log_date_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.log_date_combo.blockSignals(False)

    def retranslate(self):
        """语言切换后统一刷新页面文案（含表格行、状态胶囊与统计行）。"""
        tr = self.tr
        self.provider_header.title_label.setText(tr("Provider 列表", "Providers"))
        self.provider_table.setHorizontalHeaderLabels(self._provider_headers())
        self.add_btn.setText(tr("新增", "Add"))
        self.edit_btn.setText(tr("编辑", "Edit"))
        self.copy_btn.setText(tr("复制", "Copy"))
        self.del_btn.setText(tr("删除", "Delete"))
        self.reset_btn.setText(tr("重置配额", "Reset quota"))
        self.up_btn.setText(tr("↑ 上移", "↑ Up"))
        self.down_btn.setText(tr("↓ 下移", "↓ Down"))
        self.log_header.title_label.setText(tr("调用记录（最近）", "Call Logs (Recent)"))
        self.log_date_label.setText(tr("日期:", "Date:"))
        self._rebuild_date_items()
        self.log_provider_combo.setItemText(0, tr("全部", "All"))
        self.log_table.setHorizontalHeaderLabels(self._log_headers())
        self.clear_logs_btn.setText(tr("清空全部", "Clear All"))
        # 表格行 / 状态胶囊 / 统计行文案依赖当前语言，直接重绘
        self.refresh_providers()
        self.refresh_logs()

    # ------------------------------------------------------------------ #
    # Provider 列表渲染
    # ------------------------------------------------------------------ #
    def _selected_provider(self):
        row = self.provider_table.currentRow()
        if row < 0:
            MessageBox.information(
                self, self.tr("提示", "Notice"),
                self.tr("请先选择一行 Provider", "Please select a provider row first"))
            return None
        pid = self.provider_table.item(row, 0).data(Qt.UserRole)
        return self.gateway.providers.get(pid)

    def refresh_providers(self):
        """同步刷新 provider 列表（用户主动操作后立即调用）。"""
        self._render_providers(self.gateway.providers.list())

    def _render_providers(self, providers: list):
        tr = self.tr
        # 记住当前选中 provider，重绘后恢复，避免刷新打断选中状态
        sel_pid = None
        sel_row = self.provider_table.currentRow()
        if sel_row >= 0:
            it = self.provider_table.item(sel_row, 0)
            if it is not None:
                sel_pid = it.data(Qt.UserRole)
        self.provider_table.setRowCount(len(providers))
        for r, p in enumerate(providers):
            name_item = QTableWidgetItem(p.name)
            name_item.setData(Qt.UserRole, p.id)
            self.provider_table.setItem(r, 0, name_item)
            if p.api_type == config.API_ANTHROPIC:
                type_text = "Anthropic"
            elif p.api_type == config.API_BOTH:
                type_text = tr("兼容", "Both")
            else:
                type_text = "OpenAI"
            self.provider_table.setItem(r, 1, QTableWidgetItem(type_text))
            self.provider_table.setItem(r, 2, QTableWidgetItem(p.model))

            # 状态列用 StatusBadge 胶囊标签替代纯文字颜色
            status_text = p.status_text()
            badge = StatusBadge(status_text, status_badge_semantic(status_text))
            # 状态胶囊非交互，让鼠标事件穿透到表格，点击该列也能选中整行
            badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.provider_table.setCellWidget(r, 3, badge)

            quota_item = QTableWidgetItem(p.quota_text())
            quota_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            quota_item.setToolTip(p.quota_text())
            self.provider_table.setItem(r, 4, quota_item)
            min_input_item = QTableWidgetItem(
                f"{p.min_input_tokens}" if p.min_input_tokens else "—")
            min_input_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            # 仅为自适应调度服务，指定模型名时忽略该门槛
            min_input_item.setToolTip(
                tr("仅自适应调度(不指定模型)时生效：输入字符数达到该值才调用",
                   "Only applies to adaptive scheduling (no model set): "
                   "called when input length reaches this value"))
            self.provider_table.setItem(r, 5, min_input_item)
            # 默认只展示日期，悬停气泡显示完整时间 + 近期速度
            last_call = p.last_call_at or ""
            date_only = last_call.split(" ")[0] if last_call else "-"
            last_call_item = QTableWidgetItem(date_only)
            if last_call:
                speed = p.last_tokens_per_sec
                last_call_item.setToolTip(
                    tr(f"最近调用：{last_call}\n近期速度：{speed:.1f} tok/s",
                       f"Last call: {last_call}\nRecent speed: {speed:.1f} tok/s"))
            self.provider_table.setItem(r, 6, last_call_item)

            toggle = tr("启用", "Enable") if not p.enabled else tr("停用", "Disable")
            op_widget = QWidget()
            op = QHBoxLayout(op_widget)
            op.setContentsMargins(0, 0, 0, 0)
            op.setSpacing(4)
            # 垂直居中，避免按钮在行内被裁剪
            op.setAlignment(Qt.AlignVCenter)

            toggle_btn = QPushButton(toggle)
            toggle_btn.setProperty("class", "row-action")
            toggle_btn.clicked.connect(
                lambda _=False, pid=p.id: self._toggle_by_id(pid))
            op.addWidget(toggle_btn)

            op.addWidget(self._make_test_btn(p))

            self.provider_table.setCellWidget(r, 7, op_widget)

        # 恢复之前选中的行（刷新会清空选中，这里按 provider id 找回）
        if sel_pid is not None:
            for r in range(self.provider_table.rowCount()):
                if self.provider_table.item(r, 0).data(Qt.UserRole) == sel_pid:
                    self.provider_table.selectRow(r)
                    break

    def _toggle_by_id(self, pid):
        p = self.gateway.providers.get(pid)
        if p is None:
            return
        self.gateway.providers.set_enabled(pid, not p.enabled)
        self.refresh_providers()

    def _test_by_id(self, pid):
        p = self.gateway.providers.get(pid)
        if p is None:
            return
        if not p.name:
            return
        # 后台线程发请求：无弹窗、不卡 UI，按钮状态展示进度与结果
        self._test_state[pid] = {"status": "running"}
        worker = _TestWorker(self.gateway, pid, parent=self)
        worker.done.connect(self._on_test_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.refresh_providers()

    @Slot(int, object)
    def _on_test_done(self, pid: int, result: dict):
        if result.get("ok"):
            self._test_state[pid] = {
                "status": "ok", "elapsed_ms": result.get("elapsed_ms", 0)}
        else:
            self._test_state[pid] = {
                "status": "error", "error": result.get("error", "")}
        self.refresh_providers()

    def _make_test_btn(self, p) -> QPushButton:
        """构造行内「测连」按钮：显示后台测连状态（进行中 / 成功 / 失败，详情见气泡）。"""
        tr = self.tr
        st = self._test_state.get(p.id)
        if st is None:
            text, tip = tr("测连", "Test"), tr(
                "后台向该 Provider 发送最小请求，验证地址与密钥",
                "Sends a minimal request to this provider to verify URL and key")
            cls = "row-action"
        elif st["status"] == "running":
            text, tip = tr("测连中…", "Testing…"), tr("后台测试中，请稍候…",
                                                     "Testing in background, please wait…")
            cls = "row-action"
        elif st["status"] == "ok":
            ms = st.get("elapsed_ms", 0)
            text, tip = f"✅ {ms} ms", tr(f"测连成功，耗时 {ms} ms",
                                          f"Test passed in {ms} ms")
            cls = "row-action-success"
        else:
            text, tip = tr("❌ 失败", "❌ Failed"), \
                st.get("error") or tr("测连失败", "Test failed")
            cls = "row-action-danger"
        btn = QPushButton(text)
        btn.setProperty("class", cls)
        btn.setToolTip(tip)
        if st is not None and st["status"] == "running":
            btn.setEnabled(False)
        btn.clicked.connect(lambda _=False, pid=p.id: self._test_by_id(pid))
        return btn

    # ------------------------------------------------------------------ #
    # Provider CRUD
    # ------------------------------------------------------------------ #
    def _on_add_provider(self):
        dlg = ProviderDialog(
            providers=self.gateway.providers.list(), parent=self)
        if dlg.exec():
            p = dlg.result()
            # 追加到列表末尾（末尾即最低调度优先级）
            p.sort_order = len(self.gateway.providers.list())
            self.gateway.providers.upsert(p)
            self.refresh_providers()

    def _on_edit_provider(self):
        p = self._selected_provider()
        if p is None:
            return
        dlg = ProviderDialog(
            provider=p, providers=self.gateway.providers.list(), parent=self)
        if dlg.exec():
            self.gateway.providers.upsert(dlg.result())
            self.refresh_providers()

    def _on_copy_provider(self):
        """复制选中 provider：配置原样保留，用量/自动关闭状态清零，追加到列表末尾。"""
        from dataclasses import replace

        p = self._selected_provider()
        if p is None:
            return
        new = replace(
            p,
            id=None,  # 新记录
            name=self._unique_copy_name(p.name),
            sort_order=len(self.gateway.providers.list()),
            used_calls=0,
            used_tokens=0,
            last_tokens_per_sec=0.0,
            last_call_at="",
            auto_disabled=False,
            disable_reason="",
        )
        self.gateway.providers.upsert(new)
        self.refresh_providers()
        # 选中复制出的新行
        for r in range(self.provider_table.rowCount()):
            if self.provider_table.item(r, 0).data(Qt.UserRole) == new.id:
                self.provider_table.selectRow(r)
                break

    def _unique_copy_name(self, base: str) -> str:
        """生成不重名的副本名称：「base 副本」，重名时依次追加 2、3…"""
        names = {q.name for q in self.gateway.providers.list()}
        name = f"{base} 副本"
        if name not in names:
            return name
        i = 2
        while f"{name} {i}" in names:
            i += 1
        return f"{name} {i}"

    def _on_delete_provider(self):
        p = self._selected_provider()
        if p is None:
            return
        ret = MessageBox.question(
            self, self.tr("确认", "Confirm"),
            self.tr(f"确定删除 Provider「{p.name}」？",
                    f'Delete provider "{p.name}"?'))
        if ret == QMessageBox.Yes:
            self.gateway.providers.delete(p.id)
            self._test_state.pop(p.id, None)
            self.refresh_providers()

    def _on_reset_quota(self):
        p = self._selected_provider()
        if p is None:
            return
        p.used_calls = 0
        p.used_tokens = 0
        p.auto_disabled = False
        p.disable_reason = ""
        self.gateway.providers.upsert(p)
        self.refresh_providers()

    def _on_move(self, delta: int):
        """上移(-1)/下移(+1)：调整 provider 列表顺序，即调度优先级。"""
        providers = self.gateway.providers.list()  # 已按 sort_order 排序
        row = self.provider_table.currentRow()
        if row < 0:
            MessageBox.information(
                self, self.tr("提示", "Notice"),
                self.tr("请先选择一行 Provider", "Please select a provider row first"))
            return
        target = row + delta
        if target < 0 or target >= len(providers):
            return  # 已在边界
        # 交换目标行后，把整列重排成连续序号(0,1,2...)，保证 sort_order 互不相同
        providers[row], providers[target] = providers[target], providers[row]
        for i, p in enumerate(providers):
            p.sort_order = i
            self.gateway.providers.upsert(p)
        self.refresh_providers()
        self.provider_table.selectRow(target)

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def refresh_logs(self):
        """同步刷新调用记录（用户主动操作 / 切换过滤后立即调用）。"""
        start, end = self._log_range()
        pid = self.log_provider_combo.currentData()  # None = 全部 provider
        logs = self.gateway.logs.list(
            limit=200, provider_id=pid, start=start, end=end)
        names = {p.id: p.name for p in self.gateway.providers.list()}
        st = self.gateway.logs.stats(provider_id=pid, start=start, end=end)
        self._render_logs(logs, names, st)

    def _render_logs(self, logs: list, names: dict, st: dict):
        tr = self.tr
        self._repopulate_provider_filter(names)
        self.log_table.setRowCount(len(logs))
        for r, lg in enumerate(logs):
            time_item = QTableWidgetItem(lg.created_at)
            time_item.setData(Qt.UserRole, lg.id)
            self.log_table.setItem(r, 0, time_item)
            self.log_table.setItem(r, 1, QTableWidgetItem(
                names.get(lg.provider_id, "-")))
            self.log_table.setItem(r, 2, QTableWidgetItem(lg.model))
            status_text = tr("失败", "Failed") if lg.status == "error" \
                else tr("成功", "Success")
            status_badge = StatusBadge(
                status_text,
                "danger" if lg.status == "error" else "success")
            self.log_table.setCellWidget(r, 3, status_badge)
            # 数字列右对齐；列序匹配表头：4 输入 / 5 输出 / 6 合计 / 7 速度(tok/s)
            for col, val in ((4, f"{lg.prompt_tokens:,}"),      # 输入
                             (5, f"{lg.completion_tokens:,}"),  # 输出
                             (6, f"{lg.total_tokens:,}"),       # 合计
                             (7, f"{lg.tokens_per_sec:.1f}")):  # 速度
                it = QTableWidgetItem(val)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.log_table.setItem(r, col, it)

            del_btn = QPushButton(tr("删除", "Delete"))
            del_btn.setProperty("class", "row-action")
            del_btn.clicked.connect(
                lambda _=False, lid=lg.id: self._delete_log(lid))
            del_widget = QWidget()
            dl = QHBoxLayout(del_widget)
            dl.setContentsMargins(0, 0, 0, 0)
            dl.setAlignment(Qt.AlignVCenter)
            dl.addWidget(del_btn)
            self.log_table.setCellWidget(r, 8, del_widget)

        self._refresh_log_stats(st)

    def _log_range(self):
        """把日期下拉的预设换算成 (start, end) 时间边界。

        created_at 为 'YYYY-MM-DD HH:MM:SS' 字符串，按字符串比较即可。
        """
        key = self.log_date_combo.currentData()
        today = datetime.date.today()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if key == "all":
            return None, None
        if key == "today":
            return today.strftime("%Y-%m-%d 00:00:00"), now
        if key == "yesterday":
            d = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            return f"{d} 00:00:00", f"{d} 23:59:59"
        if key == "7d":
            return (today - datetime.timedelta(days=6)).strftime(
                "%Y-%m-%d 00:00:00"), now
        if key == "week":
            d = (today - datetime.timedelta(
                days=today.weekday())).strftime("%Y-%m-%d")
            return f"{d} 00:00:00", now
        if key == "month":
            return today.replace(day=1).strftime("%Y-%m-%d 00:00:00"), now
        return None, None

    def _repopulate_provider_filter(self, names: dict) -> None:
        """同步 provider 过滤下拉与 provider 列表，并保持当前选中项。

        provider 集合未变化时直接跳过，避免高频刷新（3 秒自动刷新 / 日志变化）
        反复 clear() 下拉框，把正在展开的下拉强制关闭、难以选择。
        """
        sig = tuple(sorted(names.items()))
        if sig == self._provider_filter_sig:
            return
        self._provider_filter_sig = sig
        current = self.log_provider_combo.currentData()
        self.log_provider_combo.blockSignals(True)
        self.log_provider_combo.clear()
        self.log_provider_combo.addItem(self.tr("全部", "All"), None)
        for pid_, name in names.items():
            self.log_provider_combo.addItem(name, pid_)
        idx = self.log_provider_combo.findData(current)
        self.log_provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.log_provider_combo.blockSignals(False)

    def _refresh_log_stats(self, st: dict):
        """渲染统计行：展示给定统计结果（查询已由调用方完成）。"""
        scope = (f"{self.log_date_combo.currentText()} · "
                 f"{self.log_provider_combo.currentText()}")
        if LanguageManager().is_zh():
            self.log_stats_label.setText(
                f"统计（{scope}）：调用 {st['calls']:,} 次　"
                f"成功 {st['successes']:,}　"
                f"失败 {st['errors']:,}　"
                f"输入 {st['prompt_tokens']:,} tok　"
                f"输出 {st['completion_tokens']:,} tok　"
                f"合计 {st['total_tokens']:,} tok")
        else:
            self.log_stats_label.setText(
                f"Stats ({scope}): {st['calls']:,} calls · "
                f"{st['successes']:,} ok · {st['errors']:,} failed · "
                f"{st['prompt_tokens']:,} input tok · "
                f"{st['completion_tokens']:,} output tok · "
                f"{st['total_tokens']:,} total tok")

    def _delete_log(self, log_id):
        self.gateway.logs.delete(log_id)
        self.refresh_logs()

    def _on_clear_logs(self):
        ret = MessageBox.question(
            self, self.tr("确认", "Confirm"),
            self.tr("确定清空全部调用记录吗？", "Clear all call logs?"))
        if ret == QMessageBox.Yes:
            self.gateway.logs.clear_all()
            self.refresh_logs()