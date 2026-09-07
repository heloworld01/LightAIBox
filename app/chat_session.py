"""对话会话：承载「对话」页的多轮消息历史与流式调用。

ChatSession 是「对话」页（ChatPage）背后的领域对象，目前不涉及桌面工具：
仅维护 user/assistant 消息列表，并把每次用户提问经 Gateway 流式调用回填。

未来接入桌面 Agent 工具循环时，工具调用（tool_use / tool_result）也以
user 消息（含内容块）的形式追加到这里，作为模型上下文的一部分，
因此 messages 存的是通用 Message 结构（dict），而非只能放纯文本。

多会话：一个 ChatSession 对应 db 里的一行 chat_sessions；``id`` 为该行主键，
``summary`` 为该会话被压缩出的「上文摘要」（发送时注入为 system），
``_seq`` 与 db.chat_messages.seq 对齐，便于写透与增量加载。
"""
import json
import time
from typing import List, Optional

from .client import Message


class ChatSession:
    """单会话消息历史（多会话中的一个，含持久化协助字段）。"""

    def __init__(self, session_id: Optional[int] = None,
                 title: str = "新对话", summary: str = "") -> None:
        self._messages: List[Message] = []
        self.id: Optional[int] = session_id      # db.chat_sessions.id
        self.title: str = title
        self.summary: str = summary              # 压缩出的上文摘要
        self._seq: int = 0                       # 与 db.chat_messages.seq 对齐

    @property
    def messages(self) -> List[Message]:
        return self._messages

    @property
    def next_seq(self) -> int:
        return self._seq + 1

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def add_user(self, text: str, images: Optional[List[str]] = None) -> None:
        """追加一条用户消息。images 为图片 data URL 列表（可选）。

        带图片时 content 用 OpenAI 风格块列表承载：text 块 + 若干 image_url 块，
        与 gateway/client 的多模态透传口径一致；纯文本时仍存字符串（兼容旧路径）。
        time 仅用于聊天页展示时间分隔条，不影响传给模型的内容。
        """
        if images:
            blocks: List[dict] = []
            if text:
                blocks.append({"type": "text", "text": text})
            for url in images:
                blocks.append({"type": "image_url",
                               "image_url": {"url": url}})
            content: object = blocks
        else:
            content = text
        self._messages.append({"role": "user", "content": content,
                               "time": time.time()})
        self._seq += 1

    def add_assistant(self, text: str) -> None:
        self._messages.append({"role": "assistant", "content": text,
                               "time": time.time()})
        self._seq += 1

    def clear(self) -> None:
        self._messages = []
        self._seq = 0

    def to_message_list(self) -> List[Message]:
        """返回一份可直接传给 Gateway.chat / chat_stream 的消息副本。

        这里直接返回引用（Gateway 只读不写），保留工具块的完整结构。
        """
        return list(self._messages)

    # -- 持久化协助（写透 / 载入都由 ChatPage 驱动） ---------------------- #
    def bind_store(self, store, session_id: int) -> None:
        """把本会话与 db.ChatStore 绑定：对齐 seq、拉取摘要。"""
        self.id = session_id
        self._seq = store.max_seq(session_id)
        self.summary = store.get_summary(session_id)

    def load_messages_from(self, rows: List[dict]) -> None:
        """用 db.ChatStore.load_messages 返回的 dict 列表填充历史（含 meta）。"""
        self._messages = list(rows)
        self._seq = len(rows)

    def estimate_tokens(self, messages: Optional[List[Message]] = None) -> int:
        """粗略估算一段对话的 token 数（无第三方 tokenizer）。

        中文约 1 字符 ~ 1 token，英文略低；这里按字符数计，足以支撑「上下文预算
        自动裁剪」的近似判断，不追求精确。
        """
        total = 0
        for m in (self._messages if messages is None else messages):
            c = m.get("content", "")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        total += len(b["text"])
                    elif isinstance(b, dict) and b.get("type") == "image_url":
                        total += 512  # 图片按 ~512 token 估算
        return total