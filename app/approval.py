"""跨线程「运行时可写授权」协调器。

智能体模式的文件写入（app/file_tools.py）与 SSH/浏览器等工具跑在后台线程里，无法直接
弹 GUI 确认框。ApprovalCoordinator 用 Qt Signal 把一次「执行前/写前确认」投递到主线程：

    工具线程 await_approval(proposal)  →  emit approval_requested（跨线程 Queued）→ 阻塞等 Event
    主线程   _on_approval_requested      →  以气泡内「确认/取消」条渲染（非弹窗）→ 用户点按钮后
                                             resolve(proposal, ok) 写回 + set

两线程互不占用对方事件循环，无死锁；await 超时（默认 120s）按「拒绝」处理，防卡死。
如何呈现（气泡内按钮条 / 弹窗）由连接 approval_requested 的 UI 侧自行决定。
"""
import threading
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal


class ApprovalCoordinator(QObject):
    """持有一次待确认授权的协调器。实例须创建于主线程，并连接 approval_requested。"""

    # 每次请求携带一个 dict proposal：{path, kind, size, display,...}，主线程据此弹窗
    approval_requested = Signal(object)

    def __init__(self, parent: Optional[QObject] = None, timeout: float = 120.0):
        super().__init__(parent)
        self.timeout = timeout

    def await_approval(self, proposal: Dict) -> bool:
        """（工具线程）发起一次授权申请，阻塞直到用户在主线程作出决定或超时。

        返回 True 表示已授权可执行，否则为拒绝。proposal 会被就地补充 _event 用于唤醒。
        """
        ev = threading.Event()
        proposal["_event"] = ev
        proposal.setdefault("approved", False)
        self.approval_requested.emit(proposal)
        return ev.wait(self.timeout) and bool(proposal.get("approved"))

    def resolve(self, proposal: Dict, ok: bool) -> None:
        """（主线程槽）把用户决定写回 proposal 并唤醒等待中的工具线程。"""
        proposal["approved"] = bool(ok)
        ev = proposal.get("_event")
        if ev is not None:
            ev.set()
