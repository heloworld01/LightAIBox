"""对话会话：承载「对话」页的多轮消息历史与流式调用。

ChatSession 是「对话」页（ChatPage）背后的领域对象，目前不涉及桌面工具：
仅维护 user/assistant 消息列表，并把每次用户提问经 Gateway 流式调用回填。

未来接入桌面 Agent 工具循环时，工具调用（tool_use / tool_result）也以
user 消息（含内容块）的形式追加到这里，作为模型上下文的一部分，
因此 messages 存的是通用 Message 结构（dict），而非只能放纯文本。
"""
import time
from typing import List, Optional

from .client import Message


class ChatSession:
    """单会话消息历史。当前按「单会话」设计，多会话管理后续再扩展。"""

    def __init__(self) -> None:
        self._messages: List[Message] = []

    @property
    def messages(self) -> List[Message]:
        return self._messages

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

    def add_assistant(self, text: str) -> None:
        self._messages.append({"role": "assistant", "content": text,
                               "time": time.time()})

    def clear(self) -> None:
        self._messages = []

    def to_message_list(self) -> List[Message]:
        """返回一份可直接传给 Gateway.chat / chat_stream 的消息副本。

        这里直接返回引用（Gateway 只读不写），保留工具块的完整结构。
        """
        return list(self._messages)