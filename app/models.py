"""领域数据模型。"""
from dataclasses import dataclass, field
from typing import Optional

from . import config


@dataclass
class Provider:
    """一个大模型 API 提供方（一个 base_url + api_key + model 组合）。"""

    name: str
    api_type: str = config.API_OPENAI
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True
    sort_order: int = 0  # 列表顺序即调度优先级（越小越靠前）
    # 输入门槛：仅自适应调度（不指定模型）时，输入字符数 >= 该值才调用此 provider
    min_input_tokens: int = 0
    # 配额
    quota_type: str = config.QUOTA_UNLIMITED
    quota_limit: int = 0
    used_calls: int = 0
    used_tokens: int = 0
    # 运行统计
    last_tokens_per_sec: float = 0.0
    last_call_at: str = ""
    auto_disabled: bool = False
    disable_reason: str = ""  # 自动关闭原因：quota(超配额) / error(连接异常) / ""(无)
    id: Optional[int] = None

    def is_quota_exceeded(self) -> bool:
        """是否已超出配额。unlimited 永不超限。"""
        if self.quota_type == config.QUOTA_CALLS:
            return self.used_calls >= self.quota_limit
        if self.quota_type == config.QUOTA_TOKENS:
            return self.used_tokens >= self.quota_limit
        return False

    def is_available(self) -> bool:
        """是否可被调度：已启用且未超配额。"""
        return self.enabled and not self.is_quota_exceeded()

    def record_usage(self, calls: int, tokens: int, tokens_per_sec: float,
                     call_at: str) -> bool:
        """累加使用量并更新最近速度。

        返回是否因本次调用后超配额而自动关闭（enabled 被置为 False）。
        """
        self.used_calls += calls
        self.used_tokens += tokens
        self.last_tokens_per_sec = tokens_per_sec
        self.last_call_at = call_at
        if self.is_quota_exceeded() and self.quota_type != config.QUOTA_UNLIMITED:
            self.enabled = False
            self.auto_disabled = True
            self.disable_reason = "quota"
            return True
        return False

    def quota_text(self) -> str:
        """配额的人类可读描述，如 3/10 次、1200/5000 tokens、无限制。"""
        from .ui.i18n import LanguageManager
        tr = LanguageManager().tr
        if self.quota_type == config.QUOTA_CALLS:
            return f"{self.used_calls}/{self.quota_limit} {tr('次', 'calls')}"
        if self.quota_type == config.QUOTA_TOKENS:
            return f"{self.used_tokens}/{self.quota_limit} tokens"
        return tr("无限制", "Unlimited")

    def status_text(self) -> str:
        """状态列文字。"""
        from .ui.i18n import LanguageManager
        tr = LanguageManager().tr
        if self.auto_disabled:
            if self.disable_reason == "error":
                return tr("已自动关闭(连接异常)", "Auto-disabled (connection error)")
            if self.disable_reason == "quota":
                return tr("已自动关闭(超配额)", "Auto-disabled (quota)")
            return tr("已自动关闭", "Auto-disabled")
        if not self.enabled:
            return tr("已停用", "Disabled")
        if self.is_quota_exceeded():
            return tr("已超配额", "Quota exceeded")
        return tr("运行中", "Running")


@dataclass
class CallLog:
    """一次调用记录。"""

    provider_id: Optional[int] = None
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0
    tokens_per_sec: float = 0.0
    status: str = "success"  # success | error
    error: str = ""
    created_at: str = ""
    id: Optional[int] = None


@dataclass
class ContentBlock:
    """响应中的一个结构化内容块（文本、工具调用、思考等）。

    与协议无关的中性表示：OpenAI 的 choices/content 与 Anthropic 的 content 块
    列表都规整到这里，供 server 层按对应协议重新序列化。
    """

    type: str = "text"               # text | tool_use | thinking | ...
    text: str = ""                   # text 块的内容
    tool_name: str = ""              # tool_use 块：工具名
    tool_input: dict = field(default_factory=dict)  # tool_use 块：调用参数
    tool_id: str = ""                # tool_use 块：调用 ID
    thinking: str = ""               # thinking 块：思考内容
    signature: str = ""              # thinking 块：签名

    def to_dict(self) -> dict:
        """转成协议无关的 dict（server 层按协议再加工）。"""
        d: dict = {"type": self.type}
        if self.type == "text":
            d["text"] = self.text
        elif self.type == "tool_use":
            d["name"] = self.tool_name
            d["input"] = self.tool_input
            d["id"] = self.tool_id
        elif self.type == "thinking":
            d["thinking"] = self.thinking
            d["signature"] = self.signature
        return d


@dataclass
class ClientResult:
    """统一客户端返回的结果。

    `content` 为纯文本拼接（兼容旧调用方与日志展示）；`blocks` 承载完整结构
    （含 tool_use），供需要透传工具调用的上层（如 Claude Code 接入）使用。
    """

    content: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: int
    tokens_per_sec: float
    blocks: list = field(default_factory=list)  # List[ContentBlock]
    stop_reason: str = ""  # 上游返回的结束原因（如 end_turn / tool_use / max_tokens）

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def has_tool_use(self) -> bool:
        return any(b.type == "tool_use" for b in self.blocks)
