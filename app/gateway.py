"""AI 网关：统一 API 代理 + 调度 + 配额 + 调用记录。

对上层暴露两个接口：
- `chat`：一次性返回 ClientResult。
- `chat_stream`：生成器，逐段 yield 文本增量；结束后返回 ClientResult。

二者都支持两种调用方式：
- 指定模型（`model=...`）：在「已启用且未超配额」的 provider 中按模型名挑选；
- 自适应（不传 `model`）：根据策略（长输入优先 / 短输入优先）在全部可用
  provider 中挑选。

调用成功/失败后更新 provider 用量、最近速度，超配额自动关闭；写入 call_logs。
"""
import datetime
from typing import Generator, List, Optional

from . import config, db
from .client import ChatError, Message, create_client
from .models import CallLog, ClientResult, Provider


class GatewayError(Exception):
    """网关层错误（无可调度 provider 或全部失败等）。"""


def _client_protocol(p: Provider, request_type: Optional[str]) -> Optional[str]:
    """决定用哪种协议客户端请求上游。兼容型(API_BOTH)跟随调用方协议，否则用自身类型。"""
    if p.api_type == config.API_BOTH:
        return request_type
    return p.api_type


def _new_client_if_available(p: Provider, protocol: Optional[str] = None):
    try:
        return create_client(p, protocol=protocol)
    except Exception as exc:  # 构造客户端失败（如缺 key）
        raise ChatError(f"初始化客户端失败: {exc}", provider_id=p.id)


def _log(store: db.CallLogStore, provider_id: int, model: str,
         result: Optional[ClientResult], status: str, error: str,
         created_at: str) -> None:
    store.insert(CallLog(
        provider_id=provider_id,
        model=model,
        prompt_tokens=result.prompt_tokens if result else 0,
        completion_tokens=result.completion_tokens if result else 0,
        total_tokens=result.total_tokens if result else 0,
        elapsed_ms=result.elapsed_ms if result else 0,
        tokens_per_sec=result.tokens_per_sec if result else 0.0,
        status=status,
        error=error,
        created_at=created_at,
    ))


