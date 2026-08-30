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

    def chat(self, messages: List[Message], **kwargs) -> ClientResult:
        start = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self.provider.model,
            messages=messages,
            **kwargs,
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

        start = time.perf_counter()
        stream: Stream = self._client.chat.completions.create(
            model=self.provider.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        chunks: List[str] = []
        usage = None
        for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage
            delta = chunk.choices[0].delta if chunk.choices else None
            piece = getattr(delta, "content", None) if delta else None
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

    def chat(self, messages: List[Message], **kwargs) -> ClientResult:
        system_parts, rest = self._split_messages(messages)

        start = time.perf_counter()
        resp = self._client.messages.create(
            model=self.provider.model,
            system="\n".join(system_parts) if system_parts else None,
            messages=rest,
            **kwargs,
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

        start = time.perf_counter()
        with self._client.messages.stream(
            model=self.provider.model,
            system="\n".join(system_parts) if system_parts else None,
            messages=rest,
            **kwargs,
        ) as stream:
            # 只把 text_delta 逐段上抛；tool_use 的 input_json 累积在最终 message 里，
            # 由 get_final_message() 一次性取出，避免向上游逐段拼 JSON。
            for event in stream:
                if (event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"):
                    yield event.delta.text

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