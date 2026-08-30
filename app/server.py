"""统一 API 服务：把进程内的 Gateway 暴露为可选的 OpenAI 兼容 / Anthropic 兼容本地 HTTP 接口。

- OpenAI 兼容：POST /v1/chat/completions、GET /v1/models
- Anthropic 兼容：POST /v1/messages

协议开关放在共享的 `ServerConfig` 上，每次请求读取，勾选即时生效、无需重启。
服务在本进程内的守护线程中运行（uvicorn），绑定 127.0.0.1。
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import config
from .client import ChatError
from .gateway import Gateway, GatewayError
from .models import ClientResult

DEFAULT_TIMEOUT = 120  # 上层 provider 调用超时（秒）

# 上游 SDK 可接受的标准参数白名单。统一 API 收到的外部请求（如 Claude Code）
# 会附带客户端自己的扩展字段（context_management / output_config 等），这些
# 字段不是上游协议参数，直接透传会导致 anthropic/openai SDK 抛 TypeError。
# 这里只透传真正属于对应协议的关键参数，其余一律丢弃。
_ANTHROPIC_PASSTHROUGH = {
    "max_tokens", "temperature", "top_p", "top_k", "stop_sequences",
    "metadata", "tools", "tool_choice", "thinking",
}
_OPENAI_PASSTHROUGH = {
    "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop",
    "frequency_penalty", "presence_penalty", "n", "seed", "tools",
    "tool_choice", "logprobs", "top_logprobs", "response_format",
}


# --------------------------------------------------------------------------- #
# 配置（被每次请求读取，UI 可直接改）
# --------------------------------------------------------------------------- #
@dataclass
class ServerConfig:
    host: str = config.DEFAULT_SERVER_HOST
    port: int = config.DEFAULT_SERVER_PORT
    openai_enabled: bool = True
    anthropic_enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, port=None, openai_enabled=None, anthropic_enabled=None):
        """运行时修改配置（UI 勾选/改端口用），加锁避免与请求读取竞争。"""
        with self._lock:
            if port is not None:
                self.port = port
            if openai_enabled is not None:
                self.openai_enabled = openai_enabled
            if anthropic_enabled is not None:
                self.anthropic_enabled = anthropic_enabled


# --------------------------------------------------------------------------- #
# 模型分发
# --------------------------------------------------------------------------- #
def _dispatch_model(model):
    """把外部模型名解析成 gateway 参数：auto/空 -> None（自适应），否则原样。"""
    if model in (None, "", config.MODEL_AUTO):
        return None
    return model


# --------------------------------------------------------------------------- #
# 请求翻译
# --------------------------------------------------------------------------- #
def _to_internal_messages(payload):
    """OpenAI：messages 原样；Anthropic：system 前置于 messages 并规整 content。

    返回 (messages, kwargs)。kwargs 为去掉保留字段后透传给 gateway 的额外参数。
    """
    messages_in = payload.get("messages")
    if not isinstance(messages_in, list) or not messages_in:
        raise ValueError("缺少 messages")

    msgs = []
    for m in messages_in:
        if not isinstance(m, dict):
            raise ValueError("messages 中每一项必须是对象")
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            # Anthropic 块列表：只要含工具块（tool_use / tool_result / thinking），
            # 就保留完整块列表（含 text 块）原样回传，否则上游 tool_result 找不到
            # 对应的 tool_use 会直接 400；纯 text 块列表则拼接为文本。
            has_non_text = any(isinstance(b, dict) and b.get("type") != "text"
                               for b in content)
            if has_non_text:
                pass  # content 保持块列表
            else:
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text")
        msgs.append({"role": role, "content": content})

    # Anthropic 顶层 system → 前置。可能是字符串，也可能是块数组
    # （Claude Code 等客户端发带 cache_control 的 text 块列表），块数组取文本拼接。
    system = payload.get("system")
    if isinstance(system, str) and system:
        msgs.insert(0, {"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(
            b.get("text", "") if isinstance(b, dict) else (b if isinstance(b, str) else "")
            for b in system)
        if text:
            msgs.insert(0, {"role": "system", "content": text})

    kwargs = {k: v for k, v in payload.items()
              if k not in ("model", "stream", "system", "messages")}
    return msgs, kwargs


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #
def _openai_error(status, etype, message):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": etype}})


def _anthropic_error(status, etype, message):
    return JSONResponse(
        status_code=status,
        content={"type": "error",
                 "error": {"type": etype, "message": message}})


def _anthropic_content_block(blk) -> dict:
    """把中性 ContentBlock 转成 Anthropic 协议的内容块 dict。"""
    d = blk.to_dict()
    if d["type"] == "tool_use":
        return {"type": "tool_use", "id": d["id"],
                "name": d["name"], "input": d["input"]}
    if d["type"] == "thinking":
        return {"type": "thinking", "thinking": d["thinking"],
                "signature": d["signature"]}
    return {"type": "text", "text": d.get("text", "")}


def _do_call(gateway, messages, model, kwargs, api_type=None):
    """执行一次（非流式）调用，返回 ClientResult 或抛 HTTPException。"""
    try:
        return gateway.chat(messages, model=model, api_type=api_type, **kwargs)
    except GatewayError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ChatError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# --------------------------------------------------------------------------- #
# OpenAI 响应
# --------------------------------------------------------------------------- #
def _openai_chunk(req_id, created, model, obj=None, delta=None,
                  finish_reason=None, usage=None):
    chunk = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
    }
    if usage is not None:
        chunk["usage"] = usage
        return chunk
    chunk["choices"] = [{
        "index": 0,
        "delta": delta or {},
        "finish_reason": finish_reason,
    }]
    return chunk


def _sse(payload):
    """OpenAI 流式帧：data: <json>。"""
    return f"data: {json.dumps(payload)}\n\n"


def _openai_stream(gateway, messages, model, kwargs, api_type=None):
    """SSE 生成器：yield 文本增量，结束后补 finish/usage/[DONE]。"""
    req_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    disp = model or config.MODEL_AUTO

    it = gateway.chat_stream(messages, model=model, api_type=api_type, **kwargs)
    result = None
    try:
        yield _sse(_openai_chunk(
            req_id, created, disp,
            delta={"role": "assistant", "content": ""}, finish_reason=None))
        while True:
            try:
                piece = next(it)
            except StopIteration as e:
                result = e.value  # ClientResult
                break
            if piece:
                yield _sse(_openai_chunk(
                    req_id, created, disp,
                    delta={"content": piece}, finish_reason=None))
    finally:
        it.close()
    yield _sse(_openai_chunk(
        req_id, created, disp, delta={}, finish_reason="stop"))
    if result is not None:
        yield _sse(_openai_chunk(
            req_id, created, disp,
            usage={
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
            }))
    yield "[DONE]\n"


# --------------------------------------------------------------------------- #
# Anthropic 响应
# --------------------------------------------------------------------------- #
def _anthropic_sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _anthropic_stream(gateway, messages, model, kwargs, api_type=None):
    """SSE 生成器：message_start → content_block_* → message_delta → message_stop。

    内容块基于 client 层返回的完整 blocks 重放，因此能透传 text / tool_use 等全部
    块类型（Claude Code 依赖 tool_use 块进入多步 agent 循环）。message_delta 的
    stop_reason 取上游真实值（tool_use 结尾应为 "tool_use"）。
    """
    msg_id = f"msg_{uuid.uuid4().hex}"
    disp = model or config.MODEL_AUTO

    it = gateway.chat_stream(messages, model=model, api_type=api_type, **kwargs)
    result = None
    pieces: list = []
    try:
        yield _anthropic_sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": disp, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        # 阶段一：逐段收文本增量，作为单一 text 块播出
        text_started = False
        while True:
            try:
                piece = next(it)
            except StopIteration as e:
                result = e.value  # ClientResult
                break
            if piece:
                if not text_started:
                    text_started = True
                    yield _anthropic_sse("content_block_start", {
                        "type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })
                pieces.append(piece)
                yield _anthropic_sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                })
        if text_started:
            yield _anthropic_sse("content_block_stop", {
                "type": "content_block_stop", "index": 0})
    finally:
        it.close()

    # 阶段二：补发结果里的全部非文本块（含 tool_use）
    #        文本块若阶段一未发出（纯文本轮实际已发出），此处跳过。
    index = 1 if text_started else 0
    if result is not None:
        for blk in result.blocks:
            if blk.type == "text":
                if text_started:
                    continue  # 已在阶段一按增量播出
                yield _anthropic_sse("content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": "text", "text": blk.text},
                })
                yield _anthropic_sse("content_block_stop", {
                    "type": "content_block_stop", "index": index})
                index += 1
            elif blk.type == "tool_use":
                # 按官方流式协议播出：start 时 input 为空对象，参数经
                # input_json_delta 携带（整段 JSON 一次发出），客户端累积后解析。
                yield _anthropic_sse("content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {
                        "type": "tool_use", "id": blk.tool_id,
                        "name": blk.tool_name, "input": {},
                    },
                })
                yield _anthropic_sse("content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "input_json_delta",
                              "partial_json": json.dumps(
                                  blk.tool_input, ensure_ascii=False)},
                })
                yield _anthropic_sse("content_block_stop", {
                    "type": "content_block_stop", "index": index})
                index += 1
            elif blk.type == "thinking":
                yield _anthropic_sse("content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {
                        "type": "thinking", "thinking": blk.thinking,
                        "signature": blk.signature,
                    },
                })
                yield _anthropic_sse("content_block_stop", {
                    "type": "content_block_stop", "index": index})
                index += 1

    output_tokens = result.completion_tokens if result else 0
    input_tokens = result.prompt_tokens if result else 0
    stop_reason = (result.stop_reason if result
                   and result.stop_reason else
                   ("tool_use" if result and result.has_tool_use else "end_turn"))
    yield _anthropic_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens,
                  "input_tokens": input_tokens},
    })
    yield _anthropic_sse("message_stop", {"type": "message_stop"})


# --------------------------------------------------------------------------- #
# FastAPI 应用
# --------------------------------------------------------------------------- #
def build_app(gateway: Gateway, config_obj: ServerConfig) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # ---- OpenAI 兼容 ----
    @app.get("/v1/models")
    def list_models():
        if not config_obj.openai_enabled:
            raise HTTPException(status_code=404, detail="OpenAI 接口已停用")
        now = int(time.time())
        models = [config.MODEL_AUTO] + sorted(
            gateway.list_models(api_type=config.API_OPENAI))
        return {
            "object": "list",
            "data": [{"id": m, "object": "model", "created": now,
                      "owned_by": "lightaibox"} for m in models],
        }

    @app.post("/v1/chat/completions")
    def chat_completions(payload: dict):
        if not config_obj.openai_enabled:
            raise HTTPException(status_code=404, detail="OpenAI 接口已停用")
        try:
            messages, kwargs = _to_internal_messages(payload)
        except ValueError as exc:
            return _openai_error(400, "invalid_request_error", str(exc))

        model = _dispatch_model(payload.get("model"))
        stream = bool(payload.get("stream"))
        kwargs = {k: v for k, v in kwargs.items() if k in _OPENAI_PASSTHROUGH}

        # 预检：模型存在性与是否有可用 provider（流式须在 200 提交前完成）。
        # 仅在该端点对应的协议类型（OpenAI）中选择 provider。
        avail = set(gateway.list_models(api_type=config.API_OPENAI))
        if model is not None and model not in avail:
            return _openai_error(
                404, "invalid_request_error",
                f"模型 {model} 不存在或不可用")
        try:
            picked = gateway.pick_provider(
                messages, model=model, api_type=config.API_OPENAI)
        except Exception:
            picked = None
        if picked is None:
            return _openai_error(
                502, "api_error", "没有可用的 OpenAI 兼容 provider")

        if stream:
            gen = _openai_stream(
                gateway, messages, model, kwargs, api_type=config.API_OPENAI)
            return StreamingResponse(gen, media_type="text/event-stream")

        try:
            result = _do_call(
                gateway, messages, model, kwargs, api_type=config.API_OPENAI)
        except HTTPException as exc:
            return _openai_error(exc.status_code, "api_error", exc.detail)
        req_id = f"chatcmpl-{uuid.uuid4().hex}"
        return {
            "id": req_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or config.MODEL_AUTO,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
            },
        }

    # ---- Anthropic 兼容 ----
    @app.post("/v1/messages")
    def create_message(payload: dict):
        if not config_obj.anthropic_enabled:
            raise HTTPException(status_code=404, detail="Anthropic 接口已停用")
        try:
            messages, kwargs = _to_internal_messages(payload)
        except ValueError as exc:
            return _anthropic_error(
                400, "invalid_request_error", str(exc))

        model = _dispatch_model(payload.get("model"))
        stream = bool(payload.get("stream"))
        kwargs = {k: v for k, v in kwargs.items() if k in _ANTHROPIC_PASSTHROUGH}

        # 仅在该端点对应的协议类型（Anthropic）中选择 provider。
        avail = set(gateway.list_models(api_type=config.API_ANTHROPIC))
        if model is not None and model not in avail:
            return _anthropic_error(
                404, "not_found_error", f"模型 {model} 不存在或不可用")
        try:
            picked = gateway.pick_provider(
                messages, model=model, api_type=config.API_ANTHROPIC)
        except Exception:
            picked = None
        if picked is None:
            return _anthropic_error(
                502, "api_error", "没有可用的 Anthropic 兼容 provider")

        if stream:
            gen = _anthropic_stream(
                gateway, messages, model, kwargs, api_type=config.API_ANTHROPIC)
            return StreamingResponse(gen, media_type="text/event-stream")

        try:
            result = _do_call(
                gateway, messages, model, kwargs, api_type=config.API_ANTHROPIC)
        except HTTPException as exc:
            return _anthropic_error(exc.status_code, "api_error", exc.detail)
        content = [_anthropic_content_block(b) for b in result.blocks]
        if not content:
            content = [{"type": "text", "text": result.content}]
        stop_reason = (result.stop_reason
                       or ("tool_use" if result.has_tool_use else "end_turn"))
        return {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model or config.MODEL_AUTO,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": result.prompt_tokens,
                "output_tokens": result.completion_tokens,
            },
        }

    return app


# --------------------------------------------------------------------------- #
# uvicorn 守护线程服务
# --------------------------------------------------------------------------- #
class GatewayServer:
    """在进程内守护线程中运行 uvicorn，暴露 build_app 创建的 FastAPI 应用。"""

    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        self.config = ServerConfig()
        self.app = None
        self._server = None
        self._thread = None

    @property
    def is_running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and self._server is not None and self._server.started)

    def start(self, cfg: ServerConfig) -> bool:
        """用给定配置（重建应用）启动服务。返回是否成功监听。"""
        if self.is_running:
            return True
        import uvicorn

        self.config = cfg
        self.app = build_app(self.gateway, cfg)
        self._server = uvicorn.Server(uvicorn.Config(
            self.app, host=cfg.host, port=cfg.port, log_level="warning",
            log_config=None))
        self._thread = threading.Thread(
            target=self._server.run, daemon=True, name="LightAIBox-HTTP")
        self._thread.start()
        # 等待启动完成或失败
        for _ in range(200):
            if self._server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        """"优雅停止：置 should_exit 并等待线程退出（守护线程不阻塞进程退出）。"""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._server = None
        self._thread = None