class Gateway:
    def __init__(self, provider_store: Optional[db.ProviderStore] = None,
                 log_store: Optional[db.CallLogStore] = None):
        self.providers = provider_store or db.ProviderStore()
        self.logs = log_store or db.CallLogStore()
        self.policy = config.POLICY_LONG_FIRST

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    def _rank_key(self, p: Provider, prompt_tokens: int):
        """调度排序 key。长输入优先：明文长度降序在前；短输入优先则相反。"""
        sign = -1 if self.policy == config.POLICY_LONG_FIRST else 1
        return sign * prompt_tokens

    def _candidates(self, messages: List[Message],
                    model: Optional[str] = None,
                    api_type: Optional[str] = None) -> List[Provider]:
        """按当前策略对可用 provider 排序，返回有序候选列表（用于逐个重试）。

        指定 model 时，进一步只保留模型名匹配的 provider；匹配不到则抛错。
        指定 api_type 时，只保留该协议的 provider（统一 API 按调用协议适配）。
        """
        prompt_len = sum(len(str(m.get("content", ""))) for m in messages)
        pool = [p for p in self.providers.list() if p.is_available()]
        if api_type:
            # 限协议类型：匹配该协议或兼容型（兼容型可用于任一协议）
            pool = [p for p in pool
                    if p.api_type in (api_type, config.API_BOTH)]
        # 输入门槛：仅自适应调度（未指定 model）时，过滤输入字符数低于阈值的 provider
        if model is None:
            pool = [p for p in pool if prompt_len >= p.min_input_tokens]
        if model:
            pool = [p for p in pool if p.model == model]
            if not pool:
                raise GatewayError(
                    f"没有可用的 provider 匹配模型「{model}」"
                    "（模型不存在、已停用或超出配额）")
        if not pool:
            raise GatewayError("没有可用的 provider（全部停用或超出配额）")
        # 列表顺序为主优先级；同顺序（sort_order 相同）时再用输入长度策略兜底
        pool.sort(key=lambda p: (p.sort_order, self._rank_key(p, prompt_len)))
        return pool

    def pick_provider(self, messages: List[Message],
                      model: Optional[str] = None,
                      api_type: Optional[str] = None) -> Optional[Provider]:
        """返回按策略排序后第一个可用 provider（可限定模型名与协议类型）。"""
        try:
            return self._candidates(messages, model=model, api_type=api_type)[0]
        except GatewayError:
            return None

    def list_models(self, api_type: Optional[str] = None) -> List[str]:
        """列出所有已启用且未超配额的 provider 提供的模型名（可限定协议类型）。"""
        return sorted({p.model for p in self.providers.list()
                       if p.is_available() and p.model
                       and (not api_type
                            or p.api_type in (api_type, config.API_BOTH))})

    def test_provider(self, provider_id: int,
                      timeout: float = 15.0) -> dict:
        """连通性测试：验证指定 provider 的地址与密钥是否可用。

        走最小请求，不计入用量、不写调用记录、不影响配额/启用状态。
        返回 {"ok": bool, "elapsed_ms": int, "error": str}。
        """
        p = self.providers.get(provider_id)
        if p is None:
            return {"ok": False, "elapsed_ms": 0, "error": "Provider 不存在"}
        try:
            client = create_client(p)
            elapsed_ms = client.ping(timeout=timeout)
            return {"ok": True, "elapsed_ms": elapsed_ms, "error": ""}
        except Exception as exc:
            return {"ok": False, "elapsed_ms": 0, "error": str(exc)}

    def _record_success(self, p: Provider, result: ClientResult,
                        now: str) -> None:
        """累加用量、更新速度，必要时自动关闭，并写成功日志。"""
        p.record_usage(calls=1, tokens=result.total_tokens,
                       tokens_per_sec=result.tokens_per_sec, call_at=now)
        self.providers.upsert(p)
        _log(self.logs, p.id, p.model, result, "success", "", now)

    def _disable_provider(self, p: Provider, reason: str = "error") -> None:
        """自动关闭 provider（如 auto 容灾时连接异常），持久化使其退出调度。"""
        p.enabled = False
        p.auto_disabled = True
        p.disable_reason = reason
        self.providers.upsert(p)

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    def chat(self, messages: List[Message], policy: Optional[str] = None,
             model: Optional[str] = None, api_type: Optional[str] = None,
             **kwargs) -> ClientResult:
        """统一调用入口（一次性返回）。

        指定 `model` 时按模型名挑选 provider，否则按策略在全部可用 provider 中
        自适应挑选。`api_type` 可限定只在该协议类型的 provider 中挑选（统一 API
        按调用协议适配）。policy 缺省时保持上一次策略（默认长输入优先）；每次调用
        重新从 DB 读 provider 列表，保证配额/启用状态被及时反映。全部 provider
        失败抛 GatewayError。
        """
        self.policy = policy or self.policy
        candidates = self._candidates(messages, model=model, api_type=api_type)

        last_err: Optional[ChatError] = None
        for p in candidates:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                client = _new_client_if_available(
                    p, protocol=_client_protocol(p, api_type))
                result = client.chat(messages, **kwargs)
            except Exception as exc:  # 调度其它 provider 时吞掉单点失败
                err = exc if isinstance(exc, ChatError) else ChatError(str(exc), p.id)
                _log(self.logs, p.id, p.model, None, "error", str(err), now)
                # auto 模式容灾：关闭失败的 provider，重新选择可用接口，减少后续影响
                if model is None:
                    self._disable_provider(p, "error")
                last_err = err
                continue

            self._record_success(p, result, now)
            return result

        if last_err is not None:
            raise GatewayError(f"所有可用 provider 调用失败，最后错误：{last_err}")
        raise GatewayError("没有可用的 provider")

    def chat_stream(self, messages: List[Message], policy: Optional[str] = None,
                    model: Optional[str] = None,
                    api_type: Optional[str] = None,
                    **kwargs) -> Generator[str, None, ClientResult]:
        """统一调用入口（流式）。

        指定 `model` 时按模型名挑选 provider，否则按策略自适应。`api_type` 可
        限定只在该协议类型的 provider 中挑选。逐段 yield 文本增量；正常结束后
        回收 ClientResult（含 token 速度）。若首选的 provider 在流式过程中失败，
        同样降级到下一个候选重试。
        """
        self.policy = policy or self.policy
        candidates = self._candidates(messages, model=model, api_type=api_type)

        last_err: Optional[ChatError] = None
        for p in candidates:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                client = _new_client_if_available(
                    p, protocol=_client_protocol(p, api_type))
                result: ClientResult = yield from client.chat_stream(
                    messages, **kwargs)
            except Exception as exc:
                err = exc if isinstance(exc, ChatError) else ChatError(str(exc), p.id)
                _log(self.logs, p.id, p.model, None, "error", str(err), now)
                # auto 模式容灾：关闭失败的 provider，重新选择可用接口
                if model is None:
                    self._disable_provider(p, "error")
                last_err = err
                continue

            self._record_success(p, result, now)
            return result

        if last_err is not None:
            raise GatewayError(f"所有可用 provider 调用失败，最后错误：{last_err}")
        raise GatewayError("没有可用的 provider")