"""
    平台契约层：agentflow/contracts.py

    职责：定义「垂直领域能力包（Vertical Pack）」的契约——Agent 规格、工具规格、
        意图规格、强制护栏规格，以及它们的合法性校验规则。

    为什么要这一层：
        改造前，4 个 Agent / 15 个工具 / 9 类意图是散落在 base/config.py、
        agents/*/graph.py、tools/registry.py 里的硬编码常量，「餐饮」与「编排」
        是耦合的——换一个业务场景只能复制整个仓库。
        改造后，编排运行时（platform/）+ 领域能力包（verticals/）分离：
        运行时只认契约，业务事实全部下沉为数据（pack.json）。
        校验规则写在这里，而不是写在文档里——文档会过期，代码不会。

    契约的边界（不做的事）：
        · 不描述节点内部逻辑（那是 nodes.py 的职责）
        · 不描述 prompt 文案（那是 prompts/ 的职责）
        · 只描述「装配关系」与「可靠性等级」，因为这才是平台需要管理的对象
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ==================== 枚举常量 ====================

# 工具分类：与 tools/registry.py 的 CATEGORIES 对齐
TOOL_CATEGORIES = ("llm", "rule", "mcp", "external")

# 可靠性等级——平台里最重要的一个字段
#   must_be_exact：结果进入业务判定/写操作，必须确定性，禁止走模型
#   best_effort ：结果只影响表达与建议，允许模型参与
GUARANTEES = ("must_be_exact", "best_effort")

# 护栏类型
GUARD_TYPES = (
    "rule_engine",          # 规则引擎接管判定，模型不参与
    "human_in_the_loop",    # interrupt 挂起，等人工确认后才落库
    "schema_validation",    # Pydantic 校验，挡在写库之前
    "quota",                # 限流/配额，防雪崩
)

# 能力包状态
PACK_STATUS = ("reference", "production", "experimental")


class PackValidationError(ValueError):
    """能力包契约违规。装配期就抛，不允许带病启动。"""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PackValidationError(msg)


# ==================== 规格对象 ====================

@dataclass(frozen=True)
class ToolSpec:
    """单个工具的规格。"""

    name: str
    category: str
    owners: List[str]
    guarantee: str = "best_effort"
    description: str = ""
    # 该工具是否会在外部系统产生副作用（写操作）——决定是否必须挂人工卡点
    has_side_effect: bool = False

    def validate(self) -> None:
        _require(bool(self.name), "工具缺少 name")
        _require(self.category in TOOL_CATEGORIES,
                 f"工具 {self.name} 的 category={self.category} 非法，允许：{TOOL_CATEGORIES}")
        _require(bool(self.owners), f"工具 {self.name} 未声明归属 Agent")
        _require(self.guarantee in GUARANTEES,
                 f"工具 {self.name} 的 guarantee={self.guarantee} 非法，允许：{GUARANTEES}")
        # 硬约束：副作用工具不允许 best_effort——写操作绝不能由概率模型直接落库
        _require(not (self.has_side_effect and self.guarantee != "must_be_exact"),
                 f"工具 {self.name} 有副作用但未声明 must_be_exact，违反写操作护栏")


@dataclass(frozen=True)
class AgentSpec:
    """单个 Agent 的规格。"""

    id: str
    name: str
    node_count: int
    intents: List[str]
    graph_module: str = ""

    def validate(self) -> None:
        _require(bool(self.id), "Agent 缺少 id")
        _require(self.node_count > 0, f"Agent {self.id} 的 node_count 必须为正")
        _require(bool(self.intents), f"Agent {self.id} 未挂任何意图")


@dataclass(frozen=True)
class IntentSpec:
    """意图规格：意图 → Agent 的路由事实。"""

    id: str
    agent: str
    complex: bool = False
    desc: str = ""


@dataclass(frozen=True)
class GuardSpec:
    """护栏规格。"""

    id: str
    type: str
    bypass_llm: bool
    scope: str
    note: str = ""

    def validate(self) -> None:
        _require(self.type in GUARD_TYPES,
                 f"护栏 {self.id} 的 type={self.type} 非法，允许：{GUARD_TYPES}")
        _require(bool(self.scope), f"护栏 {self.id} 未声明 scope")


@dataclass
class VerticalPack:
    """
    垂直领域能力包。

    一个 pack = 一套可被平台装配的业务能力：哪些 Agent、哪些工具、哪些意图、
    哪些不可绕过的护栏，以及在哪个领域里验证过。
    """

    pack_id: str
    display_name: str
    version: str
    status: str
    description: str
    agents: List[AgentSpec] = field(default_factory=list)
    tools: List[ToolSpec] = field(default_factory=list)
    intents: List[IntentSpec] = field(default_factory=list)
    guards: List[GuardSpec] = field(default_factory=list)
    entrypoints: Dict[str, str] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    # ---------- 派生属性 ----------

    @property
    def node_total(self) -> int:
        """全部 Agent 的节点总数。"""
        return sum(a.node_count for a in self.agents)

    @property
    def exact_tool_names(self) -> List[str]:
        """必须确定性执行的工具（禁止走模型）。"""
        return [t.name for t in self.tools if t.guarantee == "must_be_exact"]

    def agent_ids(self) -> List[str]:
        return [a.id for a in self.agents]

    # ---------- 校验 ----------

    def validate(self) -> None:
        """
        全量契约校验。任一条不满足即抛 PackValidationError。

        校验项（按「坏了会怎样」排序）：
            1. 元信息完整 —— 缺 id/version 时装配日志无法追溯
            2. 工具名唯一 —— 重名会让 registry.get() 静默取到错的实现
            3. 工具归属 Agent 真实存在 —— 否则 /api/tools 展示的归属是假的
            4. 意图指向的 Agent 真实存在 —— 否则运行时路由到空，表现为「答非所问」
            5. 每个 Agent 至少挂一个意图 —— 否则是永远进不去的死 Agent
            6. 有副作用的工具必须被人工卡点护栏覆盖 —— 这条是安全底线
        """
        _require(bool(self.pack_id), "能力包缺少 pack_id")
        _require(bool(self.version), f"能力包 {self.pack_id} 缺少 version")
        _require(self.status in PACK_STATUS,
                 f"能力包 {self.pack_id} 的 status={self.status} 非法，允许：{PACK_STATUS}")
        _require(bool(self.agents), f"能力包 {self.pack_id} 未定义任何 Agent")

        # 2. 工具名唯一
        names = [t.name for t in self.tools]
        dup = {n for n in names if names.count(n) > 1}
        _require(not dup, f"能力包 {self.pack_id} 存在重复工具名：{sorted(dup)}")

        for t in self.tools:
            t.validate()
        for a in self.agents:
            a.validate()
        for g in self.guards:
            g.validate()

        agent_ids = set(self.agent_ids())

        # 3. 工具归属
        for t in self.tools:
            unknown = [o for o in t.owners if o not in agent_ids]
            _require(not unknown,
                     f"工具 {t.name} 归属了不存在的 Agent：{unknown}（已定义：{sorted(agent_ids)}）")

        # 4. 意图指向
        for it in self.intents:
            _require(it.agent in agent_ids,
                     f"意图 {it.id} 指向不存在的 Agent：{it.agent}")

        # 5. 死 Agent 检测
        for a in self.agents:
            covered = [i.id for i in self.intents if i.agent == a.id]
            _require(bool(covered), f"Agent {a.id} 没有任何意图指向它，永远不可达")

        # 5b. AgentSpec.intents 与 IntentSpec 双向一致（防止两处各改一半）
        for a in self.agents:
            declared = set(a.intents)
            actual = {i.id for i in self.intents if i.agent == a.id}
            _require(declared == actual,
                     f"Agent {a.id} 声明的意图 {sorted(declared)} 与实际路由 {sorted(actual)} 不一致")

        # 6. 副作用必须有人工卡点
        side_effects = [t.name for t in self.tools if t.has_side_effect]
        if side_effects:
            covers = [g for g in self.guards if g.type == "human_in_the_loop"]
            _require(bool(covers),
                     f"存在副作用工具 {side_effects}，但能力包未声明 human_in_the_loop 护栏")

        # 7. 确定性工具必须有 rule_engine 护栏兜底
        if self.exact_tool_names:
            _require(any(g.type == "rule_engine" and g.bypass_llm for g in self.guards),
                     f"存在 must_be_exact 工具 {self.exact_tool_names}，"
                     f"但未声明 bypass_llm 的 rule_engine 护栏")

    # ---------- 反序列化 ----------

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerticalPack":
        """从 pack.json 解析。字段缺失按空处理，合法性交给 validate()。"""
        return cls(
            pack_id=data.get("pack_id", ""),
            display_name=data.get("display_name", ""),
            version=data.get("version", ""),
            status=data.get("status", "experimental"),
            description=data.get("description", ""),
            agents=[
                AgentSpec(
                    id=a.get("id", ""),
                    name=a.get("name", ""),
                    node_count=int(a.get("node_count", 0)),
                    intents=list(a.get("intents", [])),
                    graph_module=a.get("graph_module", ""),
                )
                for a in data.get("agents", [])
            ],
            tools=[
                ToolSpec(
                    name=t.get("name", ""),
                    category=t.get("category", ""),
                    owners=list(t.get("owners", [])),
                    guarantee=t.get("guarantee", "best_effort"),
                    description=t.get("description", ""),
                    has_side_effect=bool(t.get("has_side_effect", False)),
                )
                for t in data.get("tools", [])
            ],
            intents=[
                IntentSpec(
                    id=i.get("id", ""),
                    agent=i.get("agent", ""),
                    complex=bool(i.get("complex", False)),
                    desc=i.get("desc", ""),
                )
                for i in data.get("intents", [])
            ],
            guards=[
                GuardSpec(
                    id=g.get("id", ""),
                    type=g.get("type", ""),
                    bypass_llm=bool(g.get("bypass_llm", False)),
                    scope=g.get("scope", ""),
                    note=g.get("note", ""),
                )
                for g in data.get("guards", [])
            ],
            entrypoints=dict(data.get("entrypoints", {})),
            evidence=list(data.get("evidence", [])),
        )

    def summary(self) -> Dict[str, Any]:
        """装配日志 / /api/platform 用的摘要。"""
        return {
            "pack_id": self.pack_id,
            "display_name": self.display_name,
            "version": self.version,
            "status": self.status,
            "agents": len(self.agents),
            "nodes": self.node_total,
            "tools": len(self.tools),
            "intents": len(self.intents),
            "guards": len(self.guards),
            "exact_tools": len(self.exact_tool_names),
            "side_effect_tools": len([t for t in self.tools if t.has_side_effect]),
        }
