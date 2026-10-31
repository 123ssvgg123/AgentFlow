"""
    图结构静态校验：scripts/check_graph_structure.py

    为什么需要这个脚本（二次审计的教训）：
        LangGraph 对「没有出边的节点」不会报错，而是把图**当作正常结束**。
        所以 `StateGraph(...).compile()` 成功、节点数对、边数对，都不代表流程完整——
        KitchenAgent 的 `load_orders` 曾因此静默结束，靠 BaseAgent._reached_terminal
        兜底降级重跑，等于每次调度跑两遍；ReviewAgent 的 `generate_report` 同样
        让 `check_negative` / `save_analysis` 永远不执行。

    本脚本不 import 任何业务模块（不依赖 fastapi/langgraph），只用 ast 解析
    `define_structure()` 的返回值，断言：
        ① 声明的节点都存在，entry 在节点集合内
        ② 每个「非终态」节点至少有一条出边（固定边或条件边）
        ③ 所有节点从 entry 可达
        ④ 所有非终态节点最终都能走到某个终态（无死循环孤岛）

    用法：
        python scripts/check_graph_structure.py          # 全部 Agent
        python scripts/check_graph_structure.py order    # 单个 Agent
    退出码：0 全部通过；1 有结构问题（可接进 CI / selfcheck）
"""
import ast
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / "agents"

# 每个 Agent 的合法终态（没有出边、允许图在此结束的节点）
TERMINAL_NODES: Dict[str, Set[str]] = {
    "order": {"submit_order", "require_manual_review"},
    "kitchen": {"dispatch_orders"},
    "review": {"save_analysis"},
    "safety": {"save_memory"},
}


def _literal(node: ast.AST):
    """把 AST 字面量节点转成 Python 对象（只处理常量容器）"""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _parse_nodes(graph_file: Path) -> List[str]:
    """
    从 define_nodes() 的 return 字典里取键名（节点的唯一权威来源）。

    注意：define_structure() 只声明 entry / edges / conditional_edges，
    节点清单在 define_nodes() 里，两边必须能对上——对不上就说明有人只加了一条边
    或只加了一个函数。
    """
    tree = ast.parse(graph_file.read_text(encoding="utf-8"))
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef) and func.name == "define_nodes":
            for sub in ast.walk(func):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    return [_literal(k) for k in sub.value.keys if k is not None]
    raise ValueError(f"{graph_file} 中未找到 define_nodes() 的 return 字典")


def _parse_structure(graph_file: Path) -> Dict[str, object]:
    """
    从 graph.py 的 define_structure() 里解析出 nodes / entry / edges / conditional_edges。

    说明：conditional_edges 的值是 `(lambda s: ..., {分支: 目标})`，lambda 不是字面量，
    但**键**（条件边的源节点）一定是字符串常量，对结构校验来说足够了。
    """
    tree = ast.parse(graph_file.read_text(encoding="utf-8"))
    nodes = _parse_nodes(graph_file)

    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef) and func.name == "define_structure":
            for sub in ast.walk(func):
                if not (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict)):
                    continue
                result: Dict[str, object] = {
                    "nodes": nodes, "entry": "", "edges": [], "conditional_edges": [],
                }
                for key_node, value_node in zip(sub.value.keys, sub.value.values):
                    key = _literal(key_node) if key_node is not None else None
                    if key == "entry":
                        result["entry"] = _literal(value_node) or ""
                    elif key == "edges":
                        parsed = _literal(value_node) or []
                        result["edges"] = [tuple(item) for item in parsed]
                    elif key == "conditional_edges" and isinstance(value_node, ast.Dict):
                        result["conditional_edges"] = [
                            _literal(k) for k in value_node.keys if k is not None
                        ]
                return result

    raise ValueError(f"{graph_file} 中未找到 define_structure() 的 return 字典")


