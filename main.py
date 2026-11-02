"""
    AgentFlow 统一启动入口：main.py

    职责：项目根目录唯一入口，负责：
        1. 校验运行环境（Python 版本、依赖完整性、配置文件）
        2. 启动 FastAPI 服务（uvicorn，端口 8090）

    启动方式：
        python main.py                 # 默认启动 Web 服务
        python main.py --port 9000     # 自定义端口
        python main.py --check         # 仅做环境自检，不启动服务
        python main.py --no-reload     # 关闭热重载（生产建议）

    其他入口：
        python app.py                  # 等价（薄封装，指向 main）
        python run_cli.py              # 终端交互模式（不启动 Web，便于快速调试）
        python -m api.app              # 直接启动 api 层
"""
import argparse
import os
import sys

# 确保项目根目录在 sys.path 中（支持从任意目录运行）
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ==================================================================
# 环境自检
# ==================================================================

REQUIRED_MODULES = [
    ("fastapi", "Web 框架"),
    ("uvicorn", "ASGI 服务器"),
    ("pydantic", "数据校验"),
    ("jwt", "JWT 认证（PyJWT）"),
    ("dotenv", "环境变量加载"),
]

OPTIONAL_MODULES = [
    ("langgraph", "LangGraph 状态图（缺失则降级顺序执行器）"),
    ("openai", "LLM 调用（缺失则全部走规则引擎）"),
    ("httpx", "HTTP 客户端（缺失则集成降级）"),
    ("psycopg", "PostgreSQL 驱动（缺失则长期记忆降级为文件）"),
]


def check_environment() -> bool:
    """检查 Python 版本、依赖、配置文件，返回是否可启动"""
    print("=" * 72)
    print("  FoodAgent 运行环境自检")
    print("=" * 72)

    ok = True

    # ① Python 版本
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        print(f"[OK]   Python 版本 {major}.{minor}（要求 >= 3.10）")
    else:
        print(f"[FAIL] Python 版本 {major}.{minor} 过低，要求 >= 3.10")
        ok = False

    # ② 必需依赖
    for mod, desc in REQUIRED_MODULES:
        try:
            __import__(mod)
            print(f"[OK]   依赖 {mod:<16} {desc}")
        except ImportError:
            print(f"[FAIL] 缺少必需依赖 {mod:<16} {desc}")
            print(f"         修复：pip install -r requirements.txt")
            ok = False

    # ③ 可选依赖（缺失不影响启动，仅功能降级）
    for mod, desc in OPTIONAL_MODULES:
        try:
            __import__(mod)
            print(f"[OK]   可选 {mod:<16} {desc}")
        except ImportError:
            print(f"[WARN] 缺少可选依赖 {mod:<14} → {desc}")

    # ④ 配置文件
    for fname in ("config.ini", ".env"):
        path = os.path.join(ROOT, fname)
        if os.path.exists(path):
            print(f"[OK]   配置文件 {fname}")
        else:
            print(f"[WARN] 配置文件 {fname} 不存在（将使用代码内默认值）")

    # ⑤ 配置自检
    try:
        from base.config import config

        report = config.validate()
        key_ok = report["llm"]["api_key_present"]
        print(f"[{'OK' if key_ok else 'WARN'}]   LLM API Key "
              f"{'已配置' if key_ok else '未配置或格式非法（将降级为规则引擎）'}")
        print(f"[OK]   意图路由 {len(report['intents'])} 类 · "
              f"过敏原库 {report['allergen_count']} 条")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 配置加载失败：{exc}")
        ok = False

    # ⑥ 能力包自检（契约 + 声明与代码的一致性，零模型调用）
    try:
        from agentflow.runtime import get_runtime

        runtime = get_runtime()
        active = runtime.active
        if active:
            print(f"[OK]   能力包 {active.pack_id} v{active.version}："
                  f"{len(active.agents)} Agent / {active.node_total} 节点 / "
                  f"{len(active.tools)} 工具 / {len(active.intents)} 意图 / "
                  f"{len(active.guards)} 护栏")
        else:
            print("[WARN] 未发现能力包（verticals/*/pack.json），平台以裸运行时模式运行")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 能力包装配失败：{exc}")
        ok = False

    print("=" * 72)
    print("  自检结果：" + ("通过，可以启动服务" if ok else "未通过，请先修复上述 FAIL 项"))
    print("=" * 72)
    return ok


# ==================================================================
# 主入口
# ==================================================================

def main():
    parser = argparse.ArgumentParser(description="AgentFlow · 企业级多 Agent 编排平台")
    parser.add_argument("--host", default=None, help="监听地址（默认取配置）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取配置 8090）")
    parser.add_argument("--check", action="store_true", help="仅做环境自检，不启动服务")
    parser.add_argument("--reload", action="store_true", help="开启热重载（开发用）")
    parser.add_argument("--no-banner", action="store_true", help="跳过自检直接启动")
    parser.add_argument("--allow-full", action="store_true",
                        help="允许全量运行（需同时设置 ALLOW_FULL_RUN=1；默认仅最小验证）")
    args = parser.parse_args()

    # 运行策略：远程模型最小验证（硬限制见 base/run_policy.py）
    from base.run_policy import RunPolicyError, run_policy
    run_policy.print_banner()
    # 传了 --allow-full 但环境变量没放开 → 直接拒绝启动，避免误跑全量烧额度
    try:
        run_policy.require_full_permission(args.allow_full, stage="FoodAgent 全量启动")
    except RunPolicyError as exc:
        print(f"\n[启动中止] {exc}\n")
        return 2
    print()

    passed = True
    if not args.no_banner:
        passed = check_environment()
        if not passed and not args.check:
            print("\n提示：仍有 FAIL 项，服务可能无法正常工作。可使用 --no-banner 跳过自检强制启动。\n")

    if args.check:
        return 0 if passed else 1

    from base.config import config

    host = args.host or config.APP_HOST
    port = args.port or config.APP_PORT

    print(f"\n启动服务：http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print(f"前端页面：http://127.0.0.1:{port}/")
    print(f"接口文档：http://127.0.0.1:{port}/docs")
    print("按 Ctrl+C 停止\n")

    import uvicorn

    if args.reload:
        # 热重载模式必须传 import string，不能传 app 实例
        uvicorn.run("api.app:app", host=host, port=port, reload=True, log_level="info")
    else:
        from api.app import app

        uvicorn.run(app, host=host, port=port, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
