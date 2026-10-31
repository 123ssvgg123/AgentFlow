"""FoodAgent 18 接口 HTTP 冒烟测试"""
import json
import os
import sys
import urllib.error
import urllib.request

# 本地回环请求不应走系统代理（避免代理拦截 127.0.0.1 导致 502）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
# 构建彻底绕过代理的 opener
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)

BASE = "http://127.0.0.1:18090"
TOKEN = None
PASS, FAIL = "[PASS]", "[FAIL]"
results = {"pass": 0, "fail": 0}


def call(method, path, body=None, auth=True, expect=200):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if auth and TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            code = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code = e.code
        raw = e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, 0, str(e)
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw[:200]}
    return payload, code, None


def check(label, method, path, body=None, auth=True, validate=None, expect=200):
    payload, code, err = call(method, path, body, auth)
    if err:
        print(f"{FAIL} {label:<32} 连接异常: {err}")
        results["fail"] += 1
        return None
    ok = code == expect
    extra = ""
    if ok and validate:
        try:
            ok, extra = validate(payload)
        except Exception as e:  # noqa: BLE001
            ok, extra = False, f"校验异常 {e}"
    if ok:
        print(f"{PASS} {label:<32} HTTP {code}  {extra}")
        results["pass"] += 1
    else:
        print(f"{FAIL} {label:<32} HTTP {code}  {extra}  {str(payload)[:150]}")
        results["fail"] += 1
    return payload


print("=" * 78)
print("  FoodAgent 18 接口 HTTP 冒烟测试")
print("=" * 78)

# ① 登录
r = check("① 登录", "POST", "/api/auth/login",
          {"username": "waiter_01", "password": "123456"}, auth=False,
          validate=lambda p: (bool(p.get("access_token")), f"user={p.get('user', {}).get('role')}"))
if r:
    TOKEN = r["access_token"]
else:
    print("登录失败，终止测试")
    sys.exit(1)

# ② 当前用户
check("② 当前用户", "GET", "/api/auth/me",
      validate=lambda p: (p.get("user", {}).get("user_id") == "waiter_01", f"tenant={p.get('user', {}).get('tenant_id')}"))

# ③ 创建任务
r = check("③ 创建任务", "POST", "/api/tasks",
          validate=lambda p: (bool(p.get("data", {}).get("thread_id")), p.get("data", {}).get("thread_id", "")[:16]))
THREAD = r["data"]["thread_id"] if r else None

# ④ 对话（非流式）
check("④ 对话（非流式）", "POST", "/api/chat",
      {"query": "帮我推荐今天的招牌菜", "thread_id": THREAD},
      validate=lambda p: (bool(p.get("answer")), f"intent={p.get('intents')} agent={p.get('agents_invoked')} {p.get('processing_time')}s"))

# ⑦ 创建订单
check("⑦ 创建订单", "POST", "/api/orders",
      {"dishes": [{"dish_name": "番茄炒蛋", "quantity": 2, "price": 26}],
       "guest_count": 2, "table_no": "A08", "allergens": [], "member_level": "普通"},
      validate=lambda p: (bool(p.get("data", {}).get("order_id")), f"订单 {p.get('data', {}).get('order_id')}"))

# ⑧ 查询订单详情
check("⑧ 查询订单详情", "GET", "/api/orders/ORD-TEST-001",
      validate=lambda p: ("order" in p, ""))

# ⑨ 订单列表
check("⑨ 订单列表", "GET", "/api/orders?limit=5",
      validate=lambda p: (len(p.get("orders", [])) > 0, f"{p.get('total')} 单"))

# ⑩ 后厨待处理
check("⑩ 后厨待处理订单", "GET", "/api/kitchen/pending",
      validate=lambda p: (len(p.get("orders", [])) > 0, f"{p.get('total')} 单"))

# ⑪ 后厨排产
check("⑪ 生成排产方案", "POST", "/api/kitchen/schedule",
      validate=lambda p: (bool(p.get("answer")), f"{len(p.get('answer', ''))} 字"))

# ⑫ 评价分析
check("⑫ 评价分析", "POST", "/api/reviews/analyze",
      validate=lambda p: (bool(p.get("answer")), f"{len(p.get('answer', ''))} 字"))

# ⑬ 评价历史
check("⑬ 评价分析历史", "GET", "/api/reviews/history?limit=10",
      validate=lambda p: ("records" in p, f"{p.get('total')} 条"))

