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


# 智能体「结构化步骤」的流式带外标记（与 THINK/TOOL 同通道、原子整段上抛），
# 让 ChatPage 在**流式过程中**就把各子任务 / 各步骤的回答与步骤放在一起，
# 而不必等整轮结束才重排。三类开事件后跟 JSON 并以 \x01 收尾：
#   SUB_OPEN  + {index, agent_type} + \x01   开子任务块
#   STEP_OPEN + {n} + \x01                   开某一步（新一步自动收尾上一步）
#   SUB_ANSWER + {answer} + \x01             子任务的最终答案
#   SUB_END   收尾当前子任务（把它落入已完成的 blocks，后续文本归「汇总」）
SUB_OPEN = "\x01sub\x01"
STEP_OPEN = "\x01step\x01"
SUB_ANSWER = "\x01sans\x01"
SUB_END = "\x01/sub\x01"


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
        # —— 跨轮复用状态（同一 AgentChatSession 实例连跑多轮时共享）————— #
        # 工具注册表 / 目录沿会话生命周期复用：目录的「激活」状态（如 FindTools 激活的
        # 天气、用户已授权启用的工具）在轮次间保持，避免每轮重建后丢掉激活、迫使模型
        # 反复发现工具。FileTools 的沙箱目录也一并复用（同一会话同一工作目录）。
        self._registry = None
        self._catalog = None
        self._session_dir: Optional[str] = None
        # 历史上下文：记录每一轮的「用户问题 → 助手回答」，在下一轮注入系统提示，
        # 让模型记得前文（Fix 1：跨轮上下文关联的关键）。
        self._history: List[dict] = []

    def reset_context(self) -> None:
        """清空跨轮记忆（新建/切换会话、清理上下文时调用），不重建工具目录。"""
        self._history = []

    def shutdown(self) -> None:
        """释放工具注册表里持有外部资源的工具（当前仅浏览器）。

        由 ChatPage 在窗口 / 进程退出前调用：让 BrowserTool 在解释器仍正常、事件循环
        线程仍存活时**主动**脱离 Playwright 监管，从而保留桌面 Chrome——避免关程序时
        GC 被动 close 连带杀掉用户正看着的浏览器窗口。
        """
        if self._registry is None:
            return
        from .gateway_llm import close_registry_tools
        close_registry_tools(self._registry)

    def _ensure_run(self):
        """惰性构建并**复用**桌面工具注册表/目录与编排器。

        返回 (llm, agent)。llm 每轮新建（承载本轮 token 计量）；registry/catalog/
        沙箱目录只建一次并沿 `chat_stream` 多轮复用，使工具「激活状态」跨轮保持。
        """
        from .gateway_llm import (GatewayLLM, StreamingSuperAgent,
                                  build_desktop_registry)
        llm = GatewayLLM(self.gateway, provider_id=self.provider_id)
        if self._registry is None:
            if self.approval is not None and self._session_dir is None:
                self._session_dir = self._new_session_dir()
            self._registry, self._catalog = build_desktop_registry(
                self.gateway, provider_id=self.provider_id,
                session_dir=self._session_dir, approval=self.approval)
        agent = StreamingSuperAgent(llm, tool_registry=self._registry,
                                    catalog=self._catalog, config=None,
                                    max_steps=self.max_steps)
        return llm, agent

    def _build_history_context(self) -> str:
        """把前几轮的用户问题与助手回答压成一段「此前对话」文本（Fix 1）。

        仅保留最近若干轮、每段截断，避免历史无限膨胀挤占上下文预算；模型据此
        关联前文（如延续上一个提问里查过的城市天气）。
        """
        if not self._history:
            return ""
        turns = self._history[-6:]  # 只保留最近 6 轮
        lines = ["【此前对话记录，供你结合上下文回答，无需重复已给结论】"]
        for t in turns:
            u = (t.get("user") or "").strip()
            a = (t.get("answer") or "").strip()
            if u:
                lines.append("用户：" + (u[:600]))
            if a:
                lines.append("助手：" + (a[:900]))
        return "\n".join(lines)

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

        # 从会话历史里提取最新的 user 文本作为编排输入（SuperAgent 发力在单任务）。
        # 多模态细节在首版忽略；历史前文（前几轮问答）经 _build_history_context
        # 注入系统提示，实现跨轮上下文关联（Fix 1）。
        user_text = _latest_user_text(messages)
        history_context = self._build_history_context()

        # 复用（而非每轮重建）桌面工具注册表/目录与编排器：工具「激活状态」跨轮保持
        # （Fix 3），同会话的工作目录也保持，无需用户重复授权已启用的工具。
        llm, agent = self._ensure_run()

        start = _time.perf_counter()
        q: "_queue.Queue[Optional[str]]" = _queue.Queue()
        err_box: list = []

        def _producer():
            """在独立 asyncio 事件循环里消费异步流式，把片段塞入队列。"""
            try:
                import asyncio as _asyncio

                async def _drive():
                    async for piece in agent.arun_stream(
                            user_text, history_context=history_context, **kwargs):
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

        # 把本轮「问题 → 回答」记入跨轮记忆，供下一轮作为此前对话注入（Fix 1）。
        # 仅记录最终正文（已剥离 THINK/TOOL 标记），避免过程噪音污染后续上下文。
        if user_text:
            self._history.append({"user": user_text, "answer": content.strip()})

        # 整轮 token 用量：各子代理每步 + 最终汇总的所有底层 LLM 调用累加而来
        #（GatewayLLM 在本轮 chat_stream 内累计；此前硬编码 0 导致 UI 显示 0 token）。
        prompt_tokens = llm.total_prompt_tokens
        completion_tokens = llm.total_completion_tokens
        total_tokens = prompt_tokens + completion_tokens
        return ClientResult(
            content=content.strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            tokens_per_sec=(
                total_tokens / (elapsed_ms / 1000.0) if elapsed_ms > 0 else 0.0),
            # 整轮底层 LLM 调用次数（各 ReAct 步 + 汇总），供 UI 解释总 token 构成
            calls=getattr(llm, "total_calls", 0),
            blocks=[],
            stop_reason="end_turn",
            # 智能体结构化产出（子任务 → 各步骤 → 文本/工具），供 UI 分块渲染
            agent_blocks=getattr(agent, "blocks", list()),
        )
