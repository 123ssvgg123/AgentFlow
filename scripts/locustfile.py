"""
    Locust 压测脚本：scripts/locustfile.py

    职责：对 FoodAgent 的 HTTP 接口做并发压测，验证「高峰期多桌并发下单」的稳定性。

    依赖：pip install locust
    用法：
        # Web UI 模式（推荐，可视化实时曲线）
        locust -f scripts/locustfile.py --host http://127.0.0.1:8090

        # 无头模式（CI/报告用）
        locust -f scripts/locustfile.py --host http://127.0.0.1:8090 \
               --headless -u 50 -r 5 -t 120s \
               --html logs/loadtest_report.html --csv logs/loadtest

    说明：config.ini 的 [loadtest] 段提供默认参数（users/spawn_rate/run_time）。
         压测会真实写入 FileStore/PG，建议使用独立的测试门店（默认 store_loadtest）。
"""
import os
import random
import sys

from locust import HttpUser, between, events, task

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from base.config import config
    _CFG_OK = True
except Exception:  # noqa: BLE001
    _CFG_OK = False
    config = None


# ==================================================================
# 压测语料：覆盖 9 类意图（含食安/营养问答），保证路由分支都能被压到
# ==================================================================
QUERIES = {
    "dish_recommend": [
        "帮我推荐适合10人聚餐的套餐，有两位素食",
        "有什么招牌菜推荐吗",
        "三个人吃饭，预算200，帮我搭配一下",
        "推荐几道清爽解腻的菜",
    ],
    "smart_order": [
        "来一份宫保鸡丁和一份番茄蛋汤",
        "帮我点两荤一素，配米饭",
        "点一份清蒸鲈鱼，不要辣",
    ],
    "allergen_query": [
        "宫保鸡丁含不含花生",
        "我对麸质过敏，能吃什么",
        "这道菜有没有坚果",
    ],
    "kitchen_dispatch": [
        "现在后厨有多少单在排队",
        "帮我安排出餐顺序",
        "B12 那桌催了，能不能提前",
    ],
    "inventory_check": [
        "库存还剩多少牛肉",
        "鲈鱼够不够今天的量",
        "哪些食材快用完了",
    ],
    "review_analysis": [
        "帮我分析一下本周的顾客评价",
        "最近差评都说了什么",
        "美团上的评价怎么样",
    ],
    "improvement_advice": [
        "出餐慢怎么改进",
        "怎么提升顾客满意度",
        "给我一份改进建议",
    ],
    "food_safety_query": [
        "凉菜间消毒有什么标准",
        "食品留样要留多久",
        "冷藏温度要求是多少",
    ],
    "nutrition_query": [
        "这道菜多少卡路里",
        "有没有低脂高蛋白的推荐",
        "宫保鸡丁的营养成分是多少",
    ],
    "pipeline": [
        "今天是我们店开业三周年，帮我从菜品推荐到评价分析全流程处理",
    ],
}


def _all_queries():
    out = []
    for qs in QUERIES.values():
        out.extend(qs)
    return out


# ==================================================================
# 用户行为
# ==================================================================

