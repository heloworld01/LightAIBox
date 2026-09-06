"""GatewayLLM：把 LightAIBox 的多 provider 网关伪装成 LightAgents 期望的单模型 LLM。

背景
----
SuperAgent / ReActAgent 等 LightAgents 编排器依赖一个 `LightAgentsLLM` 实例，它
单次绑定「一个 model + 一个 api_key + 一个 base_url」，并暴露以下接口：

- ``invoke(messages) -> LLMResponse``
- ``invoke_with_tools(messages, tools, tool_choice) -> LLMToolResponse``
- ``astream_invoke_with_tools(...) -> AsyncIterator[dict]``（逐 token 流式 + 工具）

而 LightAIBox 的 ``Gateway`` 是「无固定单模型，按策略 / 配额 / 容灾在多个 provider
间自适应调度」，且底层同时支持 OpenAI 与 Anthropic 双协议。本模块写一个鸭子类型
适配器 ``GatewayLLM``：不继承任何类，只实现上面三个接口 + 少量被 Agent 基类读取的
属性，内部把每次调用转成 ``Gateway`` 的对应调用，从而复用网关的调度 / 配额 / 记录。

协议口径
--------
LightAgents 的内部消息一律是「OpenAI 风格」：system 是角色消息、assistant 的
工具调用在 ``tool_calls`` 字段、工具结果用 ``role="tool"``。当 Gateway 实际命中
Anthropic provider 时，这些消息与工具定义需要翻译成 Anthropic 协议（system 顶层、
tool_use / tool_result 内容块）。翻译逻辑与 LightAgents 的 ``AnthropicAdapter`` 同行，
故这里内联一份等价实现（不 import 其私有方法，避免耦合其网络层）。

首版约束
--------
- ``astream_invoke_with_tools`` 的「逐 token」由「一次非流式上游调用 + 本地分片
  吐出」实现：上游 ``Gateway.chat_with_provider`` 返回完整 ``ClientResult``（含
  tool_use blocks），这里把 content 按块切片逐步 yield，再一次性 yield done 事件。
  要真正按字节从上游逐 token 流式，需 Gateway 补一个「流式 + 工具 + 锁 provider」
  接口（gateway.chat_with_provider_stream），留待后续。
"""
from __future__ import annotations

import json
import sys
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from . import config
from .gateway import Gateway


# --------------------------------------------------------------------------- #
# LightAgents 引导 import
# --------------------------------------------------------------------------- #
def _ensure_lightagents() -> None:
    """把外部源码库 LightAgents 加入 sys.path（开发态），使其可被 import。

    打包态下 light_agents 已被 lightaibox.spec 的 _vendor_datas 拷入包根，
    ``import light_agents`` 可直接解析，本函数为 no-op。开发态（源码运行）下
    LightAgents 位于 ../LightAgents，需要显式加进 sys.path。
    """
    try:
        import light_agents  # noqa: F401
        return
    except ImportError:
        pass
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.abspath(os.path.join(here, "..", "..", "LightAgents")),
        os.path.abspath(os.path.join(here, "..", "LightAgents")),
    ]
    for cand in candidates:
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)


