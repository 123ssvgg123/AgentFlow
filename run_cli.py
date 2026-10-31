"""
    FoodAgent 终端交互模式：run_cli.py

    用途：不启动 Web 服务，直接在终端与 Orchestrator 对话。
        适合快速调试 Agent 逻辑、验证意图路由与工具调用。

    用法：
        python run_cli.py
        python run_cli.py --tenant store_001 --user waiter_01 --role waiter

    交互命令：
        /quit 或 /exit   退出
        /new             开启新任务（重置 thread_id 与记忆上下文）
        /info            查看当前会话信息与 Agent 清单
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents.orchestrator import orchestrator  # noqa: E402
from base.config import config  # noqa: E402
from memory.checkpointer import build_thread_id  # noqa: E402


async def run_session(tenant_id: str, user_id: str, role: str):
    """命令行会话主循环"""
    thread_id = build_thread_id(tenant_id, user_id)
    history = []

    print("=" * 72)
    print("  FoodAgent 终端交互模式")
    print(f"  门店={tenant_id}  员工={user_id}  角色={role}")
    print(f"  任务={thread_id}")
    print("  输入 /quit 退出，/new 新建任务，/info 查看信息")
    print("=" * 72)

    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue
        if user_input in ("/quit", "/exit"):
            print("再见。")
            break
        if user_input == "/new":
            thread_id = build_thread_id(tenant_id, user_id)
            history = []
            print(f"[已新建任务] {thread_id}")
            continue
        if user_input == "/info":
            print(f"  任务 ID   : {thread_id}")
            print(f"  意图类型  : {list(config.INTENT_ROUTES.keys())}")
            print(f"  Agent     : OrderAgent(8) / KitchenAgent(7) / ReviewAgent(7)")
            print(f"  历史轮数  : {len(history)}")
            continue

        print("\nFoodAgent > ", end="", flush=True)
        try:
            result = await orchestrator.run(
                user_input=user_input, tenant_id=tenant_id, user_id=user_id,
                role=role, thread_id=thread_id, history=history or None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[执行失败] {exc}")
            continue

        print(result.get("answer", "（无回答）"))
        print("-" * 72)
        print(f"意图: {result.get('intents')}  "
              f"Pipeline: {result.get('is_pipeline')}  "
              f"Agent: {result.get('agents_invoked')}  "
              f"耗时: {result.get('degraded_tools') and '降级工具 ' + str(result['degraded_tools'])}")
        if result.get("pending_actions"):
            for pa in result["pending_actions"]:
                print(f"[需人工确认] {pa.get('message')}")

        history.append({"question": user_input, "answer": result.get("answer", "")})
        history = history[-config.HISTORY_ROUNDS:]


def main():
    parser = argparse.ArgumentParser(description="FoodAgent 终端交互模式")
    parser.add_argument("--tenant", default="store_001", help="门店 ID")
    parser.add_argument("--user", default="waiter_01", help="员工 ID")
    parser.add_argument("--role", default="waiter", choices=["waiter", "kitchen", "manager"])
    args = parser.parse_args()

    try:
        asyncio.run(run_session(args.tenant, args.user, args.role))
    except KeyboardInterrupt:
        print("\n已中断。")


if __name__ == "__main__":
    main()
