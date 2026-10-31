"""
    Orchestrator 编排器：agents/orchestrator.py

    职责（需求文档 3.2）：
        「意图路由 + 单 Agent 直达 + 多 Agent 串联 Pipeline」

    核心能力：
        1. 意图识别（9 类餐饮意图）—— 规则快路径 → 结果缓存 → LLM → 规则关键词兜底
        2. 单 Agent 直达 —— 单一意图直接路由到对应 Agent
        3. 多 Agent 串联 Pipeline —— 复杂请求（如"从推荐到评价全流程处理"）按序串联
            OrderAgent → KitchenAgent → ReviewAgent → SafetyAgent，前一 Agent 的 State 作为后一个的初始输入
        4. 上下文传递 —— Agent 间的上下文由 Orchestrator 汇总转交（需求 4.4）
        5. 长期记忆注入 —— 任务启动时加载门店偏好 + 顾客画像（需求 3.6 ④）

    流程（需求文档 3.2 / 4.4）：
        用户输入 → Orchestrator 意图路由 → [OrderAgent] → [KitchenAgent] → [ReviewAgent]
                 → 结果汇总 → SSE 返回
"""
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from base.config import config
from base.logger import logger
# Agent 实例
from agents.kitchen_agent import kitchen_agent
from agents.order_agent import order_agent
from agents.review_agent import review_agent
from agents.safety_agent import safety_agent
from agents.state import initial_state
from memory.checkpointer import build_thread_id
from memory.store import get_store
# 提示词
from prompts.templates import Prompts
# 意图结果缓存（Redis 优先，未启用时退化为进程内字典）
from tools.cache import cache
# LLM 客户端
from tools.llm_client import llm_client


# 意图识别规则关键词（LLM 不可用时的兜底）
INTENT_KEYWORDS: Dict[str, List[str]] = {
    "dish_recommend": ["推荐", "招牌", "套餐", "吃什么", "有什么菜", "搭配", "点菜建议"],
    "smart_order": ["点一份", "帮我点", "下单", "来一份", "来份", "要一份", "点餐"],
    "allergen_query": ["过敏", "含不含", "有没有花生", "麸质", "忌口", "能不能吃"],
    "kitchen_dispatch": ["后厨", "排队", "出餐", "排产", "安排顺序", "几单", "多久能上"],
    "inventory_check": ["库存", "还剩", "够不够", "快用完", "备料", "缺什么", "补货"],
    "review_analysis": ["评价", "好评", "差评", "点评", "顾客反馈", "评论"],
    "improvement_advice": ["改进", "怎么提升", "建议", "优化", "怎么办", "改善"],
    "food_safety_query": ["食品安全", "法规", "标准", "规范", "国标", "消毒", "留样",
                          "冷藏", "冷冻", "温度", "保质期", "追溯", "台账", "处罚",
                          "合规", "事故", "采购", "验收", "怎么报备"],
    "nutrition_query": ["营养", "热量", "卡路里", "蛋白质", "脂肪", "碳水", "健康",
                        "低脂", "低卡", "成分", "减肥", "膳食"],
}

