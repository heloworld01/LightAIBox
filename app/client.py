"""统一的大模型客户端：屏蔽 OpenAI / Anthropic 协议差异。

对上层暴露两个接口：
- `chat`：一次性返回完整 ClientResult（同步）。
- `chat_stream`：生成器，逐段 yield 文本增量；结束后可再从
  生成器返回值拿到带完整 usage 的 ClientResult（含 token 速度）。
"""
import time
from typing import Generator, List, Optional

from . import config
from .models import ClientResult, ContentBlock, Provider

# 对话消息：{"role": "system"|"user"|"assistant", "content": "..."}
# content 可以是字符串，也可以是对应协议的块列表（用于透传 tool_use / tool_result）。
Message = dict


# 流式增量前缀：区分普通文本与思考内容（同一 token 信号通道内复用）。
# Python 端 _ChatWorker 原样上抛，由 ChatPage 解析；前缀本身转义后不会由
# 模型文本伪造——模型输出经过 yield 前的统一包装，见各 client 的 yield 调用。
THINK_TAG = "\x01think\x01"
THINK_END_TAG = "\x01/think\x01"


class ChatError(Exception):
    """调用失败的异常，携带 provider_id 便于记录。"""

    def __init__(self, message: str, provider_id: Optional[int] = None):
        super().__init__(message)
        self.provider_id = provider_id


class BaseClient:
    """协议客户端基类。子类实现 chat / chat_stream。"""

    def __init__(self, provider: Provider):
        self.provider = provider

    def chat(self, messages: List[Message], **kwargs) -> ClientResult:
        raise NotImplementedError

    def chat_stream(self, messages: List[Message], **kwargs) -> Generator[str, None, ClientResult]:
        raise NotImplementedError

    def ping(self, timeout: float = 15.0) -> int:
        """连通性测试：发一条最小请求，验证地址与密钥可用。

        返回耗时（毫秒）；失败抛异常。不计入配额、不写入调用记录。
        """
        raise NotImplementedError


class OpenAIClient(BaseClient):
    def __init__(self, provider: Provider):
        super().__init__(provider)
        from openai import OpenAI

        kwargs = {"api_key": provider.api_key}
        if provider.base_url:
            # OpenAI SDK 需要 base_url 以 /v1 结尾；用户可省略，这里自动补全
            base_url = provider.base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def _apply_thinking(self, extra: dict, thinking_enabled: bool) -> None:
        """把 thinking_enabled 映射为上游参数（chat / chat_stream 共用口径）。

        - 官方 reasoning 模型（o3 / gpt-5 等）：用 reasoning_effort；
        - Qwen / DeepSeek 等兼容网关：用 enable_thinking 与 vLLM 的
          chat_template_kwargs（extra_body 合并进顶层）。
        - **关闭也必须显式下发**：Qwen3 系列的对话模板默认 enable_thinking=True，
          只靠"不发参数"是关不掉的——这是此前"关闭思考不生效"的根因。
        - 仅当 provider 配了 base_url（即非官方直连）时才带这些扩展参数：
          官方 API 会拒绝未知参数，且官方 reasoning 模型的思考增量本就不暴露在
          delta 里，无需干预。
        """
        body = dict(extra.get("extra_body") or {})
        if thinking_enabled:
            extra.setdefault("reasoning_effort", "medium")
            body["enable_thinking"] = True
            body.setdefault("chat_template_kwargs", {})["enable_thinking"] = True
        elif self.provider.base_url:
            body["enable_thinking"] = False
            body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
        else:
            return
        extra["extra_body"] = body

    def chat(self, messages: List[Message], **kwargs) -> ClientResult:
        # 思考模式：与 chat_stream 同口径
        extra = dict(kwargs)
        self._apply_thinking(extra, extra.pop("thinking_enabled", False))

        start = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self.provider.model,
            messages=messages,
            **extra,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = prompt_tokens + completion_tokens
        content = resp.choices[0].message.content or "" if resp.choices else ""

        # token 处理速度（含 prompt，与模型侧计费口径一致）
        tps = total_tokens / (elapsed_ms / 1000.0) if elapsed_ms > 0 else 0.0
        return ClientResult(
            content=content or "",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            tokens_per_sec=tps,
        )

    def ping(self, timeout: float = 15.0) -> int:
        start = time.perf_counter()
        self._client.chat.completions.create(
            model=self.provider.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=timeout,
        )
        return int((time.perf_counter() - start) * 1000)

    def chat_stream(self, messages: List[Message], **kwargs) -> Generator[str, None, ClientResult]:
        from openai import Stream

        # 思考模式：与 chat 同口径（见 _apply_thinking 说明）。
        extra = dict(kwargs)
        self._apply_thinking(extra, extra.pop("thinking_enabled", False))

        start = time.perf_counter()
        stream: Stream = self._client.chat.completions.create(
            model=self.provider.model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            **extra,
        )
        chunks: List[str] = []
        usage = None
        for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage
            delta = chunk.choices[0].delta if chunk.choices else None
            piece = getattr(delta, "content", None) if delta else None
            # OpenAI reasoning 模型：思考增量字段名不统一——
            #   openai 官方兼容层 / 多数网关用 reasoning_content；
            #   vLLM(SGLang 等) webchat-instruct 这类用 reasoning。
            # 两者都读，命中即视为思考段。
            reasoning = None
            if delta is not None:
                reasoning = (getattr(delta, "reasoning_content", None)
                             or getattr(delta, "reasoning", None))
            if reasoning:
                yield THINK_TAG + reasoning + THINK_END_TAG
            if piece:
                chunks.append(piece)
                yield piece

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = prompt_tokens + completion_tokens
        tps = total_tokens / (elapsed_ms / 1000.0) if elapsed_ms > 0 else 0.0
        return ClientResult(
            content="".join(chunks),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            tokens_per_sec=tps,
        )