class FoodAgentUser(HttpUser):
    """模拟一个门店员工（服务员/后厨主管）的日常操作"""

    # 每两次请求间隔 0.5~2 秒，模拟真实操作节奏
    wait_time = between(0.5, 2.0)

    def on_start(self):
        """登录拿 Token（压测前置动作）"""
        self.token = ""
        self.tenant_id = getattr(config, "LOADTEST_TENANT_ID", "store_loadtest") if _CFG_OK else "store_loadtest"
        user_id = getattr(config, "LOADTEST_USER_ID", "loadtest_bot") if _CFG_OK else "loadtest_bot"
        self.user_id = user_id

        resp = self.client.post(
            "/api/auth/login",
            json={"username": "waiter_01", "password": "123456"},
            name="/api/auth/login",
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                self.token = data.get("access_token", "")
                self.tenant_id = (data.get("user") or {}).get("tenant_id") or self.tenant_id
            except Exception:  # noqa: BLE001
                pass
        if not self.token:
            # 登录失败仍然继续：部分接口无需 Token，压测目标以对话链路为主
            events.request.fire(
                request_type="SETUP", name="login_failed", response_time=0,
                response_length=0, exception=Exception("登录失败，Token 为空"))

    # ---------------- 任务权重（按业务发生频率设定） ----------------

    @task(40)
    def chat_single(self):
        """单意图对话（最高频：占比约 40%）"""
        self._chat(random.choice(_all_queries()[:-1]))

    @task(8)
    def chat_pipeline(self):
        """全流程 Pipeline（低频但最重：占比约 8%）"""
        self._chat(random.choice(QUERIES["pipeline"]))

    @task(15)
    def chat_recommend(self):
        """菜品推荐（占比约 15%）"""
        self._chat(random.choice(QUERIES["dish_recommend"]))

    @task(10)
    def chat_kitchen(self):
        """后厨调度（占比约 10%）"""
        self._chat(random.choice(QUERIES["kitchen_dispatch"]))

    @task(8)
    def kitchen_schedule_api(self):
        """直连后厨排产接口（占比约 8%）"""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with self.client.post(
            "/api/kitchen/schedule",
            json={"tenant_id": self.tenant_id, "human_confirmed": False},
            headers=headers, name="/api/kitchen/schedule", catch_response=True,
        ) as resp:
            if resp.status_code in (200, 202):
                resp.success()
            elif resp.status_code == 401:
                resp.failure("未授权（登录失败）")
            else:
                resp.failure(f"HTTP {resp.status_code}")

    @task(6)
    def health_check(self):
        """健康检查（占比约 6%）；注意路由挂在 /api 前缀下"""
        self.client.get("/api/health", name="/api/health")

    @task(4)
    def tools_list(self):
        """工具清单（占比约 4%）"""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client.get("/api/tools", headers=headers, name="/api/tools")

    @task(3)
    def memory_query(self):
        """记忆查询（占比约 3%）；门店偏好是 /api/memory/preferences，没有 /api/memory/store 路由"""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client.get(
            "/api/memory/preferences", headers=headers,
            name="/api/memory/preferences")

    # ---------------- 内部方法 ----------------

    def _chat(self, query: str):
        """发一次对话请求（带 Token 与门店隔离）"""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {
            "query": query,
            "tenant_id": self.tenant_id,
            "customer_id": f"cust_{random.randint(1, 200)}",
        }
        with self.client.post(
            "/api/chat", json=payload, headers=headers,
            name="/api/chat", catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    resp.failure("响应不是合法 JSON")
                    return
                # 业务级断言：回答不能为空
                if not data.get("answer"):
                    resp.failure("answer 为空")
                else:
                    resp.success()
            elif resp.status_code == 401:
                resp.failure("未授权（登录失败）")
            else:
                resp.failure(f"HTTP {resp.status_code}")


class KitchenUser(HttpUser):
    """模拟后厨主管：只关心排产与出餐（更轻的负载画像）"""

    wait_time = between(1.0, 3.0)

    def on_start(self):
        self.token = ""
        self.tenant_id = getattr(config, "LOADTEST_TENANT_ID", "store_loadtest") if _CFG_OK else "store_loadtest"
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "kitchen_01", "password": "123456"},
            name="/api/auth/login",
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                self.token = data.get("access_token", "")
                self.tenant_id = (data.get("user") or {}).get("tenant_id") or self.tenant_id
            except Exception:  # noqa: BLE001
                pass

    @task(5)
    def schedule(self):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client.post(
            "/api/kitchen/schedule",
            json={"tenant_id": self.tenant_id, "human_confirmed": True},
            headers=headers, name="/api/kitchen/schedule")

    @task(3)
    def inventory(self):
        """后厨待处理列表（库存接口未单独暴露，用待处理单量代表后厨负载）"""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client.get("/api/kitchen/pending", headers=headers,
                        name="/api/kitchen/pending")

    @task(2)
    def ask(self):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.client.post(
            "/api/chat",
            json={"query": random.choice(QUERIES["kitchen_dispatch"]),
                  "tenant_id": self.tenant_id},
            headers=headers, name="/api/chat")


# ==================================================================
# 事件钩子：压测结束打印摘要
# ==================================================================

@events.test_start.add_listener
def _on_start(environment, **kwargs):
    print("=" * 64)
    print("FoodAgent 压测开始")
    if _CFG_OK:
        print(f"  默认并发：{config.LOADTEST_USERS}  启动速率：{config.LOADTEST_SPAWN_RATE}/s"
              f"  时长：{config.LOADTEST_RUN_TIME}s")
        print(f"  压测门店：{config.LOADTEST_TENANT_ID}（请勿使用生产门店）")
    print("=" * 64)


@events.test_stop.add_listener
def _on_stop(environment, **kwargs):
    stats = environment.stats
    print("=" * 64)
    print("FoodAgent 压测结束")
    print(f"  总请求数：{stats.total.num_requests}")
    print(f"  失败数：  {stats.total.num_failures}")
    print(f"  平均耗时：{stats.total.avg_response_time:.1f} ms")
    print(f"  P95 耗时：{stats.total.get_response_time_percentile(0.95):.1f} ms")
    print(f"  RPS：     {stats.total.total_rps:.2f}")
    print("=" * 64)


if __name__ == '__main__':
    print("=" * 64)
    print("Locust 压测脚本（需通过 locust 命令运行）")
    print("=" * 64)
    print("Web UI 模式：")
    print("  locust -f scripts/locustfile.py --host http://127.0.0.1:8090")
    print()
    print("无头模式：")
    print("  locust -f scripts/locustfile.py --host http://127.0.0.1:8090 \\")
    print("         --headless -u 50 -r 5 -t 120s \\")
    print("         --html logs/loadtest_report.html --csv logs/loadtest")
    print()
    print(f"语料条数：{len(_all_queries())} 条，覆盖 {len(QUERIES)} 类场景")
    print(f"用户类：FoodAgentUser（服务/后厨综合）、KitchenUser（后厨主管）")
