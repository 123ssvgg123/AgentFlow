"""
    DeepSeek 大模型客户端：tools/llm_client.py

    职责：封装对 DeepSeek 平台（名为 `deepseek` 的对话接口）的统一调用，
        对上提供 `chat()` / `chat_stream()` / `chat_json()` 三个能力。

    密钥管理（需求「做好密钥管理与异常兜底」）：
        1. 密钥只从配置层读取（.env / 环境变量），代码内不落明文；
        2. 启动时校验密钥格式（sk- 前缀），缺失/非法立即告警但不崩溃；
        3. `mask_key()` 提供脱敏展示，日志中绝不打印完整密钥。

    异常兜底（三级）：
        Level 1  指数退避重试（网络抖动 / 限流 429）
        Level 2  主模型重试耗尽 → 若配置了备用模型则**切换备用模型再试一次**（需求
                 「主模型不可用切备用模型」）；备用仍失败才抛 LLMUnavailableError，
                 由上层工具降级为规则引擎
        Level 3  提供 `chat_json()` 的 JSON 解析兜底——解析失败时抽取首个 JSON 片段

    备用模型（需求「主模型不可用切备用模型」）：
        · 开关/模型名/地址/密钥见 config.LLM_FALLBACK_*（config.ini 的 [llm] 段）
        · 「已配置」的定义：LLM_FALLBACK_ENABLED 为真 且 fallback_model 非空
        · 未配置时行为与切换前**完全一致**（零回归）
        · 切换状态通过只读属性 `using_fallback` / `active_model` / `primary_model`
          暴露给 /api/health

    注意：本模块不做业务判断，只负责「把话说出去、把结果拿回来、把异常规整好」。
"""
# 导入异步支持与 JSON 处理
import asyncio
import json
import re
# 导入类型标注
from typing import Any, AsyncGenerator, Dict, List, Optional

# 导入 OpenAI SDK（DeepSeek 完全兼容 OpenAI 协议）
from openai import AsyncOpenAI

from base.config import config
from base.exceptions import LLMUnavailableError
from base.logger import logger
from base.run_policy import RunPolicyError, run_policy


# 备用模型全部失败时的统一报错文案
_FALLBACK_FAILED_MSG = "主模型与备用模型均调用失败"
# 未配置备用模型时的统一报错文案（与切换前保持一致的语义）
_IDENTITY_FALLBACK_MSG = "DeepSeek 调用失败"
# 账户级错误：出现即代表「再试也没用」，必须立刻停止而不是重试
_ACCOUNT_LEVEL_CODES = frozenset({401, 402, 403})
_ACCOUNT_LEVEL_KEYWORDS = (
    "Insufficient Balance",      # 402 余额不足（2026-10-30 事故就是它）
    "insufficient_quota",
    "invalid_api_key",
    "Authentication",
    "Unauthorized",
)


def _is_account_level_error(exc: BaseException) -> bool:
    """
    判断是否是「账户/密钥层面不可用」。

    为什么单独判一类：
        原来的 except Exception 把 402 余额不足也当成网络抖动去重试 ——
        主模型重试 2 次 + 备份模型 1 次 = 单次调用放大成 4 次请求。
        余额不足重试一万次也不会成功，只会把错误日志刷满、把请求量翻四倍。
    """
    if getattr(exc, "status_code", None) in _ACCOUNT_LEVEL_CODES:
        return True
    text = str(exc)
    return any(k in text for k in _ACCOUNT_LEVEL_KEYWORDS)