# ==================== 意图识别快路径与缓存 ====================
# 设计意图：意图识别原本每次都走一次 LLM（单次 2~4 秒），而门店高频问题高度重复
#（"有什么招牌菜""宫保鸡丁含花生吗"）。两级优化：
#   ① 规则快路径：强信号 + 短文本 + 无复合诉求 → 直接用正则判定，免 LLM 往返；
#   ② 结果缓存  ：以「门店 + 问题摘要」为 key 缓存 LLM 判定结果，TTL 内重复问题直接复用。
# 两者都只影响「怎么得出意图」，不改变任何 Agent 的行为与接口口径。
_FAST_PATTERNS: List[Tuple[str, List[str]]] = [
    (r"(有什么|有哪些|推荐|招牌|特色).{0,6}(菜|套餐)", ["dish_recommend"]),
    (r"(含不含|有没有|能不能吃|过敏).{0,8}(花生|坚果|海鲜|牛奶|鸡蛋|麸质|虾|蟹|芒果)", ["allergen_query"]),
    (r"(多少)(卡|大卡|千卡)|热量|营养成分|低脂|低卡|高蛋白|膳食纤维", ["nutrition_query"]),
    (r"留样|消毒|生熟分开|冷藏|冷冻|台账|追溯|食品安全法|国标|GB\s?\d+", ["food_safety_query"]),
    (r"催菜|催一下|出餐顺序|后厨.{0,4}(安排|排队|优先)|排产", ["kitchen_dispatch"]),
    (r"库存|还剩多少|够不够|缺什么|补货|备料", ["inventory_check"]),
    (r"差评|好评|评价|满意度|顾客反馈|投诉", ["review_analysis"]),
]
# 出现这些词说明是复合诉求，必须交给 LLM 判断串联顺序，不能走快路径
_COMPOUND_MARKERS = ("另外", "同时", "顺便", "并且", "然后", "再帮", "全流程",
                     "从推荐到", "一条龙", "都处理")
_FAST_MAX_CHARS = 40       # 只对短查询启用快路径
_INTENT_CACHE_TTL = 600    # 意图缓存 10 分钟（门店高频问题足够覆盖）