def _skills_dir() -> Optional[str]:
    """定位 LightAgents 的技能目录（含 <name>/SKILL.md 的父目录）。

    开发态：外部源码库 D:\\project\\LightAgents\\skills；打包态：随包拷贝进
    sys._MEIPASS 下的 skills/（见 lightaibox.spec 的 _vendor_datas）。找不到返回 None。
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))  # LightAIBox/app
    candidates = [
        os.path.abspath(os.path.join(here, "..", "..", "LightAgents", "skills")),
        os.path.abspath(os.path.join(here, "..", "LightAgents", "skills")),
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "skills"))
        candidates.append(os.path.join(meipass, "light_agents", "skills"))
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return None


def _LightAgentsLLM_types():
    """懒加载 LightAgents 的类型（LLMResponse / LLMToolResponse / ToolCall）。"""
    _ensure_lightagents()
    from light_agents.core.llm_response import (  # noqa: F401
        LLMResponse, LLMToolResponse, ToolCall,
    )
    return LLMResponse, LLMToolResponse, ToolCall


# --------------------------------------------------------------------------- #
# OpenAI 风格消息 <-> Anthropic 协议翻译（与 LightAgents AnthropicAdapter 同口径）
# --------------------------------------------------------------------------- #
def _assistant_block_messages(messages: List[Dict]) -> List[Dict]:
    """把 OpenAI 风格 assistant 的 tool_calls 翻译为 Anthropic 的 tool_use 内容块。"""
    out: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            continue
        tool_calls = m.get("tool_calls")
        if not tool_calls:
            out.append(m)
            continue
        blocks: List[Dict[str, Any]] = []
        text = m.get("content")
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            fn = (tc.get("function") if isinstance(tc, dict) else None) or {}
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except (ValueError, TypeError):
                    args = {"_raw": args}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", "") if isinstance(tc, dict) else "",
                "name": fn.get("name", ""),
                "input": args,
            })
        out.append({"role": "assistant", "content": blocks})
    return out


def _tool_result_content(messages: List[Dict]) -> List[Dict]:
    """把 OpenAI 风格 role="tool" 折叠为 Anthropic 的 user + tool_result 内容块。"""
    out: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    def _flush():
        if pending:
            out.append({
                "role": "user",
                "content": pending if len(pending) > 1 else pending[0],
            })
            pending.clear()

    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            pending.append({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            })
        else:
            _flush()
            out.append(m)
    _flush()
    return out


def _to_anthropic_messages(messages: List[Dict]) -> tuple:
    """把 OpenAI 风格消息翻译为 (system, Anthropic 消息列表)。"""
    system_parts: List[str] = []
    anthro: List[Dict[str, Any]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            else:
                system_parts.append("".join(
                    str(b.get("text", "")) for b in content
                    if isinstance(b, dict) and b.get("type") == "text"))
            continue
        if role == "assistant":
            arole = "assistant"
        elif role == "user":
            arole = "user"
        else:
            arole = "user"

        if isinstance(content, list):
            translated: List[Dict[str, Any]] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt in ("image_url", "input_image"):
                    inner = b.get("image_url") or b.get("input_image") or {}
                    url = inner.get("url", "") if isinstance(inner, dict) else ""
                    translated.append(_image_url_to_block(url))
                else:
                    translated.append(b)
            content = translated

        if (anthro and anthro[-1]["role"] == arole
                and isinstance(anthro[-1]["content"], str)
                and isinstance(content, str)):
            anthro[-1]["content"] = anthro[-1]["content"] + "\n" + content
        else:
            anthro.append({"role": arole, "content": content})

    return ("\n".join(p for p in system_parts if p) or None), anthro


def _image_url_to_block(url: str) -> Dict[str, Any]:
    if url.startswith("data:"):
        head, _, payload = url.partition(",")
        mime = head[len("data:"):].split(";", 1)[0] or "image/png"
        return {"type": "image", "source": {"type": "base64",
                                            "media_type": mime, "data": payload}}
    return {"type": "image", "source": {"type": "base64",
                                        "media_type": "image/png", "data": url}}


def _fill_tool_schemas(tools: List[Dict]) -> List[Dict]:
    """OpenAI 风格 tools -> Anthropic tools 定义。"""
    anthro: List[Dict[str, Any]] = []
    for t in tools or []:
        fn = (t.get("function") if isinstance(t, dict) else None) or {}
        name = fn.get("name")
        if not name:
            continue
        anthro.append({
            "name": name,
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object"},
        })
    return anthro


# --------------------------------------------------------------------------- #
# GatewayLLM：把 Gateway 伪装成 LightAgents 的 LLM
# --------------------------------------------------------------------------- #
class GatewayLLM:
    """鸭子类型兼容 ``LightAgentsLLM`` 的网关适配器。

    不继承 ``LightAgentsLLM``，仅实现其被 Agent 消费的接口面：``invoke`` /
    ``invoke_with_tools`` / ``astream_invoke_with_tools``，以及 ``model`` 等
    只读属性。每次调用都让 Gateway 自适应调度（用户决策：子代理全部走自适应）。
    """

    def __init__(self, gateway: Gateway, provider_id: Optional[int] = None,
                 temperature: float = 0.7, max_tokens: Optional[int] = None):
        self.gateway = gateway
        self.provider_id = provider_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.model = ""          # 由 Gateway 自适应调度决定，这里不固定
        self.provider = "gateway"

    # -- 内部：按协议把消息喂给 Gateway ------------------------------------- #
    def _invoke(self, messages: List[Dict], tools: Optional[List[Dict]] = None,
                **kwargs):
        """带工具时走 chat_with_provider（返回 (result, pid, proto)）；否则 chat。"""
        if tools:
            need_proto = self.gateway.effective_protocol(self.provider_id) \
                if self.provider_id is not None else None
            # 首轮未知协议，默认按 OpenAI 传 schema；拿到结果后再随协议锁定纠偏。
            # chat_with_provider 会按实际命中 provider 的协议自行翻译 tools？
            #   不——Gateway 的 client.chat 把 tools 原样透传，需在此按协议翻译。
            result, pid, proto = self.gateway.chat_with_provider(
                messages, provider_id=self.provider_id,
                tools=tools, tool_choice="auto", **kwargs)
            return result, pid, proto
        result = self.gateway.chat(
            messages, provider_id=self.provider_id, **kwargs)
        return result, self.provider_id, None

    # -- LLMResponse / LLMToolResponse 构造 --------------------------------- #
    @staticmethod
    def _usage(result) -> Dict[str, int]:
        return {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        }

    def invoke(self, messages: List[Dict[str, str]], **kwargs) -> Any:
        """非流式调用，返回 LLMResponse。"""
        LLMResponse, _, _ = _LightAgentsLLM_types()
        result, _, _ = self._invoke(messages, **kwargs)
        return LLMResponse(
            content=result.content or "",
            model=result.model if hasattr(result, "model") else "",
            usage=self._usage(result),
            latency_ms=result.elapsed_ms,
        )

    def invoke_with_tools(self, messages: List[Dict], tools: List[Dict],
                          tool_choice: Union[str, Dict] = "auto", **kwargs) -> Any:
        """带工具的调用，返回 LLMToolResponse。"""
        _, LLMToolResponse, ToolCall = _LightAgentsLLM_types()
        # 先按 OpenAI 口径请求一次；命中的 provider 若是 Anthropic，需翻译消息/schema。
        # 简化：先探测协议——若 provider_id 已锁定则直接按其协议构造；否则先按
        # OpenAI 试，若报 protocol 错再降级为 Anthropic（首版够用，错误路径少见）。
        proto = self.gateway.effective_protocol(self.provider_id) \
            if self.provider_id is not None else None

        if proto == config.API_ANTHROPIC:
            msgs = _tool_result_content(_assistant_block_messages(messages))
            result, _, _ = self._invoke(
                msgs, tools=_fill_tool_schemas(tools), **kwargs)
        else:
            result, _, _ = self._invoke(messages, tools=tools, **kwargs)

        tool_calls = [
            ToolCall(id=b.tool_id, name=b.tool_name,
                     arguments=json.dumps(b.tool_input or {}, ensure_ascii=False))
            for b in result.blocks if b.type == "tool_use"
        ]
        return LLMToolResponse(
            content=result.content or None,
            tool_calls=tool_calls,
            model=getattr(result, "model", "") or "",
            usage=self._usage(result),
            latency_ms=result.elapsed_ms,
        )

    async def astream_invoke_with_tools(
        self, messages: List[Dict], tools: List[Dict],
        tool_choice: Union[str, Dict] = "auto", **kwargs,
    ) -> AsyncIterator[dict]:
        """异步工具调用流式：优先真实上游流式，失败回退到本地分片。

        事件协议（与 LightAgents 一致）：
        - {"type": "content", "text": <增量文本>}
        - {"type": "done", "content": <全文>, "tool_calls": [ToolCall, ...]}

        真实流式：复用各 provider 的 chat_stream（逐 token 上抛文本增量，结束时回
        组装 tool_use/tool_calls 块；OpenAI 由 chat_stream 拼接分片，Anthropic 由
        get_final_message 带回）。若流式带工具有任何异常（协议/网络/CD 边角），回退
        到「buffered _invoke + 本地切片」，保证不回归。
        """
        _, _, ToolCall = _LightAgentsLLM_types()
        proto = self.gateway.effective_protocol(self.provider_id) \
            if self.provider_id is not None else None
        try:
            # ---- 真实流式（带工具） ----
            if proto == config.API_ANTHROPIC:
                msgs = _tool_result_content(_assistant_block_messages(messages))
                tools_kw = _fill_tool_schemas(tools)
                tc_kw = {}
            else:
                msgs = messages
                tools_kw = tools
                tc_kw = {"tool_choice": tool_choice}
            it = self.gateway.chat_with_provider_stream(
                msgs, provider_id=self.provider_id,
                tools=tools_kw, **tc_kw, **kwargs)
            result = None
            text: List[str] = []
            while True:
                try:
                    piece = it.send(None)
                except StopIteration as e:
                    result, _pid, _rproto = e.value
                    break
                if piece:
                    text.append(piece)
                    yield {"type": "content", "text": piece}
            content = result.content or "".join(text)
            tool_calls = [
                ToolCall(id=b.tool_id, name=b.tool_name,
                         arguments=json.dumps(b.tool_input or {}, ensure_ascii=False))
                for b in (result.blocks or []) if b.type == "tool_use"
            ]
            yield {"type": "done", "content": content, "tool_calls": tool_calls}
            return
        except Exception:
            # 回退到 buffered 实现（保留原逻辑，保证不回归）
            pass

        # ---- buffered 回退 ----
        if proto == config.API_ANTHROPIC:
            msgs = _tool_result_content(_assistant_block_messages(messages))
            result, _, _ = self._invoke(
                msgs, tools=_fill_tool_schemas(tools), **kwargs)
        else:
            result, _, _ = self._invoke(messages, tools=tools, **kwargs)

        content = result.content or ""
        if content:
            # 本地按块切片，模拟逐字增量（避免每次只吐一个字符过频，按行/块吐）。
            # 块间加微小间隔，让伪造流式以自然节奏涌现而非瞬间硬爆发。真实流式
            # 成功后此回退路径很少走到。
            import asyncio as _asyncio
            chunk = 0
            step = max(1, 120)
            while chunk < len(content):
                yield {"type": "content", "text": content[chunk:chunk + step]}
                chunk += step
                await _asyncio.sleep(0.01)

        tool_calls = [
            ToolCall(id=b.tool_id, name=b.tool_name,
                     arguments=json.dumps(b.tool_input or {}, ensure_ascii=False))
            for b in (result.blocks or []) if b.type == "tool_use"
        ]
        yield {"type": "done", "content": content, "tool_calls": tool_calls}

    # -- 纯文本流式（无工具；Simple/Plan/Reflection 的 astream_invoke 路径） --- #
    def stream_invoke(self, messages: List[Dict], **kwargs):
        """同步纯文本流式，yield 文本增量（真逐 token，直连 Gateway.chat_stream）。"""
        proto = self.gateway.effective_protocol(self.provider_id) \
            if self.provider_id is not None else None
        msgs = messages
        if proto == config.API_ANTHROPIC:
            msgs = _tool_result_content(_assistant_block_messages(messages))
        # Gateway.chat_stream 返回生成器，结束经 StopIteration.value 携带 ClientResult
        it = self.gateway.chat_stream(msgs, provider_id=self.provider_id, **kwargs)
        try:
            while True:
                try:
                    piece = next(it)
                except StopIteration:
                    break
                if piece:
                    yield piece
        finally:
            try:
                it.close()
            except Exception:
                pass

    async def astream_invoke(self, messages: List[Dict], **kwargs):
        """异步纯文本流式：把同步 stream_invoke 包进线程池桥接。

        SimpleAgent/PlanSolve/Reflection 的 arun_stream 走此路径，逐 token 吐字。
        """
        import asyncio
        loop = asyncio.get_running_loop()

        queue: asyncio.Queue = asyncio.Queue()

        def _produce():
            try:
                for chunk in self.stream_invoke(messages, **kwargs):
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(queue.put(e), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        loop.run_in_executor(None, _produce)
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item


# --------------------------------------------------------------------------- #
# StreamingSuperAgent：流式复用 SuperAgent 的编排（意图路由 + 子代理 + 汇总）
# --------------------------------------------------------------------------- #
# 与 LightAgents SuperAgent 的 _route_intent 同口径的启发式拆解。
def _route_intent(input_text: str) -> List[Dict[str, str]]:
    text = input_text.strip()
    has_compare = ("对比" in text or "比较" in text or " vs " in text.lower())
    has_plan = ("规划" in text or "方案" in text or "计划" in text or "分析" in text)
    has_report = ("报告" in text or "写" in text or "生成" in text)

    tasks: List[Dict[str, str]] = []
    if has_compare and ("和" in text or "与" in text or "、" in text):
        tasks.append({"task": "对请求中涉及的对象做逐项对比分析，给出异同结论。",
                      "agent_type": "reflection"})
    elif has_plan:
        tasks.append({"task": f"把下面的问题拆解成步骤并给出可执行方案：{text}",
                      "agent_type": "plan"})
    elif has_report:
        tasks.append({"task": f"基于以下需求产出结构化内容/报告：{text}",
                      "agent_type": "react"})
    else:
        tasks.append({"task": text, "agent_type": "react"})
    return tasks


class _ActiveScopedRegistry:
    """按 catalog 激活状态裁剪的工具注册表视图（懒加载的核心）。

    子代理（ReAct / Reflection / PlanSolve）都通过 ``tool_registry.get_all_tools()``
    构建一轮工具的 JSON Schema、``get_tool()`` 执行工具。本类包裹真实 registry，
    只把 catalog 中 ``active=True`` 的工具暴露给子代理，从而把 LightAgents 原生的
    「FindTools 发现 → 激活 → 下一轮注入 schema」懒加载语义接进 StreamingSuperAgent
    直接驱动的子代理循环。

    关键：子代理 ``arun_stream`` 的 while 循环**每轮都重新调用 _build_tool_schemas()**，
    因此 activate 发生后，下一轮循环自然拿到新工具，无需重跑子任务。

    其余方法/属性（register_tool、_functions、list_tools、read_metadata_cache、
    circuit_breaker 等）经 ``__getattr__`` 透传到真实 registry，保持基类 Agent
    初始化（注册 SkillTool 等）与其它路径不受影响。
    """

    def __init__(self, base, catalog):
        self._base = base
        self._catalog = catalog

    def _catalog_active(self):
        return set(self._catalog.active_names()) if self._catalog is not None else set()

    def _catalog_known(self):
        return set(self._catalog.list_all()) if self._catalog is not None else set()

    def _is_visible(self, name):
        # 可见性判据：catalog 里明确登记为「未激活」（懒加载）的工具才隐藏；
        # 已激活的、以及压根没在 catalog 登记的（如 SkillTool 的 "Skill"）一律可见，
        # 避免把「注册了但没登记目录」的常驻工具误伤。
        known = self._catalog_known()
        if name in known:
            return name in self._catalog_active()
        return True

    def get_all_tools(self):
        return [t for t in self._base.get_all_tools() if self._is_visible(t.name)]

    def get_tool(self, name):
        return self._base.get_tool(name) if self._is_visible(name) else None

    def get_function(self, name):
        # 函数工具量少且通常常驻；原样透传，保留与 build_tool_schemas 的一致性。
        return self._base.get_function(name)

    def __getattr__(self, name):
        # 仅当本类未显式定义该方法/属性时触发；透传真实 registry 的其余接口。
        return getattr(self._base, name)


class StreamingSuperAgent:
    """流式超级智能体编排器：复用 LightAgents 的意图路由与子代理，逐 token 回传。

    与 SuperAgent 的差异：SuperAgent.run() 是同步非流式；这里驱动 **子代理的
    arun_stream**，把每个 StreamEvent 映射回「token 通道」的字符串（普通文本 +
    THINK_TAG 思考段 + TOOL_TAG 工具事件），供 ChatPage 的 _on_token 原样消费。

    子代理由 LightAgents 的 default_subagent_factory 创建；其 LLM 一律用本模块的
    GatewayLLM（全部走 Gateway 自适应调度）。工具发现 / FindTools 复用 SuperAgent
    的 ToolCatalog 语义——首版只驱动单/多子代理的流式产出与汇总，工具按需激活
    （catalog 懒加载）由子代理自身经 find 工具完成。

    run() 同步入口：内部用 asyncio.run 跑 arun_stream，把增量经 yield 上抛，
    结束后返回（最终回答, 总 token 数统计）。供 agent_bridge 的 AgentChatSession
    在后台 QThread 里驱动。
    """

    def __init__(self, gateway_llm: GatewayLLM, tool_registry=None, config=None,
                 max_steps: int = 3, catalog=None):
        self.llm = gateway_llm
        self.tool_registry = tool_registry
        self.max_steps = max_steps
        self._catalog = catalog
        # 懒加载 LightAgents 的 Config / factory，避免构造期 import 失败
        self._config = config
        self._subagent_factory = None

    def _lazy_setup(self):
        _ensure_lightagents()
        from light_agents.core.config import Config
        from light_agents.agents.factory import default_subagent_factory
        if self._config is None:
            cfg = Config()
            # 关闭带文件写入副作用的内置工具（TodoWrite/DevLog/会话持久化/子代理）；
            # 技能系统启用但仅作「知识注入」（SkillTool 只返回技能文本，我们不给它
            # 文件读/写工具，故 agent 无法落盘文件——保持只读安全约定）。
            cfg.todowrite_enabled = False
            cfg.devlog_enabled = False
            cfg.session_enabled = False
            cfg.subagent_enabled = False
            cfg.trace_enabled = False
            skills_dir = _skills_dir()
            cfg.skills_dir = skills_dir
            cfg.skills_enabled = skills_dir is not None
            cfg.skills_auto_register = True
            self._config = cfg
        if self._subagent_factory is None:
            # 用 _ActiveScopedRegistry 包一层：子代理只能看到 catalog 里 active 的工具，
            # 未激活工具（如 weather）需先经 FindTools 发现并激活，下一轮才注入 schema。
            scoped = self._make_scoped_registry()
            self._subagent_factory = lambda atype: default_subagent_factory(
                atype, self.llm, scoped, self._config)
        # 把技能也挂进工具目录（FindTools 可发现），并确保 SkillTool 已注册到注册表
        self._wire_skills()
        return self._config

    def _make_scoped_registry(self):
        """构造按 catalog 激活状态裁剪的注册表视图；无 catalog 时退回真实注册表。"""
        if self._catalog is None:
            return self.tool_registry
        return _ActiveScopedRegistry(self.tool_registry, self._catalog)

    def _wire_skills(self) -> None:
        """把技能目录注册进 tool_registry 与 ToolCatalog，供 FindTools 发现。"""
        _ensure_lightagents()
        if self.tool_registry is None or self._config is None:
            return
        try:
            from light_agents.skills import SkillLoader
            from light_agents.tools.builtin.skill_tool import SkillTool
        except Exception:  # noqa: BLE001 —— 技能不可用则静默跳过
            return
        loader = SkillLoader(skills_dir=self._config.skills_dir)
        # SkillTool 的 Tool.name = "Skill"
        if self.tool_registry.get_tool("Skill") is None:
            self.tool_registry.register_tool(SkillTool(skill_loader=loader))
        if self._catalog is not None:
            for name in loader.list_skills():
                skill = loader.get_skill(name)
                if skill is None:
                    continue
                self._catalog.add_entry(
                    name=f"skill:{name}",
                    description=skill.description or f"技能 {name}",
                    tags=[name, "技能", "skill"],
                    category="skill",
                    resident=False,
                )

    # ------------------------------------------------------------------ #
    def run(self, input_text: str, **kwargs) -> str:
        """同步入口：把异步 arun_stream 包进 asyncio.run，返回最终回答纯文本。

        供 agent_bridge 的 AgentChatSession 在后台 QThread 里驱动——那里要的是
        一次次「普通文本增量」，故这里返回去掉带外标记（THINK/TOOL 段）的正文。
        真正逐 token 的 UI 体验由 agent_bridge 改用本类的异步 generator 提供。
        """
        import asyncio
        from .agent_bridge import TOOL_TAG, TOOL_END_TAG, THINK_TAG, THINK_END_TAG

        async def _drive():
            collected: List[str] = []
            async for token in self.arun_stream(input_text, **kwargs):
                collected.append(token)
            return "".join(collected)

        raw = asyncio.run(_drive())
        # 剥离带外信号段，只留正文
        for start, end in ((THINK_TAG, THINK_END_TAG), (TOOL_TAG, TOOL_END_TAG)):
            out = []
            rest = raw
            while start in rest:
                before, _, rest = rest.partition(start)
                out.append(before)
                _, _, rest = rest.partition(end)
            out.append(rest)
            raw = "".join(out)
        return raw.strip()

    # ------------------------------------------------------------------ #
    async def arun_stream(self, input_text: str, **kwargs):
        """异步流式编排：逐 token yield（含 THINK_TAG/TOOL_TAG 带外信号）。

        ChatPage 的 _ChatWorker 在后台 QThread 里跑同步循环消费；本生成器应在
        agent_bridge 里经一个独立 asyncio 事件循环被消费，把每段增量上抛。
        """
        _ensure_lightagents()
        from light_agents.core.streaming import StreamEventType

        self._lazy_setup()
        subtasks = _route_intent(input_text)
        if not subtasks:
            subtasks = [{"task": input_text, "agent_type": "react"}]

        results: List[Dict[str, Any]] = []

        for subtask in subtasks:
            agent_type = subtask.get("agent_type", "react")
            task = subtask.get("task", "") or input_text
            try:
                subagent = self._subagent_factory(agent_type)
            except Exception as exc:  # noqa: BLE001
                yield f"\n（子代理创建失败：{exc}）\n"
                results.append({"task": task, "agent_type": agent_type,
                                "success": False, "summary": str(exc)})
                continue

            collected: List[str] = []
            # LightAgents 只在「钩子」里发工具开始事件（on_tool_call），不 yield 进
            # 生成器；这里把钩子接进来记录（tool_call_id -> (name, args)），并在随后的
            # TOOL_CALL_FINISH 到达前先补发一条「call」事件——这样 UI 能看到
            # 「🔧 正在调用 X（参数…）」这一步，而不是只有完成态。
            pending_calls: Dict[str, tuple] = {}

            async def _on_tool_call(event):  # noqa: ANN001 —— AgentEvent
                d = event.data or {}
                tid = d.get("tool_call_id", "")
                if tid:
                    pending_calls[tid] = (d.get("tool_name", "?"),
                                          d.get("args", {}) or {})

            try:
                async for evt in subagent.arun_stream(
                        task, on_tool_call=_on_tool_call, **kwargs):
                    from .agent_bridge import _tool_event
                    if evt.type == StreamEventType.TOOL_CALL_FINISH:
                        tid = (evt.data or {}).get("tool_call_id", "")
                        if tid in pending_calls:
                            name, args = pending_calls.pop(tid)
                            yield _tool_event("call", name=name, id=tid, args=args)
                    for piece in self._emit_event(evt):
                        yield piece
                    if evt.type == StreamEventType.LLM_CHUNK:
                        collected.append(evt.data.get("chunk", "")
                                         or evt.data.get("text", ""))
                    elif evt.type == StreamEventType.AGENT_FINISH:
                        # 最终答案可能只经 AGENT_FINISH.result 带回：当模型经 Finish 工具
                        # 收尾的那一轮无 LLM_CHUNK 正文（见 light_agents react_agent），
                        # 若这里不补吐，对话页会一直空白。已逐 token 流过（collected 已含
                        # 全文）的路径则不重复吐；补吐时按 ~120 字分片并以微小间隔吐出，
                        # 避免尾部整段瞬间跳出（结合流式防抖自然过渡）。
                        result = ((evt.data or {}).get("result", "") or "").strip()
                        if result and result not in "".join(collected):
                            import asyncio as _ai
                            step = max(1, 120)
                            for _i in range(0, len(result), step):
                                yield result[_i:_i + step]
                                await _ai.sleep(0.012)
            except Exception as exc:  # noqa: BLE001
                msg = f"\n（子任务执行失败：{exc}）\n"
                yield msg
                results.append({"task": task, "agent_type": agent_type,
                                "success": False, "summary": str(exc)})
                continue
            results.append({
                "task": task, "agent_type": agent_type,
                "success": True, "summary": "".join(collected),
            })

        # 汇总：单子任务成功直接返回其正文，否则用 LLM 汇总（流式）
        if len(results) == 1 and results[0].get("success"):
            return

        parts = _synthesize_parts(input_text, results)
        try:
            for chunk in self.llm.stream_invoke(
                [{"role": "user", "content": parts["prompt"]}]):
                yield chunk
        except Exception as exc:  # noqa: BLE001
            yield "\n\n" + parts["joined"]

    def _emit_event(self, evt):
        """把单个 StreamEvent 映射回 token 通道的字符串片段列表。"""
        _ensure_lightagents()
        from light_agents.core.streaming import StreamEventType
        from .agent_bridge import _tool_event, THINK_TAG, THINK_END_TAG

        t = evt.type
        data = evt.data or {}
        if t == StreamEventType.LLM_CHUNK:
            return [data.get("chunk", "") or data.get("text", "")]
        if t == StreamEventType.THINKING:
            return [f"{THINK_TAG}"
                    + (data.get("thinking", "") or data.get("thinking_text", ""))
                    + THINK_END_TAG]
        if t == StreamEventType.TOOL_CALL_FINISH:
            result_text = data.get("result", "") or ""
            return [_tool_event("result", name=data.get("tool_name", "?"),
                                id=data.get("tool_call_id", ""),
                                ok=not result_text.startswith("❌"),
                                text=_clip_text(result_text))]
        if t == StreamEventType.TOOL_CALL_START:
            return [_tool_event("call", name=data.get("tool_name", "?"),
                                id=data.get("tool_call_id", ""),
                                args=data.get("args", {}))]
        if t == StreamEventType.ERROR:
            return [f"\n（错误：{data.get('error', '') or data.get('message', '')}）\n"]
        # STEP_* / AGENT_* 不吐正文
        return []

def _clip_text(s: str, n: int = 400) -> str:
    """工具结果文本截断（用于状态行），避免超长结果撑爆气泡。"""
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def _synthesize_parts(input_text: str, results: List[Dict[str, Any]]) -> Dict[str, str]:
    """构建「多子任务成果汇总」的提示词与未汇总拼接文本。"""
    parts = []
    for i, r in enumerate(results, 1):
        tag = "✓" if r.get("success") else "✗"
        parts.append(f"[子任务{i}｜{r.get('agent_type')} {tag}]\n{r.get('summary', '')}")
    joined = "\n\n".join(parts)
    prompt = (
        "你是超级智能体的汇总器。请把下面各子任务的成果合并成一份连贯、"
        "有逻辑的最终回答，保留关键结论，去掉冗余。\n\n"
        f"原始用户请求：{input_text}\n\n各子任务成果：\n{joined}"
    )
    return {"prompt": prompt, "joined": joined}


# --------------------------------------------------------------------------- #
# 工具层桥接：把 LightAIBox 的只读桌面工具接入 LightAgents ToolRegistry/ToolCatalog
# --------------------------------------------------------------------------- #
class DesktopToolAdapter:
    """把一个 DesktopTools 的 ToolDef（dict）包成 LightAgents 期望的 `Tool` 面。

    LightAgents 的 ReAct/Simple 子代理通过注册表读工具：``get_parameters()`` 供
    schema 构建（_tool_runner.build_tool_schemas），``run(parameters) -> ToolResponse``
    供实际执行。这里对只读桌面工具做薄适配，保持其 `execute` 的「返回纯文本观察」
    语义——执行结果转成 ToolResponse.success 的 text，失败转 error。

    首版仅装配只读 / 无副作用工具（时间 / 计算器 / 网关状态），不含文件写入类，
    与「智能体模式只读」的安全约定一致。
    """

    def __init__(self, desktop: "DesktopTools", name: str):
        self.name = name
        self.description = desktop._tools[name]["description"]
        self._desktop = desktop
        self._params = desktop._tools[name]["parameters"]  # JSON Schema（object）

    def get_parameters(self):
        """把 JSON Schema 的 properties 转成 ToolParameter 列表。"""
        _ensure_lightagents()
        from light_agents.tools.base import ToolParameter
        schema = self._params or {}
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        params = []
        for pname, pdef in props.items():
            ptype = {"string": "string", "integer": "integer",
                     "number": "number", "boolean": "boolean"}.get(
                (pdef or {}).get("type", "string"), "string")
            params.append(ToolParameter(
                name=pname, type=ptype,
                description=(pdef or {}).get("description", f"参数 {pname}"),
                required=pname in required,
                default=(pdef or {}).get("default"),
            ))
        return params

    def run(self, parameters: Dict[str, Any]):
        _ensure_lightagents()
        from light_agents.tools.response import ToolResponse
        observation = self._desktop.execute(self.name, parameters or {})
        if observation.startswith("❌"):
            return ToolResponse.error(code="EXECUTION_ERROR", message=observation)
        return ToolResponse.success(text=observation, data={"output": observation})

    async def arun_with_timing(self, parameters: Dict[str, Any]):
        """异步执行 + 时间统计（ReAct 代理调用的是这一接口，而非直接 run()）。

        与 LightAgents 的 Tool.arun_with_timing 语义一致；我们的 run() 已把工具
        结果/异常收敛成 ToolResponse（不会裸抛），这里直接委托并附上耗时即可。
        """
        _ensure_lightagents()
        from light_agents.tools.response import ToolResponse
        import time as _time
        start = _time.perf_counter()
        response = self.run(parameters or {})
        elapsed_ms = int((_time.perf_counter() - start) * 1000)
        if response is not None and getattr(response, "stats", None) is None:
            try:
                response.stats = {"time_ms": elapsed_ms}
            except Exception:  # noqa: BLE001
                pass
        return response

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description}


def build_desktop_registry(gateway: Gateway, provider_id: Optional[int] = None,
                           session_dir: Optional[str] = None,
                           approval=None):
    """构造一个含只读桌面工具 + （可选）沙箱写文件工具 + FindTools 元工具的 ToolRegistry。

    返回 (registry, catalog)：registry 供子代理建 schema / 执行；catalog 供 FindTools
    做关键词检索 + 按需激活。二者与 SuperAgent._register_find_tools 的装配方式一致。

    仅当同时提供 session_dir 与 approval（app/approval.ApprovalCoordinator）时，才会把
    FileTools 的写文件工具（write_text / write_docx / write_xlsx）注册进 registry 与
    catalog —— 否则保持纯只读，向后兼容。
    """
    _ensure_lightagents()
    from light_agents.tools.registry import ToolRegistry
    from light_agents.tools.tool_catalog import ToolCatalog
    from .agent_tools import DesktopTools  # noqa: PLC0415 —— 延迟到此处避免循环导入

    desktop = DesktopTools(gateway, provider_id=provider_id)
    registry = ToolRegistry()

    # 更丰富的内置「日期/时间/用时区推断地点」工具，取代桌面版 get_current_time
    # （离线、只读；额外给出星期、UTC 偏移、IANA 时区与推断地点）。
    date_time_tool = None
    try:
        from light_agents.tools.builtin.date_time_tool import DateTimeTool  # noqa: PLC0415
        date_time_tool = DateTimeTool()
    except Exception:  # noqa: BLE001 —— 内置工具缺失则回退到桌面版
        date_time_tool = None

    for tool in desktop.list_tools():
        if date_time_tool is not None and tool["name"] == "get_current_time":
            continue  # get_current_time 由内置 DateTimeTool 提供（更全）
        registry.register_tool(DesktopToolAdapter(desktop, tool["name"]))
    if date_time_tool is not None and registry.get_tool(date_time_tool.name) is None:
        registry.register_tool(date_time_tool)

    # 天气查询（LightAgents 内置 WeatherTool，联网访问中国天气网）。
    # 注册为「非常驻」工具：不直接塞进子代理的默认工具 schema，而是登记进 catalog，
    # 由模型先经 FindTools 检索（如「天气」）发现后，再在下一轮按需注入 schema。
    # 这样既保留「按需发现」的懒加载语义，又保证执行时 registry.get_tool 能取到实例。
    weather_tool = None
    ip_location_tool = None
    try:
        from light_agents.tools.builtin.weather_tool import WeatherTool  # noqa: PLC0415
        from light_agents.tools.builtin.ip_location_tool import IPLocationTool  # noqa: PLC0415
        # 天气与 IP 定位联动：city 缺省时，WeatherTool 经 location_provider 自动
        # 推断当前城市（见 ip_location_tool.get_city），免去用户手动报城市。
        ip_location_tool = IPLocationTool()
        weather_tool = WeatherTool(location_provider=ip_location_tool.get_city)
    except Exception:  # noqa: BLE001 —— 内置工具缺失/联网权限受限则回退为不提供
        weather_tool = None
        ip_location_tool = None
    if weather_tool is not None and registry.get_tool("weather") is None:
        registry.register_tool(weather_tool)
    if ip_location_tool is not None and registry.get_tool("get_current_location") is None:
        registry.register_tool(ip_location_tool)

    file_tools = None
    if session_dir is not None and approval is not None:
        from .file_tools import FileTools  # noqa: PLC0415
        file_tools = FileTools(session_dir, approval)
        for tool in file_tools.list_tools():
            registry.register_tool(DesktopToolAdapter(file_tools, tool["name"]))

    # 工具发现：目录 + FindTools 元工具（常驻）
    catalog = ToolCatalog(registry)
    from light_agents.tools.builtin.find_tools_tool import FindToolsTool
    find = FindToolsTool(catalog=catalog, auto_activate=True)
    if registry.get_tool("FindTools") is None:
        registry.register_tool(find)
    catalog.add_entry(
        name="FindTools",
        description=getattr(find, "description", "检索并发现可用工具"),
        tags=["发现", "检索", "工具", "工具目录", "find", "tools"],
        category="meta",
        resident=True,
    )
    for t in desktop.list_tools():
        if date_time_tool is not None and t["name"] == "get_current_time":
            catalog.add_entry(
                name=date_time_tool.name,
                description=date_time_tool.description,
                tags=["时间", "日期", "时区", "地点", "几点", "几号",
                      "time", "date", "datetime", "location"],
                category="desktop", resident=True,
            )
            continue
        catalog.add_entry(
            name=t["name"], description=t["description"],
            tags=[t["name"]], category="desktop", resident=True,
        )
    if weather_tool is not None:
        # 非常驻：仅登记进目录，供 FindTools 检索「天气」按需发现；已在 registry 注册，
        # 由 SuperAgent/模型经发现后激活下一轮注入 schema。联网工具，默认不常驻以控制
        # 上下文占用，并契合「只读离线优先」的首版约定（模型主动索取时才暴露给上层）。
        catalog.add_entry(
            name="weather",
            description=weather_tool.description,
            tags=["天气", "温度", "气温", "预报", "湿度", "weather",
                  "temperature", "forecast"],
            category="web",
            resident=False,
        )
    if ip_location_tool is not None:
        # 常驻：模型第 1 轮即可见可调（「我在哪 / 当前地理位置」无需先经 FindTools
        # 发现）。IP 定位无参数、联网只读、上下文占用极低，适合直接内置。
        catalog.add_entry(
            name=ip_location_tool.name,
            description=ip_location_tool.description,
            tags=["位置", "定位", "IP", "我在哪", "城市", "省市", "where",
                  "location", "ip", "geo"],
            category="web",
            resident=True,
        )
    if file_tools is not None:
        for t in file_tools.list_tools():
            catalog.add_entry(
                name=t["name"], description=t["description"],
                tags=[t["name"], "write", "file", "产出", "生成"]
                + ([t["name"].replace("write_", "")] if t["name"].startswith("write_") else []),
                category="output", resident=True,
            )
    return registry, catalog