def _check(agent: str, structure: Dict[str, object]) -> List[str]:
    """返回问题列表（空列表 = 该 Agent 结构完整）"""
    nodes: List[str] = list(structure["nodes"])          # type: ignore[arg-type]
    entry: str = str(structure["entry"])
    edges: List[Tuple[str, str]] = list(structure["edges"])  # type: ignore[arg-type]
    conditional: List[str] = list(structure["conditional_edges"])  # type: ignore[arg-type]

    problems: List[str] = []
    node_set = set(nodes)
    terminal = TERMINAL_NODES.get(agent, set())

    if not nodes:
        return ["未声明任何节点"]
    if entry not in node_set:
        problems.append(f"entry「{entry}」不在 nodes 中")

    # 边的两端必须是已声明节点（拼写错误的边会被 LangGraph 静默忽略）
    for source, target in edges:
        if source not in node_set:
            problems.append(f"固定边源节点「{source}」未声明")
        if target not in node_set:
            problems.append(f"固定边目标「{target}」未声明")
    for source in conditional:
        if source not in node_set:
            problems.append(f"条件边源节点「{source}」未声明")

    # ② 非终态节点必须有出边
    outgoing: Dict[str, Set[str]] = {name: set() for name in nodes}
    for source, target in edges:
        outgoing.setdefault(source, set()).add(target)
    for source in conditional:
        outgoing.setdefault(source, set()).update(
            name for name in nodes if name != source)  # 条件边可去任意节点

    for name in nodes:
        if name not in terminal and not outgoing.get(name):
            problems.append(
                f"「{name}」既不是终态也没有出边——LangGraph 会在此静默结束，"
                f"只能靠 _reached_terminal 降级重跑")

    # ③ 可达性（从 entry 出发，沿固定边 + 条件边）
    reachable: Set[str] = set()
    if entry in node_set:
        queue = [entry]
        while queue:
            current = queue.pop()
            if current in reachable:
                continue
            reachable.add(current)
            if current in conditional:
                queue.extend(name for name in nodes if name not in reachable)
            else:
                queue.extend(target for target in outgoing.get(current, ())
                             if target not in reachable)
    unreachable = node_set - reachable
    if unreachable:
        problems.append(f"从 entry 不可达的节点：{sorted(unreachable)}")

    # ④ 每个可达的非终态节点都能到达某个终态（反向 BFS）
    reverse: Dict[str, Set[str]] = {name: set() for name in nodes}
    for source, target in edges:
        reverse.setdefault(target, set()).add(source)
    for source in conditional:
        for target in nodes:
            if target != source:
                reverse.setdefault(target, set()).add(source)

    can_finish: Set[str] = set(terminal)
    frontier = list(terminal)
    while frontier:
        current = frontier.pop()
        for previous in reverse.get(current, ()):
            if previous not in can_finish:
                can_finish.add(previous)
                frontier.append(previous)
    stuck = reachable - can_finish
    if stuck:
        problems.append(f"无法到达终态的节点：{sorted(stuck)}")

    return problems


def main(argv: List[str]) -> int:
    only = [a for a in argv[1:] if not a.startswith("-")]
    graph_files = sorted(AGENTS_DIR.glob("*/graph.py"))
    if only:
        graph_files = [p for p in graph_files if p.parent.name in only]

    if not graph_files:
        print("未找到任何 agents/*/graph.py")
        return 1

    failed = 0
    print("=" * 72)
    print("Agent 图结构静态校验（不依赖 langgraph，纯 AST 解析）")
    print("=" * 72)
    for graph_file in graph_files:
        agent = graph_file.parent.name
        structure = _parse_structure(graph_file)
        problems = _check(agent, structure)
        nodes = structure["nodes"]
        edges = structure["edges"]
        conditional = structure["conditional_edges"]
        status = "OK  " if not problems else "FAIL"
        print(f"\n[{status}] {agent:8s} 节点 {len(nodes):2d} | 固定边 {len(edges):2d} | "
              f"条件边 {len(conditional)} | entry={structure['entry']}")
        if problems:
            failed += 1
            for problem in problems:
                print(f"       ✗ {problem}")

    print("\n" + "=" * 72)
    print(f"结果：{len(graph_files) - failed}/{len(graph_files)} 个 Agent 结构完整")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