class Orchestrator:
    """
    系统编排器：意图路由 + 多 Agent 串联 Pipeline。

    使用方式：
        orch = Orchestrator()
        result = await orch.run("帮我推荐10人聚餐套餐", tenant_id="store_001", user_id="waiter_01")
    """

    # Agent 名称映射
    AGENTS = {
        "order": ("OrderAgent", order_agent),
        "kitchen": ("KitchenAgent", kitchen_agent),
        "review": ("ReviewAgent", review_agent),
        "safety": ("SafetyAgent", safety_agent),
    }

    def __init__(self):
        logger.info("Orchestrator 初始化完成：4 个 Agent（OrderAgent / KitchenAgent / ReviewAgent / SafetyAgent）")

    # ==================== 意图识别 ====================

    async def classify_intent(self, user_input: str,
                              history_summary: str = "",
                              tenant_id: str = "") -> Dict[str, Any]:
        """
        识别用户意图（9 类）。

        识别顺序：规则快路径 → 结果缓存 → LLM → 规则兜底。
        快路径与缓存都只省掉 LLM 往返，判定结果的结构与 LLM 分支完全一致
        （多一个 source 字段，标明本次判定来自 rule_fast / cache / llm / rule_fallback）。
        """
        # ⓪ 规则快路径：短查询 + 强信号 + 无复合诉求，直接判定
        fast = self._fast_classify(user_input)
        if fast:
            logger.info(f"[Orchestrator] 规则快路径命中：{fast['intents']}（免 LLM 往返）")
            return fast

        # ① 结果缓存：同一门店在 TTL 内问同样的问题，直接复用上次判定
        cache_key = ""
        if user_input and user_input.strip():
            digest = hashlib.md5(user_input.strip().encode("utf-8")).hexdigest()[:16]
            cache_key = f"intent:{tenant_id or 'global'}:{digest}"
            cached = cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("intents"):
                logger.info(f"[Orchestrator] 意图缓存命中：{cached['intents']}"
                            f"（Pipeline={cached.get('is_pipeline')}）")
                return {**cached, "source": "cache"}

        # ② LLM 识别
        try:
            prompt = Prompts.ORCHESTRATOR_ROUTE.format(
                user_input=user_input, history_summary=history_summary or "无")
            result = await llm_client.chat_json(
                [{"role": "system", "content": "你是餐饮系统调度中枢，只输出 JSON。"},
                 {"role": "user", "content": prompt}],
                temperature=0.1, default=None,
            )
            if isinstance(result, dict) and result.get("intents"):
                # 校验意图合法性与路由可行性
                valid = [i for i in result["intents"] if i in config.INTENT_ROUTES]
                if valid:
                    result["intents"] = valid
                    result.setdefault("primary_intent", valid[0])
                    result["degraded"] = False
                    result["source"] = "llm"
                    if cache_key:
                        cache.set(cache_key,
                                  {k: v for k, v in result.items() if k != "source"},
                                  ttl=_INTENT_CACHE_TTL)
                    logger.info(f"[Orchestrator] LLM 识别意图：{valid}"
                                f"（Pipeline={result.get('is_pipeline')}）")
                    return result
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Orchestrator] LLM 意图识别失败，降级为规则: {exc}")

        # ③ 规则兜底
        return self._rule_classify(user_input)

    def _fast_classify(self, user_input: str) -> Optional[Dict[str, Any]]:
        """
        规则快路径：只处理「短、单意图、强信号」的查询。

        保守条件（任一不满足就返回 None，交回 LLM）：
            - 文本长度 ≤ 40：长文本常含多个诉求；
            - 不含复合标记词（另外/同时/全流程…）：复合诉求必须由 LLM 排串联顺序；
            - 正则只命中唯一一类意图。
        """
        text = (user_input or "").strip()
        if not text or len(text) > _FAST_MAX_CHARS:
            return None
        if any(marker in text for marker in _COMPOUND_MARKERS):
            return None

        hits: List[str] = []
        for pattern, intents in _FAST_PATTERNS:
            if re.search(pattern, text):
                hits.extend(intents)
        hits = list(dict.fromkeys(hits))
        if len(hits) != 1:
            return None

        intent = hits[0]
        return {
            "intents": [intent],
            "primary_intent": intent,
            "is_pipeline": False,
            "reason": "规则快路径命中强信号关键词（免 LLM 往返）",
            "degraded": False,
            "source": "rule_fast",
        }

    def _rule_classify(self, user_input: str) -> Dict[str, Any]:
        """规则意图识别（降级）：多关键词命中，按命中数与优先级排序"""
        text = user_input or ""
        # 逗号/顿号/空格切分，判断是否存在多个独立诉求
        intents_hit: List[str] = []
        for intent, keywords in INTENT_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                intents_hit.append(intent)

        # 顺序修正：按业务执行顺序排列（点餐 → 后厨 → 评价）
        order_seq = ["dish_recommend", "smart_order", "allergen_query",
                     "kitchen_dispatch", "inventory_check",
                     "review_analysis", "improvement_advice",
                     "food_safety_query", "nutrition_query"]
        intents_hit = sorted(set(intents_hit), key=lambda i: order_seq.index(i) if i in order_seq else 99)

        if not intents_hit:
            intents_hit = [config.DEFAULT_INTENT]

        # 判定是否 Pipeline：命中跨多个 Agent 的意图
        agents_hit = {config.get_agent_for_intent(i) for i in intents_hit}
        is_pipeline = len(agents_hit) > 1 or any(
            kw in text for kw in ["全流程", "从推荐到", "一条龙", "整体处理", "全部都"])

        logger.info(f"[Orchestrator] 规则识别意图：{intents_hit}（Pipeline={is_pipeline}）")
        return {
            "intents": intents_hit,
            "primary_intent": intents_hit[0],
            "is_pipeline": is_pipeline,
            "reason": "规则关键词匹配" + ("，检测到多 Agent 诉求" if is_pipeline else ""),
            "degraded": True,
        }

    # ==================== 长期记忆注入 ====================

    async def _load_memory(self, tenant_id: str, user_id: str,
                           customer_id: str = "") -> Tuple[Dict, Dict]:
        """
        加载长期记忆（需求 3.6 ④ 读取时机）：门店偏好 + 顾客画像。
        """
        try:
            store = await get_store()
            preferences = await store.load_store_preferences(tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Orchestrator] 门店偏好加载失败: {exc}")
            preferences = {}

        customer_history = {}
        if customer_id:
            try:
                store = await get_store()
                profile = await store.load_customer_profile(tenant_id, customer_id, user_id)
                if profile:
                    customer_history = profile
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Orchestrator] 顾客画像加载失败: {exc}")

        return preferences, customer_history

    # ==================== 主流程 ====================

    async def run(self, user_input: str, tenant_id: str, user_id: str = "",
                  role: str = "waiter", thread_id: Optional[str] = None,
                  customer_id: str = "", history: Optional[List[Dict]] = None,
                  human_confirmed: bool = False,
                  manager_confirmed: bool = False,
                  manager_request_revise: bool = False,
                  priority_adjustment: Optional[str] = None,
                  allergen_notice_confirmed: bool = False,
                  notice_channel: str = "verbal",
                  agent_sequence: Optional[List[str]] = None,
                  context_extractors: Optional[Dict[str, Any]] = None,
                  seed_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        执行一次完整任务（意图路由 → Agent 执行 → 结果汇总）。

        参数：
            user_input:        用户输入
            tenant_id:         门店 ID（隔离键）
            user_id:           员工 ID（隔离键）
            role:              角色（waiter/kitchen/manager）
            thread_id:         任务 ID（Checkpointer 隔离键），不传则自动生成
            customer_id:       顾客 ID（用于加载顾客画像）
            history:           历史对话（短期记忆）
            human_confirmed:   后厨主管是否已确认优先级
            manager_confirmed: 店长是否已确认差评回复
            manager_request_revise: 店长要求重写差评回复（回到 generate_report）
            allergen_notice_confirmed: 服务员是否已完成过敏原口头告知并获顾客确认
            notice_channel:    告知渠道（verbal/sms/system）
            agent_sequence:    【声明式】显式指定 Pipeline 阶段与顺序。
                               例：["kitchen", "review"] 可跳过 OrderAgent（适用于
                               「只查后厨并分析评价」这类不需要点餐的组合）。
                               传 None 时由意图自动推导（保持原行为）。
            context_extractors:【声明式】覆盖内置上下文提取器，key ∈ {order, kitchen, review}。
            seed_context:      【声明式】为下游阶段预置上下文（如跳过 order 时手动提供
                               pending_orders，供 KitchenAgent 直接排产）。

        返回：
            {answer, agent_results, intents, is_pipeline, degraded_tools, ...}
        """
        thread_id = thread_id or build_thread_id(tenant_id, user_id)
        logger.info("=" * 70)
        logger.info(f"[Orchestrator] 任务开始 thread={thread_id} 门店={tenant_id} 员工={user_id} 角色={role}")
        logger.info(f"[Orchestrator] 用户输入：{user_input[:100]}")
        if agent_sequence:
            logger.info(f"[Orchestrator] 声明式 Pipeline 阶段：{' → '.join(agent_sequence)}")

        # Step 1：加载长期记忆
        preferences, customer_history = await self._load_memory(tenant_id, user_id, customer_id)

        # Step 2：意图识别
        history_summary = ""
        if history:
            history_summary = "；".join(
                f"Q:{h.get('question', '')[:20]} A:{h.get('answer', '')[:20]}" for h in history[-2:])
        intent_result = await self.classify_intent(user_input, history_summary, tenant_id)
        intents = intent_result.get("intents", [config.DEFAULT_INTENT])
        is_pipeline = intent_result.get("is_pipeline", False)

        # Step 3：组装初始 State
        state = initial_state(
            tenant_id=tenant_id, user_id=user_id, role=role, thread_id=thread_id,
            user_input=user_input,
            intent=intents[0], intents=intents,
            store_preferences=preferences,
            customer_history=customer_history,
            human_confirmed=human_confirmed,
            manager_confirmed=manager_confirmed,
            manager_request_revise=manager_request_revise,
            priority_adjustment=priority_adjustment or "",
            allergen_notice_confirmed=allergen_notice_confirmed,
            notice_channel=notice_channel,
        )
        if history:
            state["messages"] = history
        # 声明式：预置上下文（如跳过 order 时手动提供 pending_orders）
        if seed_context:
            state.update(seed_context)

        # Step 4：路由执行
        if agent_sequence or is_pipeline:
            result_state, agent_results = await self._run_pipeline(
                state, intents,
                agent_sequence=agent_sequence,
                context_extractors=context_extractors,
            )
        else:
            result_state, agent_results = await self._run_single(state, intents[0])

        # Step 5：结果汇总
        return self._aggregate(result_state, agent_results, intent_result)

    async def _run_single(self, state: Dict[str, Any],
                          intent: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """单 Agent 直达"""
        agent_key = config.get_agent_for_intent(intent)
        agent_name, agent = self.AGENTS[agent_key]
        logger.info(f"[Orchestrator] 单 Agent 路由：{intent} → {agent_name}")

        result_state = await agent.ainvoke(state, thread_id=state.get("thread_id"))
        agent_results = {agent_key: agent.format_result(result_state)}
        return result_state, agent_results

    async def _run_pipeline(self, state: Dict[str, Any],
                            intents: List[str],
                            agent_sequence: Optional[List[str]] = None,
                            context_extractors: Optional[Dict[str, Any]] = None
                            ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        多 Agent 串联 Pipeline（需求 4.4）：
            OrderAgent → KitchenAgent → ReviewAgent
        前一个 Agent 的 State 作为下一个 Agent 的初始输入（上下文传递）。

        【声明式扩展】agent_sequence 可显式指定阶段与顺序：
            · 传 None → 由意图自动推导（原行为，保证向后兼容）
            · 传 ["kitchen", "review"] → 跳过 OrderAgent，直接排产后评析
              （此时 KitchenAgent 需要的 pending_orders 可由 seed_context 预置，
                或由 extractors 中自定义的 order 提取器提供）

        Human-in-the-Loop 处理：
            Pipeline 是「一次请求内串行跑完多个 Agent」，无法在中间停下来等人输入
            （SSE/HTTP 请求不能挂起等人工）。因此 Pipeline 模式下会打开
            pipeline_mode 开关，让各 Agent 跳过 interrupt，只把「需要确认」的
            信息累积到 pending_actions，由前端在整条流水线结束后统一提示。
        """
        # 按业务顺序确定要执行的 Agent 序列
        if agent_sequence:
            # 声明式：过滤非法 key，保留调用方指定顺序
            agent_sequence = [k for k in agent_sequence if k in self.AGENTS]
            logger.info(f"[Orchestrator] 使用声明式 Agent 序列：{agent_sequence}")
        else:
            agent_sequence = self.build_agent_sequence(intents)

        logger.info(f"[Orchestrator] 多 Agent 串联 Pipeline：{' → '.join(agent_sequence)}")

        agent_results: Dict[str, Any] = {}
        carried_context: Dict[str, Any] = {}
        suspended_stages: List[Dict[str, Any]] = []

        for idx, key in enumerate(agent_sequence, 1):
            agent_name, agent = self.AGENTS[key]
            logger.info(f"[Orchestrator] Pipeline 阶段 {idx}/{len(agent_sequence)}：{agent_name}")

            # 前序阶段已请求人工介入 → 该阶段及其后续仍继续执行，
            # 但把「未确认」状态透传下去，由各 Agent 自行决定是否降级处理。
            prior_pending = bool(suspended_stages)

            # 上下文传递：把前面 Agent 的产出注入当前 State
            stage_state = {
                **state,
                "pipeline_stage": idx,
                "pipeline_mode": True,          # 关键：Pipeline 下不中断等待人工
                "prior_pending": prior_pending,
                "pipeline_context": carried_context,
                # 跨 Agent 传递排除清单，避免下游重复推荐已售罄/缺料菜品
                "excluded_dishes": state.get("excluded_dishes", []),
                "excluded_ingredients": state.get("excluded_ingredients", []),
                **carried_context,
            }
            stage_state["thread_id"] = f"{state.get('thread_id')}_{key}"

            # 显式清空上一次循环可能残留的中断标记，避免跨阶段污染
            for flag in ("need_human_confirm", "need_manager_review"):
                stage_state.pop(flag, None)

            try:
                result_state = await agent.ainvoke(stage_state, thread_id=stage_state["thread_id"])
                agent_results[key] = agent.format_result(result_state)
                # 提取传给下游的上下文（声明式提取器，可被 context_extractors 覆盖）
                carried_context.update(self._extract_context(key, result_state,
                                                            extractors=context_extractors))
                state.update({k: v for k, v in result_state.items()
                              if k not in ("messages",)})  # 合并回主 State

                # 记录本阶段是否请求人工确认（不中断，仅累积）
                if result_state.get("need_human_confirm") or result_state.get("need_manager_review"):
                    suspended_stages.append({
                        "stage": key,
                        "agent": agent_name,
                        "need_human_confirm": bool(result_state.get("need_human_confirm")),
                        "need_manager_review": bool(result_state.get("need_manager_review")),
                        "confirm_reason": result_state.get("confirm_reason", []),
                    })
                    logger.warning(f"[Orchestrator] Pipeline 阶段 {agent_name} "
                                   f"请求人工确认（已挂起，不阻断后续阶段）")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[Orchestrator] Pipeline 阶段 {agent_name} 失败: {exc}")
                agent_results[key] = {"error": str(exc), "answer": f"{agent_name} 执行失败"}
                state.setdefault("errors", []).append(f"{agent_name}: {exc}")

        # 把 Pipeline 内累积的待确认项挂到主 State，供 _aggregate 汇总
        if suspended_stages:
            state["pipeline_pending_stages"] = suspended_stages
            # 保持与单 Agent 一致的 pending_actions 语义：只要有任一阶段需要确认就置位
            state["need_human_confirm"] = any(s["need_human_confirm"] for s in suspended_stages)
            state["need_manager_review"] = any(s["need_manager_review"] for s in suspended_stages)

        return state, agent_results

    @staticmethod
    def _extract_context(agent_key: str, state: Dict[str, Any],
                         extractors: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Agent 间上下文传递（需求 4.4：「上下文传递」）—— 声明式实现。

        原实现为硬编码 if/elif，导致无法自定义 Pipeline 组合
        （例如「跳过 OrderAgent 只跑 kitchen + review」时，kitchen 需要的
        pending_orders 没有任何来源）。

        现在改为「声明式提取器表」+ 可选覆盖：
            _CONTEXT_EXTRACTORS = {"order": fn, "kitchen": fn, "review": fn}
        调用方可通过 extractors 参数注入自定义提取器（按 key 覆盖）。

        内置提取器：
            order   → recommended_dishes + pending_orders（供 KitchenAgent 排产）
            kitchen → ingredient_needs + station_assignment（供下游运营参考）
            review  → 无（终态，不需向后传递）
        """
        table = dict(Orchestrator._CONTEXT_EXTRACTORS)
        if extractors:
            table.update(extractors)
        extractor = table.get(agent_key)
        if extractor is None:
            return {}
        try:
            return extractor(state) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Orchestrator] 上下文提取失败（{agent_key}）: {exc}")
            return {}

    # -------------------- 内置上下文提取器 --------------------

    @staticmethod
    def _ctx_from_order(state: Dict[str, Any]) -> Dict[str, Any]:
        """OrderAgent → 下游：推荐菜品 + 待排产订单"""
        dishes = state.get("recommended_dishes", [])
        needs = state.get("customer_needs", {})
        return {
            "recommended_dishes": dishes,
            "pending_orders": [{
                "订单号": state.get("order_id", "PIPELINE"),
                "桌号": needs.get("table_no") or "周年庆",
                "菜品列表": [{"菜品名称": d.get("dish_name"), "数量": d.get("quantity", 1),
                              "单价": d.get("price", 0)} for d in dishes],
                "order_time": state.get("created_at"),
                "member_level": needs.get("member_level") or "普通",
                "allergens": state.get("allergen_result", {}).get("allergens_found", []),
            }] if dishes else [],
        }

    @staticmethod
    def _ctx_from_kitchen(state: Dict[str, Any]) -> Dict[str, Any]:
        """KitchenAgent → 下游：食材需求 + 工位分配"""
        return {
            "ingredient_needs": state.get("inventory_result", {}).get("required", {}),
            "station_assignment": state.get("station_assignment", {}),
        }

    @staticmethod
    def _ctx_from_review(state: Dict[str, Any]) -> Dict[str, Any]:
        """ReviewAgent → 下游：终态，不向后传递"""
        return {}

    @staticmethod
    def _ctx_from_safety(state: Dict[str, Any]) -> Dict[str, Any]:
        """SafetyAgent → 下游：终态，不向后传递"""
        return {}

    #: 声明式提取器表（可被 run(..., context_extractors=...) 覆盖）
    _CONTEXT_EXTRACTORS: Dict[str, Any] = {}
    # ==================== Pipeline 阶段裁剪（声明式） ====================

    @staticmethod
    def build_agent_sequence(intents: List[str],
                             default_order: Optional[List[str]] = None) -> List[str]:
        """
        由意图列表推导 Agent 执行序列（声明式，供自定义 Pipeline 组合使用）。

        用法示例：
            # 只跑 kitchen + review（跳过 OrderAgent）
            seq = Orchestrator.build_agent_sequence(["kitchen_dispatch", "review_analysis"])
            # → ["kitchen", "review"]
        """
        order = default_order or ["order", "kitchen", "review", "safety"]
        seq: List[str] = []
        for intent in intents:
            key = config.get_agent_for_intent(intent)
            if key and key not in seq:
                seq.append(key)
        seq.sort(key=lambda k: order.index(k) if k in order else 99)
        return seq

    def _aggregate(self, state: Dict[str, Any], agent_results: Dict[str, Any],
                   intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """汇总多 Agent 结果，生成统一的返回结构"""
        # 主回答：单 Agent 取该 Agent 的 answer；Pipeline 则拼接各阶段
        if len(agent_results) == 1:
            answer = next(iter(agent_results.values())).get("answer", "")
        else:
            parts = []
            stage_titles = {"order": "智能点餐", "kitchen": "后厨调度", "review": "评价分析",
                            "safety": "食安问答"}
            for key in ["order", "kitchen", "review", "safety"]:
                if key in agent_results:
                    parts.append(f"# {stage_titles.get(key, key)}\n\n"
                                 f"{agent_results[key].get('answer', '')}")
            answer = "\n\n---\n\n".join(parts)

        # 汇总降级工具与错误（可观测性）
        degraded = list(dict.fromkeys(state.get("degraded_tools", [])))
        errors = state.get("errors", [])

        # 需要人工确认的标记（Human-in-the-Loop）
        pending_actions = []
        if state.get("need_human_confirm"):
            pending_actions.append({
                "type": "kitchen_priority_confirm",
                "message": "后厨排产方案需要主管确认",
                "reason": state.get("confirm_reason", []),
            })
        if state.get("need_manager_review"):
            pending_actions.append({
                "type": "review_reply_confirm",
                "message": "有严重差评需要店长确认回复",
                "drafts": state.get("reply_drafts", []),
            })

        # Pipeline 模式下各阶段的待确认项（不阻断流程，结束后统一提示）
        pipeline_pending = state.get("pipeline_pending_stages", [])

        return {
            "answer": answer,
            "thread_id": state.get("thread_id"),
            "tenant_id": state.get("tenant_id"),
            "intents": intent_result.get("intents", []),
            "primary_intent": intent_result.get("primary_intent"),
            "intent_reason": intent_result.get("reason", ""),
            "is_pipeline": intent_result.get("is_pipeline", False),
            "agents_invoked": list(agent_results.keys()),
            "agent_results": agent_results,
            "order_id": state.get("order_id", ""),
            "allergen_risk": state.get("allergen_risk", "none"),
            "allergen_result": state.get("allergen_result", {}),
            "pending_actions": pending_actions,
            "pipeline_pending_stages": pipeline_pending,
            "needs_manual_review": bool(state.get("needs_manual_review")),
            "degraded_tools": degraded,
            "errors": errors,
        }

    # ==================== 流式接口 ====================

    async def run_stream(self, user_input: str, tenant_id: str, user_id: str = "",
                         role: str = "waiter", **kwargs):
        """
        流式执行（供 SSE 接口使用）。
        产出：(event_type, data) 元组，event_type ∈ {intent, stage, token, result, error}

        实现说明：Agent 内部节点是确定性的（规则引擎 + LLM 工具），
        因此流式输出分为「阶段进度事件」+「最终回答的逐字输出」两部分。
        """
        try:
            # 阶段 1：意图识别
            yield ("intent", {"message": "正在识别意图..."})
            preferences, customer_history = await self._load_memory(
                tenant_id, user_id, kwargs.get("customer_id", ""))
            intent_result = await self.classify_intent(user_input, tenant_id=tenant_id)
            yield ("intent", {
                "intents": intent_result.get("intents", []),
                "primary_intent": intent_result.get("primary_intent"),
                "is_pipeline": intent_result.get("is_pipeline", False),
                "reason": intent_result.get("reason", ""),
                "source": intent_result.get("source", ""),
            })

            # 阶段 2：执行
            # 修复（二次审计）：原先 pipeline 分支硬编码 ["order","kitchen","review"]，
            # 把 SafetyAgent 排除在外——而 9 类意图里 food_safety_query / nutrition_query
            # 本来就归属 safety，导致「从点餐到食安全流程」这类请求的食安环节被静默跳过。
            # 现在按意图识别结果推导 Agent 序列（去重保序），不再写死三件套。
            intents = intent_result.get("intents") or [
                intent_result.get("primary_intent") or config.DEFAULT_INTENT]
            is_pipeline = bool(intent_result.get("is_pipeline"))
            agent_sequence = []
            for intent in intents:
                key = config.get_agent_for_intent(intent)
                if key and key not in agent_sequence:
                    agent_sequence.append(key)
            if not agent_sequence:
                agent_sequence = [config.get_agent_for_intent(config.DEFAULT_INTENT)]

            stage_titles = {"order": "智能点餐 Agent", "kitchen": "后厨调度 Agent",
                            "review": "评价分析 Agent", "safety": "食安问答 Agent"}
            for key in agent_sequence:
                yield ("stage", {"agent": self.AGENTS[key][0], "title": stage_titles.get(key, key)})

            stream_kwargs = dict(kwargs)
            if is_pipeline:
                # 把推导出的序列交给 run()，避免它二次分类后走另一条路径
                stream_kwargs["agent_sequence"] = agent_sequence
            result = await self.run(user_input, tenant_id, user_id, role, **stream_kwargs)

            # 阶段 3：把最终回答逐字流出（打字机效果）
            answer = result.get("answer", "")
            # 按句子/标点切分，模拟流式输出节奏
            import re
            chunks = re.split(r"(?<=[。！？\n])", answer)
            for chunk in chunks:
                if chunk:
                    yield ("token", {"token": chunk})

            # 阶段 4：结果元数据
            yield ("result", result)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Orchestrator] 流式执行失败: {exc}")
            yield ("error", {"message": f"处理失败：{exc}"})


# ==================== 全局单例 ====================
orchestrator = Orchestrator()


# ==================================================================
# 上下文提取器注册（类定义之后，避免前向引用）
# ==================================================================
Orchestrator._CONTEXT_EXTRACTORS = {
    "order": Orchestrator._ctx_from_order,
    "kitchen": Orchestrator._ctx_from_kitchen,
    "review": Orchestrator._ctx_from_review,
    "safety": Orchestrator._ctx_from_safety,
}


if __name__ == '__main__':
    import asyncio

    async def _selftest():
        print("=" * 60)
        print("Orchestrator 意图识别自检（规则模式）")
        print("=" * 60)
        cases = [
            "帮我推荐适合10人聚餐的套餐，有两位素食",
            "现在后厨有多少单在排队，帮我安排出餐顺序",
            "帮我分析一下本周的顾客评价",
            "今天是我们店开业三周年，帮我从菜品推荐到评价分析全流程处理",
        ]
        for text in cases:
            r = orchestrator._rule_classify(text)
            print(f"\n输入：{text}")
            print(f"  意图：{r['intents']}  Pipeline={r['is_pipeline']}")

    asyncio.run(_selftest())
