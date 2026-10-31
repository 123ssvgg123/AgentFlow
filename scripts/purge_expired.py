"""
    记忆清理入口脚本：scripts/purge_expired.py

    用途：按需求 3.6.5「记忆清理策略」清理/归档过期长期记忆，供**外部定时任务**调用
         （Windows 计划任务 / cron / K8s CronJob，挂载示例见 docs/OBSERVABILITY.md 第九节）。

        保留口径（默认值来自 base/config.py，可用命令行参数临时覆盖）：
            对话记忆 conversation      90 天归档
            门店偏好 store_preferences  永久（0 = 不清理）
            顾客画像 customer_profiles  永久（0 = 不清理）
            评价分析 review_analysis    730 天（2 年）
            订单状态 order_status_log   90 天（与对话同归档口径）
            合规留痕 compliance_log     3 年（COMPLIANCE_LOG_YEARS）

    与服务的实现关系（复用，不重复实现）：
        · 真正干活的是 memory/store.py 的 `BaseStore.purge_expired()`
        · 本脚本只负责「解析参数 → 取仓库单例 → 调用 → 打印 → 退出码」

    实现选择：
        config.PG_ENABLED=true  → PostgresStore：DELETE ... WHERE 时间列 < now() - interval
        config.PG_ENABLED=false → FileStore：遍历 data/memory_store/*.json 过滤后写回

    用法：
        python scripts/purge_expired.py                     # 全部门店/全表按策略清理
        python scripts/purge_expired.py --tenant store_001  # 只清理指定门店
        python scripts/purge_expired.py --dry-run           # 只统计不删除
        python scripts/purge_expired.py --review-days 30    # 临时覆盖某数据域保留期

    退出码：0 = 全部成功；1 = 初始化失败或有数据域清理失败。
"""
# 导入参数解析与时间工具
import argparse
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 数据域展示名（顺序即打印顺序，与需求 3.6.5 的表述顺序对齐）
DOMAIN_LABELS = [
    ("conversation", "对话记忆（Checkpointer 归档口径）"),
    ("review_analysis", "评价分析记录"),
    ("order_status_log", "订单状态流转日志"),
    ("compliance_log", "合规留痕"),
    ("store_preferences", "门店偏好（永久保留）"),
    ("customer_profiles", "顾客画像（永久保留）"),
]


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="FoodAgent 记忆清理/归档任务（需求 3.6.5 记忆清理策略）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tenant", default="",
                        help="只清理指定门店 ID；不传 = 处理全部"
                             "（FileStore 遍历 data/memory_store/*.json，PostgresStore 全表按条件删）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计将被清理的条数，不真正删除")
    parser.add_argument("--conversation-days", type=int, default=None,
                        help="临时覆盖：对话记忆归档天数（0 = 永久）")
    parser.add_argument("--review-days", type=int, default=None,
                        help="临时覆盖：评价分析保留天数（0 = 永久）")
    parser.add_argument("--order-log-days", type=int, default=None,
                        help="临时覆盖：订单状态流转日志保留天数（0 = 永久）")
    parser.add_argument("--compliance-years", type=int, default=None,
                        help="临时覆盖：合规留痕保留年限（0 = 永久）")
    parser.add_argument("--tmp-dir", default="",
                        help="仅用于验证/演练：临时指定记忆目录（等价于设置环境变量 "
                             "FOODAGENT_MEMORY_STORE_DIR），不影响生产 data/memory_store")
    return parser.parse_args()


def build_retention(args: argparse.Namespace) -> dict:
    """把命令行覆盖项组装成 purge_expired(retention=...) 所需的字典"""
    from memory.store import default_retention

    retention = default_retention()
    overrides = {
        "conversation": args.conversation_days,
        "review_analysis": args.review_days,
        "order_status_log": args.order_log_days,
        "compliance_log": args.compliance_years,
    }
    for domain, value in overrides.items():
        if value is not None:
            retention[domain] = value
    return retention


async def run() -> int:
    """主流程：初始化仓库 → 调用 purge_expired → 打印汇总"""
    args = parse_args()
    # --tmp-dir 必须在 import memory.store（即 FileStore 构造）之前生效
    if args.tmp_dir:
        os.environ["FOODAGENT_MEMORY_STORE_DIR"] = args.tmp_dir

    from base.config import config
    from base.logger import logger
    from memory.store import get_store

    retention = build_retention(args)
    started = time.time()

    print("=" * 72)
    print("  FoodAgent 记忆清理/归档任务（需求 3.6.5）")
    print("=" * 72)
    print(f"  模式        ：{'DRY-RUN（只统计不删除）' if args.dry_run else '实跑（会删除过期数据）'}")
    print(f"  门店        ：{args.tenant or '全部门店'}")
    print(f"  保留策略    ：对话 {retention['conversation']} 天 / "
          f"评价 {retention['review_analysis']} 天 / "
          f"订单日志 {retention['order_status_log']} 天 / "
          f"合规留痕 {retention['compliance_log']} 年 / "
          f"门店偏好与顾客画像 {'永久' if not retention['store_preferences'] else retention['store_preferences']}")
    print("-" * 72)

    try:
        store = await get_store()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 长期记忆仓库初始化失败：{exc}")
        return 1

    print(f"  存储实现    ：{type(store).__name__}"
          f"{'（PostgreSQL，SQL 全表删除）' if type(store).__name__ == 'PostgresStore' else '（JSON 文件，逐门店过滤）'}")
    if args.tmp_dir and type(store).__name__ != "FileStore":
        print("  [WARN] 已指定 --tmp-dir，但当前启用的是 PostgreSQL 实现，该参数不生效")

    try:
        purged = await store.purge_expired(
            retention=retention, tenant_id=args.tenant, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[记忆清理] 执行失败")
        print(f"[FAIL] 清理执行失败：{type(exc).__name__}: {exc}")
        return 1

    # ---- 打印每个数据域的条数 ----
    print("-" * 72)
    total = 0
    for domain, label in DOMAIN_LABELS:
        count = int(purged.get(domain, 0) or 0)
        total += count
        if domain in ("store_preferences", "customer_profiles"):
            print(f"  {label:<32} 跳过（永久保留，配置 0）")
        elif domain == "conversation":
            print(f"  {label:<32} {count} 条"
                  f"（Checkpointer 表由 LangGraph 维护，本项目不做物理删除）")
        else:
            print(f"  {label:<32} {count} 条")
    # 兜底打印未知数据域（未来扩展时不会静默丢失）
    for domain, count in purged.items():
        if domain not in dict(DOMAIN_LABELS):
            total += int(count or 0)
            print(f"  {domain:<32} {count} 条")

    elapsed = time.time() - started
    print("-" * 72)
    print(f"  合计 {'将' if args.dry_run else '已'}清理 {total} 条记录/事件，"
          f"总耗时 {elapsed:.2f}s")
    print("=" * 72)
    if args.dry_run:
        print("  提示：以上为预演结果，未删除任何数据；去掉 --dry-run 即实跑。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