class AnthropicClient(BaseClient):
    def __init__(self, provider: Provider):
        super().__init__(provider)
        import anthropic

        kwargs = {"api_key": provider.api_key}
        if provider.base_url:
            # Anthropic SDK 会把 /v1/messages 拼到 base_url 后，故末尾不能带 /v1
            base_url = provider.base_url.rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)
        self._fix_bearer_auth()

    def _fix_bearer_auth(self) -> None:
        """把真实 API key 注入 Authorization: Bearer，兼容按 Bearer 鉴权的网关。

        Anthropic Python SDK 默认只把 api_key 放进 `x-api-key` 请求头，而
        `Authorization` 头填的是占位符 `Bearer 123`。走官方 API 没问题，但接入
        自建网关（如 deepseek 服务网格 / devmarket 这类同时暴露 OpenAI 与
        Anthropic 端点的网关）时，对方只认 `Authorization: Bearer <token>`，
        于是 Anthropic 端点鉴权失败（"Token 不存在"），导致 Claude Code 接入时
        provider 被自动关闭，而 UI 连通性测试恰好走 OpenAI 路径所以测不出问题
        （OpenAI SDK 会把完整 key 放进 Bearer）。

        这里通过 `_custom_headers` 覆盖占位符：该字典在 SDK 的 default_headers
        属性里最后合并，能够覆盖 auth_headers 注入的 `Bearer 123`。若 SDK 后续
        版本结构调整（无 _custom_headers 属性），则静默跳过，保持向后兼容。
        """
        custom = getattr(self._client, "_custom_headers", None)
        if isinstance(custom, dict):
            custom["Authorization"] = f"Bearer {self.provider.api_key}"

    def chat(self, messages: List[Message], **kwargs) -> ClientResult:
        system_parts, rest = self._split_messages(messages)

        # Anthropic 协议 max_tokens 是必选参数；OpenAI 协议不需要。
        # 对话页等调用方一般不显式传，这里给默认上限。
        extra = dict(kwargs)
        extra.setdefault("max_tokens", 8192)
        start = time.perf_counter()
        resp = self._client.messages.create(
            model=self.provider.model,
            system="\n".join(system_parts) if system_parts else None,
            messages=rest,
            **extra,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        usage = resp.usage
        prompt_tokens = usage.input_tokens if usage else 0
        completion_tokens = usage.output_tokens if usage else 0
        total_tokens = prompt_tokens + completion_tokens

        blocks = self._blocks_from_message(resp)
        content = "".join(b.text for b in blocks if b.type == "text")
        tps = total_tokens / (elapsed_ms / 1000.0) if elapsed_ms > 0 else 0.0
        return ClientResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            tokens_per_sec=tps,
            blocks=blocks,
            stop_reason=getattr(resp, "stop_reason", None) or "",
        )

    def ping(self, timeout: float = 15.0) -> int:
        start = time.perf_counter()
        self._client.messages.create(
            model=self.provider.model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
            timeout=timeout,
        )
        return int((time.perf_counter() - start) * 1000)

    def chat_stream(self, messages: List[Message], **kwargs) -> Generator[str, None, ClientResult]:
        system_parts, rest = self._split_messages(messages)

        # max_tokens 是 Anthropic 协议必选参数，调用方缺省时给默认上限，
        # 否则 messages.stream() 直接抛 missing required keyword-only argument。
        extra = dict(kwargs)
        extra.setdefault("max_tokens", 8192)

        # 思考模式：调用方传 thinking_enabled=True 时打开上游 extended thinking。
        # budget 取调用方 thinking_budget，否则用默认（1024，上游最小合法值）。
        # 上游要求 max_tokens > budget，不足则抬升。
        thinking_enabled = extra.pop("thinking_enabled", False)
        if thinking_enabled:
            budget = extra.pop("thinking_budget", 1024) or 1024
            extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if extra.get("max_tokens", 0) <= budget:
                extra["max_tokens"] = budget + 1024

        start = time.perf_counter()
        with self._client.messages.stream(
            model=self.provider.model,
            system="\n".join(system_parts) if system_parts else None,
            messages=rest,
            **extra,
        ) as stream:
            # 把 text_delta 逐段上抛；思考增量用 THINK_TAG 段落包装上抛；
            # tool_use 的 input_json 累积在最终 message 里，由 get_final_message()
            # 一次性取出，避免向上游逐段拼 JSON。
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                dtype = getattr(event.delta, "type", None)
                if dtype == "text_delta":
                    yield event.delta.text
                elif dtype == "thinking_delta":
                    yield THINK_TAG + event.delta.thinking + THINK_END_TAG

            final = stream.get_final_message()
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            usage = getattr(final, "usage", None)
            prompt_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            completion_tokens = getattr(usage, "output_tokens", 0) if usage else 0
            total_tokens = prompt_tokens + completion_tokens
            blocks = self._blocks_from_message(final)
            content = "".join(b.text for b in blocks if b.type == "text")
            tps = total_tokens / (elapsed_ms / 1000.0) if elapsed_ms > 0 else 0.0
            return ClientResult(
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                elapsed_ms=elapsed_ms,
                tokens_per_sec=tps,
                blocks=blocks,
                stop_reason=getattr(final, "stop_reason", None) or "",
            )

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_messages(messages: List[Message]):
        """拆分 system（Anthropic 顶层参数）与其它角色，并保留非文本内容块。

        content 为块列表时原样透传（tool_use / tool_result 需无损回传给上游）；
        为字符串时保持纯文本。
        """
        system_parts: List[str] = []
        rest: List[Message] = []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(m.get("content", ""))
            else:
                content = m.get("content", "")
                rest.append({
                    "role": m.get("role", "user"),
                    "content": content,
                })
        return system_parts, rest

    @staticmethod
    def _blocks_from_message(resp) -> List[ContentBlock]:
        """把 SDK 响应消息的 content 块列表规整成中性 ContentBlock 列表。"""
        blocks: List[ContentBlock] = []
        for b in getattr(resp, "content", []) or []:
            btype = getattr(b, "type", None)
            if btype == "text":
                blocks.append(ContentBlock(type="text", text=getattr(b, "text", "") or ""))
            elif btype == "tool_use":
                blocks.append(ContentBlock(
                    type="tool_use",
                    tool_name=getattr(b, "name", "") or "",
                    tool_input=getattr(b, "input", {}) or {},
                    tool_id=getattr(b, "id", "") or "",
                ))
            elif btype == "thinking":
                blocks.append(ContentBlock(
                    type="thinking",
                    thinking=getattr(b, "thinking", "") or "",
                    signature=getattr(b, "signature", "") or "",
                ))
        return blocks


def create_client(provider: Provider,
                  protocol: Optional[str] = None) -> BaseClient:
    """按协议构造对应客户端。

    - 常规 provider：用其自身 api_type（OpenAI / Anthropic）；
    - 兼容型（API_BOTH）：用 `protocol`（调用方端点协议）决定用哪种客户端去请求上游；
    - `protocol` 缺省时退回 provider.api_type（兼容型默认 OpenAI）。
    """
    target = protocol or provider.api_type
    if target == config.API_ANTHROPIC:
        return AnthropicClient(provider)
    return OpenAIClient(provider)