"""
    能力包加载器：agentflow/pack_loader.py

    职责：
        1. 从 verticals/<pack>/pack.json 读能力包并校验（契约见 contracts.py）
        2. 用「代码事实」反向校验声明的事实——pack.json 说 10 个节点，
           agents/order/graph.py 里就必须真有 10 个节点，不允许文档与代码漂移
        3. 与运行中的工具注册表交叉校验（工具名/分类/归属是否与代码一致）

    设计取舍：
        交叉校验放到「静态扫描」而不是 import 执行，因为装配期不该把
        langgraph / openai 这些重依赖拉起来；正则读源码足够发现漂移，
        真正的行为验证交给 scripts/verify_platform.py 与集成测试。
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

from base.logger import logger
from agentflow.contracts import PackValidationError, VerticalPack

# 项目根目录（platform/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERTICALS_DIR = os.path.join(ROOT, "verticals")

# 匹配 define_nodes() 里返回字典的键："parse_order": parse_order,
_NODE_KEY_RE = re.compile(r'"([a-z0-9_]+)"\s*:\s*[A-Za-z_]')


def discover_pack_files(verticals_dir: str = VERTICALS_DIR) -> List[str]:
    """列出所有能力包描述文件。"""
    if not os.path.isdir(verticals_dir):
        return []
    found = []
    for name in sorted(os.listdir(verticals_dir)):
        path = os.path.join(verticals_dir, name, "pack.json")
        if os.path.isfile(path):
            found.append(path)
    return found


def load_pack(path: str) -> VerticalPack:
    """
    读取并校验单个能力包。

    Raises:
        PackValidationError: JSON 非法或契约违规
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise PackValidationError(f"能力包不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise PackValidationError(f"能力包 JSON 非法（{path}）：{exc}") from exc

    pack = VerticalPack.from_dict(data)
    pack.validate()
    return pack


def probe_agent_node_counts(root: str = ROOT) -> Dict[str, int]:
    """
    静态扫描 agents/<id>/graph.py，数出每个 Agent 实际声明的节点个数。

    实现说明：只取 define_nodes 里 `return { ... }` 这一层字典的键。
    不能对整段做正则——conditional_edges 的键（路由分支名）不是节点，
    混进来会把 order 的 10 个节点数成 19 个。
    """
    counts: Dict[str, int] = {}
    agents_dir = os.path.join(root, "agents")
    if not os.path.isdir(agents_dir):
        return counts

    for name in sorted(os.listdir(agents_dir)):
        graph_py = os.path.join(agents_dir, name, "graph.py")
        if not os.path.isfile(graph_py):
            continue
        try:
            with open(graph_py, "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue

        idx = source.find("def define_nodes")
        if idx < 0:
            continue
        seg = source[idx:]
        ret = seg.find("return {")
        if ret < 0:
            continue
        brace = seg.index("{", ret)
        counts[name] = len(_keys_from_brace(seg, brace))
    return counts


def _keys_from_brace(source: str, start: int) -> List[str]:
    """
    从 `source[start]` 处的 `{` 起做引号感知的括号配对，返回这一层字典的键。

    只收「深度 1 上、且键是合法标识符」的项：
        "parse_order": parse_order,     → 收
        "node": "订单解析",              → 不收（值是中文字符串，不是标识符调用）
        # 注释里的 "xxx": yyy             → 天然被字符串状态机挡住
    """
    if start >= len(source) or source[start] != "{":
        return []
    depth = 0
    in_str = None
    keys: List[str] = []
    buf: List[str] = []
    j = start
    while j < len(source):
        ch = source[j]
        if in_str:
            if ch == "\\":
                j += 2
                continue
            if ch == in_str:
                in_str = None
            elif depth == 1:
                buf.append(ch)
        elif ch in "\"'":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and ch == ":":
            key = "".join(buf).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                keys.append(key)
            buf = []
        elif depth == 1:
            if ch in ",\n":
                buf = []
            else:
                buf.append(ch)
        j += 1
    return list(dict.fromkeys(keys))


def _extract_dict_keys(source: str, var_name: str) -> List[str]:
    """
    从源码中取出 `var_name = { ... }` 这个字面量的顶层键。

    为什么要做括号配对而不是全文正则：tools/*.py 里除了工具字典还有别的常量字典，
    全文正则会把这些键也算成工具，导致「包里没登记」的假告警。
    """
    m = re.search(rf"^{var_name}\s*=\s*\{{", source, re.MULTILINE)
    if not m:
        return []
    return _keys_from_brace(source, source.index("{", m.start()))


def probe_tool_facts(root: str = ROOT) -> Dict[str, Any]:
    """
    静态扫描 tools/ 下四类工具字典，取出 {工具名: 分类}。

    不 import，避免拉起 openai / httpx 等依赖；只认
        LLM_TOOLS = { "llm_classify_intent": llm_classify_intent, ... }
    这一层字典字面量的键。
    """
    files = {
        "llm": ("llm_tools.py", "LLM_TOOLS"),
        "rule": ("rule_tools.py", "RULE_TOOLS"),
        "mcp": ("mcp_tools.py", "MCP_TOOLS"),
        "external": ("external_api.py", "EXTERNAL_TOOLS"),
    }
    facts: Dict[str, Any] = {"tools": {}, "counts": {}}
    tools_dir = os.path.join(root, "tools")

    for category, (fname, var_name) in files.items():
        path = os.path.join(tools_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        names = _extract_dict_keys(source, var_name)
        facts["counts"][category] = len(names)
        for n in names:
            facts["tools"].setdefault(n, category)
    return facts


def cross_check(pack: VerticalPack, root: str = ROOT) -> Dict[str, List[str]]:
    """
    声明 vs 代码 的漂移检查。

    Returns:
        {"errors": [...], "warnings": [...]}
        errors 表示「pack.json 与代码事实矛盾」，warnings 表示「代码里有但包里没登记」
    """
    errors: List[str] = []
    warnings: List[str] = []

    # ① 节点数
    node_facts = probe_agent_node_counts(root)
    for agent in pack.agents:
        actual = node_facts.get(agent.id)
        if actual is None:
            warnings.append(f"Agent {agent.id} 未找到 agents/{agent.id}/graph.py，无法核对节点数")
        elif actual != agent.node_count:
            errors.append(
                f"Agent {agent.id} 声明 {agent.node_count} 个节点，"
                f"但 agents/{agent.id}/graph.py 实际 {actual} 个"
            )

    # ② 工具清单
    tool_facts = probe_tool_facts(root)
    declared = {t.name: t.category for t in pack.tools}
    code_tools = tool_facts.get("tools", {})
    if code_tools:
        missing_in_pack = sorted(set(code_tools) - set(declared))
        missing_in_code = sorted(set(declared) - set(code_tools))
        if missing_in_code:
            errors.append(f"能力包登记了代码里不存在的工具：{missing_in_code}")
        if missing_in_pack:
            warnings.append(f"代码里存在但能力包未登记的工具：{missing_in_pack}")
        for name, cat in declared.items():
            actual = code_tools.get(name)
            if actual and actual != cat:
                errors.append(f"工具 {name} 分类不一致：包内 {cat} / 代码 {actual}")

    return {"errors": errors, "warnings": warnings}


def load_all(verticals_dir: str = VERTICALS_DIR, *, strict: bool = False) -> Dict[str, VerticalPack]:
    """
    扫描并加载全部能力包。

    Args:
        strict: True 时任何漂移都抛异常（CI 用）；False 时只记日志（运行时用）
    """
    packs: Dict[str, VerticalPack] = {}
    for path in discover_pack_files(verticals_dir):
        pack = load_pack(path)
        report = cross_check(pack, ROOT)
        for w in report["warnings"]:
            logger.warning(f"[pack:{pack.pack_id}] {w}")
        for e in report["errors"]:
            logger.error(f"[pack:{pack.pack_id}] {e}")
        if report["errors"] and strict:
            raise PackValidationError(f"能力包 {pack.pack_id} 与代码不一致：{report['errors']}")
        packs[pack.pack_id] = pack
    return packs


def resolve_pack(pack_id: Optional[str] = None) -> Optional[VerticalPack]:
    """
    取得当前要装配的能力包。

    未指定时取环境变量 ACTIVE_PACK，再退化为唯一的 reference 包——
    这是「一个平台、多个垂直领域」的切换点：换场景只换 ACTIVE_PACK。
    """
    packs = load_all()
    if not packs:
        return None
    target = pack_id or os.getenv("ACTIVE_PACK", "").strip()
    if target:
        return packs.get(target)
    for p in packs.values():
        if p.status == "reference":
            return p
    return next(iter(packs.values()))
