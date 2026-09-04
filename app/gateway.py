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
from typing import Dict, Generator, List, Optional

from . import config, db
from .client import ChatError, Message, create_client
from .models import CallLog, ClientResult, Provider

# auto 容灾：连续失败该次数后才自动关闭 provider（避免单次网络抖动/偶发 5xx
# 就把正常 provider 永久停用）。计数为进程内状态，成功调用即清零。
_AUTO_DISABLE_FAILURES = 3
# 多模态降级阈值：用户配置了支持多模态、但图片请求连续失败该次数后，
# 系统判定其实不支持图片，自动撤销其多模态标记（保留启用，仅不再路由图片请求）。
_MM_DOWNGRADE_FAILURES = 2


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


# 图片块类型识别：OpenAI 用 image_url / input_image，Anthropic 用 image。
# 任一消息的 content 为块列表且含这些类型，即视为多模态请求。
_IMAGE_BLOCK_TYPES = {"image", "image_url", "input_image"}


def _is_multimodal_request(messages: List[Message]) -> bool:
    """判断请求是否含图片（多模态）。content 为块列表时检查块类型。"""
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") in _IMAGE_BLOCK_TYPES
               for b in content):
            return True
    return False


class Gateway:
    def __init__(self, provider_store: Optional[db.ProviderStore] = None,
                 log_store: Optional[db.CallLogStore] = None):
        self.providers = provider_store or db.ProviderStore()
        self.logs = log_store or db.CallLogStore()
        self.policy = config.POLICY_LONG_FIRST
        # auto 容灾的进程内连续失败计数：provider_id -> 连续失败次数。成功调用清零，
        # 用户手动启用/停用也不会重置（失败计数只在调用结果里增减）。
        self._fail_counts: Dict[int, int] = {}
        # 多模态降级的进程内连续「图片请求失败」计数：provider_id -> 次数。
        # 与通用失败计数分开：只有多模态请求失败才累计，用于撤销误标的多模态能力。
        self._mm_fail_counts: Dict[int, int] = {}

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    def _rank_key(self, p: Provider, prompt_tokens: int):
        """调度排序 key。长输入优先：明文长度降序在前；短输入优先则相反。"""
        sign = -1 if self.policy == config.POLICY_LONG_FIRST else 1
        return sign * prompt_tokens

    def _candidates(self, messages: List[Message],
                    model: Optional[str] = None,
                    api_type: Optional[str] = None,
                    provider_id: Optional[int] = None,
                    require_multimodal: bool = False) -> List[Provider]:
        """按当前策略对可用 provider 排序，返回有序候选列表（用于逐个重试）。

        指定 model 时，进一步只保留模型名匹配的 provider；匹配不到则抛错。
        指定 provider_id 时，直接锁定该 provider（精确选择，跳过调度）。
        指定 api_type 时，只保留该协议的 provider（统一 API 按调用协议适配）。
        require_multimodal 为 True 时，只保留支持多模态的 provider；过滤后为空
        则抛错（auto 调度多模态请求不降级到文本模型）。
        """
        prompt_len = sum(len(str(m.get("content", ""))) for m in messages)
        pool = [p for p in self.providers.list() if p.is_available()]
        if provider_id is not None:
            # 精确按 provider 指定：不受调度策略 / 输入门槛影响，仅要求可用
            matched = [p for p in pool if p.id == provider_id]
            if not matched:
                raise GatewayError(
                    f"指定的 provider「{provider_id}」不存在、已停用或超出配额")
            return matched
        if api_type:
            # 限协议类型：匹配该协议或兼容型（兼容型可用于任一协议）
            pool = [p for p in pool
                    if p.api_type in (api_type, config.API_BOTH)]
        if require_multimodal:
            # 多模态请求：过滤掉不支持图片的 provider，空则报错（不降级文本模型）
            pool = [p for p in pool if p.multimodal]
            if not pool:
                raise GatewayError("没有可用的多模态 provider（请启用支持图片的 provider）")
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
                      api_type: Optional[str] = None,
                      provider_id: Optional[int] = None) -> Optional[Provider]:
        """返回按策略排序后第一个可用 provider（可限定模型名 / 协议类型 / 直接指定）。"""
        try:
            return self._candidates(
                messages, model=model, api_type=api_type,
                provider_id=provider_id)[0]
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
        # 调用成功即清零该 provider 的连续失败计数（下一次失败从 0 重新累计）
        self._fail_counts.pop(p.id, None)
        # 图片请求成功证明其确实支持多模态，清零降级计数
        self._mm_fail_counts.pop(p.id, None)

    def _record_failure(self, p: Provider) -> bool:
        """累计一次失败，返回是否已达到「连续失败阈值」从而应自动关闭。

        auto 容灾不再单次失败即关闭：网络抖动、偶发 5xx / 上游限流等瞬时错误
        不应把一个正常 provider 永久停用。只有当同一 provider 连续失败达到
        _AUTO_DISABLE_FAILURES 次才触发自动关闭。任何一次成功调用都会清零计数。
        """
        key = p.id if p.id is not None else id(p)
        self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
        return self._fail_counts[key] >= _AUTO_DISABLE_FAILURES

    def _disable_provider(self, p: Provider, reason: str = "error") -> None:
        """自动关闭 provider（连续失败达阈值时调用），持久化使其退出调度。"""
        p.enabled = False
        p.auto_disabled = True
        p.disable_reason = reason
        self.providers.upsert(p)
        key = p.id if p.id is not None else id(p)
        self._fail_counts.pop(key, None)

    def _record_multimodal_failure(self, p: Provider) -> bool:
        """累计一次「图片请求失败」，返回是否应撤销其多模态标记。

        用户手动配置了支持多模态、但实际发图就报错——典型是该模型并不具备视觉能力。
        单次失败可能是网络抖动，故用独立的小阈值；一旦达标即判定其实不支持多模态。
        """
        if not p.multimodal or p.id is None:
            return False
        self._mm_fail_counts[p.id] = self._mm_fail_counts.get(p.id, 0) + 1
        return self._mm_fail_counts[p.id] >= _MM_DOWNGRADE_FAILURES

    def _downgrade_multimodal(self, p: Provider) -> None:
        """撤销 provider 的多模态标记（保留启用状态），持久化使其不再被图片请求路由到。"""
        p.multimodal = False
        self.providers.upsert(p)
        self._mm_fail_counts.pop(p.id, None)

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    def chat(self, messages: List[Message], policy: Optional[str] = None,
             model: Optional[str] = None, api_type: Optional[str] = None,
             provider_id: Optional[int] = None,
             **kwargs) -> ClientResult:
        """统一调用入口（一次性返回）。

        指定 `model` 时按模型名挑选 provider，否则按策略在全部可用 provider 中
        自适应挑选。`provider_id` 可精确指定某个 provider（跳过调度）。
        `api_type` 可限定只在该协议类型的 provider 中挑选（统一 API
        按调用协议适配）。policy 缺省时保持上一次策略（默认长输入优先）；每次调用
        重新从 DB 读 provider 列表，保证配额/启用状态被及时反映。全部 provider
        失败抛 GatewayError。
        """
        self.policy = policy or self.policy
        # 多模态请求：auto 调度下要求路由到支持图片的 provider
        need_mm = _is_multimodal_request(messages)
        candidates = self._candidates(
            messages, model=model, api_type=api_type, provider_id=provider_id,
            require_multimodal=need_mm)

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
                # 多模态请求失败：若该 provider 标称支持多模态却发图就错，
                # 累计后撤销其多模态标记（与通用停用相互独立）。
                if need_mm and self._record_multimodal_failure(p):
                    self._downgrade_multimodal(p)
                # auto 模式容灾：仅当连续失败达到阈值才自动关闭 provider，
                # 单次抖动/偶发错误不永久停用，只触发本次降级到下一个候选。
                if model is None and self._record_failure(p):
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
                    provider_id: Optional[int] = None,
                    **kwargs) -> Generator[str, None, ClientResult]:
        """统一调用入口（流式）。

        指定 `model` 时按模型名挑选 provider，否则按策略自适应。`provider_id`
        可精确指定某个 provider。`api_type` 可限定只在该协议类型的 provider 中
        挑选。逐段 yield 文本增量；正常结束后回收 ClientResult（含 token 速度）。
        若首选的 provider 在流式过程中失败，同样降级到下一个候选重试。
        """
        self.policy = policy or self.policy
        need_mm = _is_multimodal_request(messages)
        candidates = self._candidates(
            messages, model=model, api_type=api_type, provider_id=provider_id,
            require_multimodal=need_mm)

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
                # 多模态请求失败：累计后撤销标称支持多模态 provider 的标记
                if need_mm and self._record_multimodal_failure(p):
                    self._downgrade_multimodal(p)
                # auto 模式容灾：仅当连续失败达到阈值才自动关闭，单次失败只降级重试
                if model is None and self._record_failure(p):
                    self._disable_provider(p, "error")
                last_err = err
                continue

            self._record_success(p, result, now)
            return result

        if last_err is not None:
            raise GatewayError(f"所有可用 provider 调用失败，最后错误：{last_err}")
        raise GatewayError("没有可用的 provider")