class DeepSeekClient:
    """DeepSeek 对话客户端（OpenAI 兼容协议，支持主/备模型切换）"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None):
        # 允许调用方覆盖，默认取全局配置
        self.api_key = api_key or config.LLM_API_KEY
        self.base_url = base_url or config.LLM_BASE_URL
        self.model = model or config.LLM_MODEL
        self.timeout = config.LLM_TIMEOUT
        # 是否具备可用密钥（缺失时全部调用走降级）
        self._configured = bool(self.api_key and self.api_key.startswith('sk-'))
        self._client: Optional[AsyncOpenAI] = None

        # ---- 备用模型（需求「主模型不可用切备用模型」） ----
        # 沿用主配置：地址/密钥为空串时分别回落主 base_url / 主 api_key
        self.fallback_model: str = config.LLM_FALLBACK_MODEL
        self.fallback_base_url: str = config.LLM_FALLBACK_BASE_URL or self.base_url
        self.fallback_api_key: str = config.LLM_FALLBACK_API_KEY or self.api_key
        # 备用模型是否可用（开关打开 且 配了非空 model 名）
        self._fallback_configured = bool(
            config.LLM_FALLBACK_ENABLED and self.fallback_model)
        # 是否正处于「已切换到备用模型」状态（只读属性 using_fallback 对外暴露）
        self._using_fallback = False
        # 备用模型 SDK 客户端（惰性创建）
        self._fallback_client: Optional[AsyncOpenAI] = None

        if not self._configured:
            logger.warning(
                "DeepSeek API Key 缺失或格式非法（应以 sk- 开头）。"
                "所有 LLM 工具将自动降级为规则引擎，请检查 .env 中的 LLM_API_KEY。"
            )
        else:
            logger.info(f"DeepSeek 客户端就绪：model={self.model}, base_url={self.base_url}, "
                        f"api_key={self.mask_key(self.api_key)}")
        if self._fallback_configured:
            logger.info(f"[LLM] 备用模型已就绪：model={self.fallback_model}, "
                        f"base_url={self.fallback_base_url}, "
                        f"api_key={self.mask_key(self.fallback_api_key)}")

    # ==================== 密钥安全 ====================

    @staticmethod
    def mask_key(key: str) -> str:
        """密钥脱敏：只保留前 6 位与后 4 位，中间以 * 替代。用于日志与接口回显。"""
        if not key:
            return "<empty>"
        if len(key) <= 10:
            return key[:2] + "*" * (len(key) - 2)
        return f"{key[:6]}{'*' * 8}{key[-4:]}"

    @property
    def available(self) -> bool:
        """密钥是否可用（供上层工具决定是否走降级）"""
        return self._configured and config.LLM_ENABLED

    @property
    def primary_model(self) -> str:
        """主模型名（供 /api/health 展示）"""
        return self.model

    @property
    def fallback_enabled(self) -> bool:
        """备用模型是否已启用（开关打开 且 配了模型名）"""
        return self._fallback_configured

    @property
    def using_fallback(self) -> bool:
        """当前是否处于「已切换到备用模型」状态（只读，供 /api/health 观测）"""
        return self._using_fallback

    @property
    def active_model(self) -> str:
        """当前生效的模型名（未切换 = 主模型；已切换 = 备用模型）"""
        return self._active_model_name()

    # ==================== 主/备模型切换 ====================

    def _active_model_name(self) -> str:
        """当前生效的模型名"""
        if self._using_fallback and self.fallback_model:
            return self.fallback_model
        return self.model

    def _active_client(self) -> AsyncOpenAI:
        """当前生效的 SDK 客户端"""
        return self._ensure_fallback_client() if self._using_fallback else self._ensure_client()

    def _switch_to_fallback(self) -> bool:
        """
        切换到备用模型（幂等）。返回是否切换成功。

        仅当备用模型「已配置」时才切换；未配置时打 warning 并返回 False，
        调用方据此走与切换前完全一致的降级路径（零回归）。
        """
        if self._fallback_configured:
            if not self._using_fallback:
                logger.warning(f"[LLM] 主模型 {self.model} 调用失败，"
                               f"切换到备用模型 {self.fallback_model}")
            self._using_fallback = True
            return True
        logger.warning(f"[LLM] 主模型 {self.model} 调用失败，"
                       f"且未配置备用模型（LLM_FALLBACK_MODEL 为空或开关关闭），"
                       f"按原有路径降级")
        return False

    def _switch_back_to_primary(self) -> None:
        """备用模型调用成功后恢复主模型标记（下次请求优先主模型），并打恢复日志"""
        if self._using_fallback:
            self._using_fallback = False
            logger.info(f"[LLM] 备用模型 {self.fallback_model} 调用成功，"
                        f"已恢复主模型 {self.model}（下次请求优先主模型）")

    def _record_llm_metric(self, ok: bool) -> None:
        """
        记录一次 LLM 调用指标。

        result 取值：主模型成功 = success；备用模型成功 = success_fallback；
        最终失败 = error。model 标签一律用**主模型名**，便于按「服务」聚合，
        再用 result 区分「是否走了备用模型」。
        """
        try:
            from tools.metrics import record_llm

            if ok:
                result = "success_fallback" if self._using_fallback else "success"
            else:
                result = "error"
            record_llm(self.model, result)
        except Exception:  # noqa: BLE001 - 指标失败绝不影响主流程
            pass

    def _ensure_client(self) -> AsyncOpenAI:
        """惰性创建 SDK 客户端（避免无密钥时构造报错）"""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # 重试由本类自己控制，避免与 SDK 叠加
            )
        return self._client

    def _ensure_fallback_client(self) -> AsyncOpenAI:
        """惰性创建备用模型 SDK 客户端（独立的 api_key / base_url）"""
        if self._fallback_client is None:
            self._fallback_client = AsyncOpenAI(
                api_key=self.fallback_api_key,
                base_url=self.fallback_base_url,
                timeout=self.timeout,
                max_retries=0,
            )
        return self._fallback_client

    # ==================== 核心调用 ====================

    def _spend_budget(self, where: str) -> None:
        """
        运行策略：远程模型按量计费（非本地部署），调用前先扣「单次运行预算」。

        超限不抛到用户面前，而是转成 LLMUnavailableError —— 上层工具本来就会
        降级到规则引擎，所以表现为「模型不可用时系统仍能给出确定结果」，
        既守住了成本限制，又不破坏既有降级链路。
        """
        try:
            run_policy.llm.spend(where=where)
        except RunPolicyError as exc:
            logger.warning(f"运行策略拦截 LLM 调用（{where}）：{exc}")
            raise LLMUnavailableError(
                "本次运行已达最小验证的远程调用上限，LLM 工具降级为规则引擎",
                tool="llm_client", detail=str(exc),
            ) from exc

    async def chat(self, messages: List[Dict[str, str]], *, temperature: float = 0.3,
                   max_retries: int = 2, response_format: Optional[dict] = None) -> str:
        """
        非流式对话。返回模型文本。

        调用顺序：
            主模型（重试 max_retries 次）→ 仍失败且备用模型已配置 → 备用模型再试**一次**
            → 仍失败抛 LLMUnavailableError，由调用方决定降级策略。

        ⚠️ 运行策略：本模型为远程 API（非本地部署、按 token 计费），调用前先扣预算，
        超限直接降级，避免一次运行演变成完整一轮全量流程（见 base/run_policy.py）。
        """
        if not self.available:
            raise LLMUnavailableError(
                "DeepSeek 未启用或密钥缺失，无法调用大模型",
                tool="llm_client", detail="LLM_ENABLED=false 或 LLM_API_KEY 缺失",
            )
        self._spend_budget("llm_client.chat")

        try:
            text = await self._chat_with(self._ensure_client(), self.model, messages,
                                         temperature, max_retries, response_format)
            self._record_llm_metric(True)
            return text
        except LLMUnavailableError as primary_exc:
            # 账户级失败（余额不足 / 鉴权）：切备用模型用的是同一把 key，必然同样失败
            if not getattr(primary_exc, "retryable", True):
                self._record_llm_metric(False)
                raise
            # 备用模型未配置 → 行为与切换前完全一致（零回归）
            if not self._switch_to_fallback():
                self._record_llm_metric(False)
                raise LLMUnavailableError(
                    _IDENTITY_FALLBACK_MSG,
                    tool="llm_client", detail=f"已重试 {max_retries} 次：{primary_exc}",
                ) from primary_exc

            try:
                text = await self._chat_with(self._ensure_fallback_client(),
                                             self.fallback_model, messages,
                                             temperature, 0, response_format)
            except LLMUnavailableError as fallback_exc:
                self._record_llm_metric(False)
                raise LLMUnavailableError(
                    _FALLBACK_FAILED_MSG,
                    tool="llm_client",
                    detail=f"主模型 {self.primary_model}: {primary_exc}；"
                           f"备用模型 {self.fallback_model}: {fallback_exc}",
                ) from fallback_exc
            # 先记指标（此时 _using_fallback 仍为 True → 记为 success_fallback），再恢复主模型
            self._record_llm_metric(True)
            self._switch_back_to_primary()
            return text

    async def _chat_with(self, client: AsyncOpenAI, model_name: str,
                         messages: List[Dict[str, str]], temperature: float,
                         max_retries: int, response_format: Optional[dict]) -> str:
        """
        用指定客户端/模型调用一次对话（含指数退避重试）。

        失败（重试耗尽）时抛 LLMUnavailableError，由 chat() 决定是否切备用模型。
        """
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                kwargs: Dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temperature,
                    "timeout": self.timeout,
                }
                if response_format:
                    kwargs["response_format"] = response_format
                resp = await client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                return content.strip()
            except Exception as exc:  # noqa: BLE001 - 需兜住所有 SDK 异常
                last_exc = exc
                # 账户级失败（402 余额不足 / 401/403 鉴权）→ 立即停止，不重试
                if _is_account_level_error(exc):
                    logger.error(
                        f"[LLM 账户级失败] {model_name}: {exc} —— "
                        f"不重试、不切备用模型（重试必然同样失败）"
                    )
                    raise LLMUnavailableError(
                        "DeepSeek 账户不可用（余额不足或密钥失效），已停止重试",
                        tool="llm_client", detail=str(exc), retryable=False,
                    ) from exc
                if attempt < max_retries:
                    backoff = 0.8 * (2 ** attempt)
                    logger.warning(f"[LLM 重试] {model_name} 第 {attempt + 1}/{max_retries} 次失败: "
                                   f"{exc}，{backoff:.1f}s 后重试")
                    await asyncio.sleep(backoff)

        raise LLMUnavailableError(
            f"模型 {model_name} 调用失败（已重试 {max_retries} 次）",
            tool="llm_client", detail=str(last_exc),
        )

    async def chat_stream(self, messages: List[Dict[str, str]], *,
                          temperature: float = 0.3) -> AsyncGenerator[str, None]:
        """
        流式对话。逐 token 产出文本。

        异常兜底（降级语义与切换前**完全一致**，任何情况都不向上抛异常）：
            主模型流失败且尚未产出任何 token → 切备用模型重试一次
            → 仍失败产出友好提示文案，保证 SSE 不中断。
        """
        if not self.available:
            yield "（大模型服务未启用，当前为降级模式）"
            return

        # 运行策略：流式同样先扣预算；超限直接给降级文案，不发起远程请求
        try:
            run_policy.llm.spend(where="llm_client.chat_stream")
        except RunPolicyError as exc:
            logger.warning(f"运行策略拦截流式 LLM 调用：{exc}")
            yield "（本次运行已达最小验证的远程调用上限，已停止继续生成。）"
            return

        emitted = 0
        account_level = False
        try:
            async for token in self._stream_with(self._ensure_client(), self.model,
                                                 messages, temperature):
                emitted += 1
                yield token
            self._record_llm_metric(True)
            return
        except Exception as exc:  # noqa: BLE001
            account_level = _is_account_level_error(exc)
            logger.error(f"[LLM 流式] 主模型 {self.model} 调用失败: {exc}"
                         f"{'（账户级，不切备用）' if account_level else ''}")

        # token 已开始产出时不切换（避免重复内容）；仅在完全没产出时切备用模型
        # 账户级失败（402/401/403）切备用模型用的是同一把 key，必败 —— 直接放弃
        if emitted == 0 and not account_level and self._switch_to_fallback():
            try:
                async for token in self._stream_with(self._ensure_fallback_client(),
                                                     self.fallback_model,
                                                     messages, temperature):
                    emitted += 1
                    yield token
                self._record_llm_metric(True)
                self._switch_back_to_primary()
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[LLM 流式] 备用模型 {self.fallback_model} 调用失败: {exc}")

        self._record_llm_metric(False)
        yield f"（抱歉，餐饮助手服务暂时不可用，请稍后重试或拨打咨询热线：{config.SERVICE_PHONE}）"

    async def _stream_with(self, client: AsyncOpenAI, model_name: str,
                           messages: List[Dict[str, str]],
                           temperature: float) -> AsyncGenerator[str, None]:
        """用指定客户端/模型发起一次流式调用（异常交由调用方处理）"""
        stream = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=temperature,
            timeout=self.timeout,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            token = getattr(delta, "content", None)
            if token:
                yield token

    async def chat_json(self, messages: List[Dict[str, str]], *, temperature: float = 0.1,
                        default: Optional[Any] = None) -> Any:
        """
        结构化 JSON 输出。用于意图识别、需求解析、情感分析等需要结构化返回的场景。

        JSON 解析三级兜底：
            1. 直接 json.loads
            2. 剥离 ```json ``` 代码块后重试
            3. 正则抽取首个 {...} 或 [...] 片段
        全部失败 → 返回 default（不抛异常，保证 Agent 节点继续执行）

        说明：LLMUnavailableError 只在「主模型 + 备用模型都失败」后才抛出，
              由上层降级为规则引擎，这里不吞异常。
        """
        try:
            text = await self.chat(
                messages, temperature=temperature,
                response_format={"type": "json_object"},
            )
        except LLMUnavailableError:
            raise  # 交给上层降级为规则引擎，这里不吞

        # Level 1：直接解析
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

        # Level 2：剥离 markdown 代码块
        cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            pass

        # Level 3：抽取首个 JSON 片段
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning(f"[LLM JSON] 解析失败，返回默认值。原始文本前 200 字: {text[:200]!r}")
        return default

    async def health_check(self) -> dict:
        """连通性探测：供启动自检与 /health 接口调用"""
        if not self.available:
            return {"ok": False, "reason": "LLM 未启用或密钥缺失", "model": self.model}
        try:
            text = await self.chat(
                [{"role": "user", "content": "回复两个字：在线"}],
                temperature=0.0, max_retries=0,
            )
            return {"ok": True, "model": self.model, "echo": text[:20]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc), "model": self.model}


# ==================== 全局单例 ====================
# 全项目共用一个客户端实例（复用连接池，避免重复握手）
llm_client = DeepSeekClient()


if __name__ == '__main__':
    # 自检：打印密钥脱敏状态、主/备模型配置与连通性
    print("客户端可用：", llm_client.available)
    print("密钥脱敏：", DeepSeekClient.mask_key(config.LLM_API_KEY))
    print("主模型：", llm_client.primary_model)
    print("备用模型已启用：", llm_client.fallback_enabled, "|", llm_client.fallback_model or "<未配置>")
    print("当前生效模型：", llm_client.active_model, "| 是否已切换备用：", llm_client.using_fallback)

    async def _probe():
        result = await llm_client.health_check()
        print("连通性探测：", result)
        print("探测后生效模型：", llm_client.active_model,
              "| 是否已切换备用：", llm_client.using_fallback)

    asyncio.run(_probe())