# ⑭ 查询门店偏好
check("⑭ 查询门店偏好", "GET", "/api/memory/preferences",
      validate=lambda p: ("preferences" in p, f"tenant={p.get('tenant_id')}"))

# ⑮ 更新门店偏好
check("⑮ 更新门店偏好", "PUT", "/api/memory/preferences",
      {"popular_dishes": ["招牌烤鸭"], "peak_hours": ["11:30-13:00"], "avg_order_value": 68.5},
      validate=lambda p: (p.get("status") == "success", p.get("message", "")))

# ⑯ 查询顾客画像
check("⑯ 查询顾客画像", "GET", "/api/memory/customer/cust_888",
      validate=lambda p: ("customer" in p, f"found={p.get('found')}"))

# ⑰ 更新顾客画像
check("⑰ 更新顾客画像", "PUT", "/api/memory/customer/cust_888",
      {"customer_id": "cust_888", "dietary_restrictions": ["低卡"],
       "allergens": ["花生"], "favorite_dishes": ["清蒸鲈鱼"], "member_level": "金卡"},
      validate=lambda p: (p.get("status") == "success", p.get("message", "")))

# ⑱ Agent 清单
# 口径说明：实现为 OrderAgent 10 + KitchenAgent 7 + ReviewAgent 7 + SafetyAgent 9 = 33 节点
#           （需求文档 3.3.1 写的 8 节点为旧口径，项目已统一为 10 节点）
check("⑱ Agent 清单", "GET", "/api/agents",
      validate=lambda p: (p.get("total_agents") == 4 and p.get("total_nodes") == 33,
                          f"{p.get('total_agents')} Agent / {p.get('total_nodes')} 节点"))

# ⑥ 清除任务历史
check("⑥ 清除任务历史", "DELETE", f"/api/chat/history/{THREAD}",
      validate=lambda p: (p.get("status") == "success", ""))

# ---------- 辅助接口 ----------
print("-" * 78)
check("辅助 工具清单", "GET", "/api/tools",
      validate=lambda p: (p.get("total") == 15, f"{p.get('total')} 工具 {p.get('counts')}"))
check("辅助 意图清单", "GET", "/api/intents",
      validate=lambda p: (p.get("total") == 9, f"{p.get('total')} 意图"))
check("辅助 前端配置", "GET", "/api/config",
      validate=lambda p: (len(p.get("quick_questions", [])) >= 3, f"{len(p.get('quick_questions', []))} 快捷问题"))
check("辅助 健康检查", "GET", "/api/health",
      validate=lambda p: (p.get("status") == "healthy", f"checkpointer={p.get('dependencies', {}).get('checkpointer')}"))


def _validate_health_llm(payload):
    """校验 /api/health 的 LLM 段：主/备模型切换字段齐备，且密钥必须脱敏"""
    llm = (payload.get("dependencies") or {}).get("llm") or {}
    required = ["primary_model", "fallback_enabled", "fallback_model",
                "active_model", "using_fallback", "api_key"]
    missing = [k for k in required if k not in llm]
    if missing:
        return False, f"缺少字段 {missing}"
    raw_key = os.getenv("LLM_API_KEY", "")
    if raw_key and raw_key in str(llm.get("api_key", "")):
        return False, "api_key 未脱敏，存在明文泄露风险"
    return True, (f"active={llm.get('active_model')} "
                  f"fallback_enabled={llm.get('fallback_enabled')} "
                  f"using_fallback={llm.get('using_fallback')} "
                  f"api_key={llm.get('api_key')}")


check("辅助 健康检查 LLM 主备字段", "GET", "/api/health",
      validate=_validate_health_llm)
check("辅助 接口清单", "GET", "/api/endpoints",
      validate=lambda p: (p.get("total") == 18, f"{p.get('total')} 接口"))
check("前端 首页", "GET", "/", auth=False,
      validate=lambda p: ("_raw" in p or True, "index.html"))

# 认证失败场景
print("-" * 78)
check("鉴权 无 Token 应 401", "GET", "/api/auth/me", auth=False, expect=401,
      validate=lambda p: (True, p.get("detail", "")))
check("鉴权 错误密码应 401", "POST", "/api/auth/login",
      {"username": "waiter_01", "password": "wrong"}, auth=False, expect=401,
      validate=lambda p: (True, p.get("detail", "")))

print("=" * 78)
print(f"  汇总：通过 {results['pass']} · 失败 {results['fail']}")
print("=" * 78)
sys.exit(0 if results["fail"] == 0 else 1)
