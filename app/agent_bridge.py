"""智能体框架桥接：让「对话」页以真正的 SuperAgent 编排问答。

本模块在 LightAIBox 的进程内网关（Gateway：调度 / 配额 / 容灾 / 多模态路由）之上，
通过 `AgentChatSession` 接入外部 LightAgents（../LightAgents）的 SuperAgent 编排层——
意图路由 → 子代理分发 → 工具发现 → 流式汇总，底层 LLM 调用全部走 Gateway 自适应调度。
流式产物经 token / THINK_TAG / TOOL_TAG 带外通道原样上抛，由 ChatPage 解析渲染。

编排与协议适配实现在 app/gateway_llm.py（GatewayLLM + StreamingSuperAgent）：
- GatewayLLM：鸭子类型实现 LightAgents 的 LLM 接口面，内部持有 Gateway，每次调用重新调度。
- StreamingSuperAgent：复用 SuperAgent 的意图路由 + 子代理工厂 + 工具发现，但子代理走
  ReActAgent.arun_stream 真逐 token 流式，汇总阶段逐 token 回传。

本文件保留 `chat_stream(messages)` 这一「同步生成器」契约（_ChatWorker 依赖它），
内部用后台 asyncio 线程桥接 `StreamingSuperAgent.arun_stream` 的异步流式。
"""
import json
import os
import datetime
import random
from typing import Any, Generator, List, Optional

from .client import THINK_TAG, THINK_END_TAG, Message
from .gateway import Gateway

# 工具活动标记：与 THINK_TAG 同属「带外信号」，用控制字符包裹一段 JSON，
# 经同一 token 通道上抛，由 ChatPage 解析后渲染为状态行。
TOOL_TAG = "\x01tool\x01"
TOOL_END_TAG = "\x01/tool\x01"


def _latest_user_text(messages: List[Message]) -> str:
    """从会话历史里取最新的 user 文本（含多模态块中的 text 部分）。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") in ("text", "input_text")
                     and c.get("text")]
            if parts:
                return " ".join(parts).strip()
    return ""


def _tool_event(kind: str, **fields) -> str:
    """把一条工具事件包成 TOOL_TAG...TOOL_END_TAG 段（kind=call/result）。"""
    payload = {"kind": kind}
    payload.update(fields)
    return f"{TOOL_TAG}{json.dumps(payload, ensure_ascii=False)}{TOOL_END_TAG}"


class AgentChatSession:
    """承载「智能体模式」下单轮问答的编排会话。

    对外暴露 `chat_stream(messages)`，与普通客户端一致：messages 为 LightAIBox 通用
    dict 列表（含 system / 多模态内容块），yield 文本增量（思考段 THINK_TAG、工具活
    动段 TOOL_TAG 带外透传），StopIteration.value 为 ClientResult。由 ChatPage 的
    _ChatWorker 原样消费。

    实现：接入 LightAgents 的 SuperAgent 编排（StreamingSuperAgent）——意图路由 +
    子代理分发 + 工具发现 + 流式汇总，底层 LLM 走 Gateway 自适应调度。为兼容
    _ChatWorker 的「同步生成器」消费方式，用后台 asyncio 线程桥接异步流式。
    """

    def __init__(self, gateway: Gateway, provider_id: Optional[int] = None,
                 model: str = "", max_steps: int = 5, approval=None):
        self.gateway = gateway
        self.provider_id = provider_id
        self.model = model
        self.max_steps = max_steps
        # 运行时「写文件」授权协调器（app/approval.ApprovalCoordinator）。为 None 时
        # 只注册只读工具，智能体无法产出文件。
        self.approval = approval

    @staticmethod
    def _new_session_dir() -> str:
        """计算并创建一个会话专用沙箱目录：<OUTPUT_ROOT>/<YYYY-MM-DD>/<HHMMSS>_<4hex>。"""
        from .config import OUTPUT_ROOT
        now = datetime.datetime.now()
        stamp = now.strftime("%H%M%S") + "_" + f"{random.randrange(1 << 16):04x}"
        base = os.path.join(OUTPUT_ROOT, now.strftime("%Y-%m-%d"))
        session_dir = os.path.join(base, stamp)
        os.makedirs(session_dir, exist_ok=True)
        return session_dir

    # ------------------------------------------------------------------ #
    def chat_stream(self, messages: List[Message],
                    **kwargs) -> Generator[str, None, Any]:
        """驱动一次编排：逐段 yield 文本 / 思考 / 工具活动，return ClientResult。"""
        from .models import ClientResult
        import time as _time
        import queue as _queue
        import threading as _threading

        # 从会话历史里提取最新的 user 文本作为编排输入（SuperAgent 发力在单任务；
        # 历史前文作为上下文注入系统提示）。多模态/历史细节在首版忽略。
        user_text = _latest_user_text(messages)

        # 构建 GatewayLLM -> StreamingSuperAgent（工具：只读桌面工具 + FindTools）
        from .gateway_llm import (GatewayLLM, StreamingSuperAgent,
                                  build_desktop_registry)
        llm = GatewayLLM(self.gateway, provider_id=self.provider_id)
        if self.approval is not None:
            session_dir = self._new_session_dir()
        else:
            session_dir = None
        registry, catalog = build_desktop_registry(
            self.gateway, provider_id=self.provider_id,
            session_dir=session_dir, approval=self.approval)
        agent = StreamingSuperAgent(llm, tool_registry=registry,
                                    catalog=catalog, config=None,
                                    max_steps=self.max_steps)

        start = _time.perf_counter()
        q: "_queue.Queue[Optional[str]]" = _queue.Queue()
        err_box: list = []

        def _producer():
            """在独立 asyncio 事件循环里消费异步流式，把片段塞入队列。"""
            try:
                import asyncio as _asyncio

                async def _drive():
                    async for piece in agent.arun_stream(user_text, **kwargs):
                        q.put(piece)

                _asyncio.run(_drive())
            except Exception as exc:  # noqa: BLE001
                err_box.append(exc)
            finally:
                q.put(None)

        t = _threading.Thread(target=_producer, daemon=True)
        t.start()

        collected_text: List[str] = []
        while True:
            piece = q.get()
            if piece is None:
                break
            if piece:
                collected_text.append(piece)
                yield piece
        if err_box:
            exc = err_box[0]
            msg = str(getattr(exc, "args", ("",)) and exc.args[0] or exc)
            raise RuntimeError(f"智能体编排失败：{msg}") from exc

        t.join(timeout=5)
        # 返回的正文剥离带外标记段（THINK/TOOL），与 UI 侧 _on_token 的解析一致；
        # 直播期间仍逐段上抛原始 piece（含 tag），由 ChatPage 负责渲染。
        content = "".join(collected_text)
        # 注意：循环变量不能用 start/end——生成器体在开头用 start 记录了耗时起点，
        # 若被这里的标记串覆盖，下方 elapsed_ms 会拿「字符串」做减法（float - str）。
        for open_tag, close_tag in ((THINK_TAG, THINK_END_TAG),
                                    (TOOL_TAG, TOOL_END_TAG)):
            cleaned, rest = [], content
            while open_tag in rest:
                before, _, rest = rest.partition(open_tag)
                cleaned.append(before)
                _, _, rest = rest.partition(close_tag)
            cleaned.append(rest)
            content = "".join(cleaned)
        elapsed_ms = int((_time.perf_counter() - start) * 1000)
        return ClientResult(
            content=content.strip(),
            prompt_tokens=0,
            completion_tokens=0,
            elapsed_ms=elapsed_ms,
            tokens_per_sec=0.0,
            blocks=[],
            stop_reason="end_turn",
        )
