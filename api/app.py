"""
    FastAPI 应用装配：api/app.py

    职责（需求文档 4.1「交互层」）：
        · 创建 FastAPI 应用实例、注册路由、挂载静态前端
        · 配置 CORS（前端与后端分端口部署时使用）
        · 生命周期钩子：启动期做配置自检 + 依赖预热（长时间记忆仓库 / Checkpointer）
        · 异常统一处理：所有业务异常返回结构化 JSON，避免前端拿到 500 裸栈

    启动方式（三种任选）：
        · python app.py                 （本文件自带 uvicorn 启动）
        · python main.py                （项目根目录统一入口）
        · uvicorn api.app:app --port 8090

    访问：
        前端页面  http://127.0.0.1:8090/
        接口文档  http://127.0.0.1:8090/docs
        健康检查  http://127.0.0.1:8090/api/health
"""
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from api.routes import router
from base.config import config
from base.exceptions import FoodAgentError
from base.logger import logger


# ==================================================================
# 生命周期：启动自检 + 依赖预热
# ==================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理。

    启动阶段（startup）：
        1. 打印配置自检结果（密钥是否就位、PG/Milvus 是否启用）
        2. 预热长期记忆仓库（PG 失败自动降级 FileStore，不阻塞启动）
        3. 预热 Checkpointer（PostgresSaver / MemorySaver 三级降级）
        4. 校验 15 个工具均已注册

    关闭阶段（shutdown）：
        · 记录关闭日志（连接由各客户端按需创建，无需显式释放）
    """
    logger.info("=" * 72)
    logger.info("  FoodAgent AI 智能餐饮 Agent 系统  启动中 ...")
    logger.info("=" * 72)

    # ---------- ① 配置自检 ----------
    report = config.validate()
    logger.info(f"[启动] 监听地址    : http://{config.APP_HOST}:{config.APP_PORT}")
    logger.info(f"[启动] 前端页面    : http://127.0.0.1:{config.APP_PORT}/")
    logger.info(f"[启动] 接口文档    : http://127.0.0.1:{config.APP_PORT}/docs")
    llm_state = "已配置" if report["llm"]["api_key_present"] else "缺失(将降级为规则引擎)"
    logger.info(f"[启动] LLM         : {report['llm']['model']} @ {report['llm']['base_url']} 密钥{llm_state}")
    logger.info(f"[启动] PostgreSQL  : {'启用' if report['postgres']['enabled'] else '未启用(降级文件记忆)'}"
                f" {report['postgres']['dsn']}")
    logger.info(f"[启动] Milvus      : {report['milvus']['host']}")
    logger.info(f"[启动] 意图路由    : {len(report['intents'])} 类")
    logger.info(f"[启动] 过敏原库    : {report['allergen_count']} 条")

    if not report["llm"]["api_key_present"]:
        logger.warning("[启动] LLM API Key 未配置或格式非法，所有 LLM 工具将走规则引擎降级分支")

    # ---------- ② 预热长期记忆仓库 ----------
    try:
        from memory.store import get_store

        store = await get_store()
        logger.info(f"[启动] 长期记忆仓库: {type(store).__name__}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[启动] 长期记忆仓库初始化失败（不影响启动）: {exc}")

    # ---------- ③ 预热 Checkpointer ----------
    try:
        from memory.checkpointer import CheckpointerHolder

        cp = CheckpointerHolder.get()
        logger.info(f"[启动] 短期记忆检查点器: {type(cp).__name__ if cp else 'none'}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[启动] Checkpointer 初始化失败（不影响启动）: {exc}")

    # ---------- ④ 工具注册校验 ----------
    try:
        from tools.registry import tool_registry

        counts = tool_registry.count()
        total = sum(counts.values())
        logger.info(f"[启动] 工具注册    : 共 {total} 个 {counts}")
        if total != 15:
            logger.warning(f"[启动] 工具数量应为 15，当前为 {total}，请检查 tools/registry.py")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[启动] 工具注册校验失败: {exc}")

    # ---------- ⑤ 可选重型组件初始化（默认关闭，开启后生效） ----------
    # 三个组件均为「渐进增强」：未启用时全部为 no-op，不影响主流程。
    try:
        from tools.tracing import init_tracing

        tr = init_tracing()
        if tr.get("enabled"):
            logger.info(f"[启动] LangSmith   : 已启用 project={tr.get('project')}")
        else:
            logger.info(f"[启动] LangSmith   : 未启用（{tr.get('reason')}）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[启动] LangSmith 初始化失败（不影响启动）: {exc}")

    try:
        from tools.cache import cache

        cr = cache.init()
        if cr.get("redis"):
            logger.info(f"[启动] Redis 缓存  : 已启用 "
                        f"{config.REDIS_HOST}:{config.REDIS_PORT}/db{config.REDIS_DB}")
        else:
            logger.info(f"[启动] Redis 缓存  : 未启用（降级进程内缓存，{cr.get('reason')}）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[启动] 缓存初始化失败（不影响启动）: {exc}")

    try:
        from tools.metrics import init_metrics

        mr = init_metrics()
        if mr.get("enabled"):
            logger.info(f"[启动] Prometheus  : 已启用 {config.PROMETHEUS_METRICS_PATH}")
        else:
            logger.info(f"[启动] Prometheus  : 未启用（记录函数为 no-op，{mr.get('reason')}）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[启动] 指标初始化失败（不影响启动）: {exc}")

    logger.info("=" * 72)
    logger.info("  启动完成。4 个 Agent / 15 个工具 / 9 类意图 / 18 个接口 已就绪")
    logger.info("=" * 72)

    yield

    # ---------- 关闭阶段：释放 PG 连接池（幂等，reload 模式反复调用也安全） ----------
    try:
        from memory.checkpointer import close_checkpointer

        close_checkpointer()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[关闭] 释放 Checkpointer 连接池失败（忽略）: {exc}")

    try:
        from memory.store import close_store

        close_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[关闭] 释放长期记忆连接池失败（忽略）: {exc}")

    logger.info("[关闭] FoodAgent 服务已停止")


# ==================================================================
# 应用实例
# ==================================================================

app = FastAPI(
    title="FoodAgent AI 智能餐饮 Agent 系统",
    description=(
        "基于 LangGraph + Orchestrator 的智能餐饮 Agent 系统。\n\n"
        "- **4 个 Agent**：OrderAgent(10 节点) / KitchenAgent(7 节点) / ReviewAgent(7 节点) / SafetyAgent(9 节点)\n"
        "- **15 个工具**：LLM 工具 4 / 规则引擎 4 / MCP 工具 4 / 外部 API 封装 3\n"
        "- **9 类意图**：菜品推荐 / 智能点餐 / 过敏原查询 / 后厨调度 / 库存检查 / 评价分析 / 改进建议 / 食安法规问答 / 营养健康问答\n"
        "- **三层记忆**：Checkpointer 短期记忆 + PostgreSQL 长期记忆 + 餐饮行业特定记忆\n"
        "- **自包含**：食品安全法规与营养知识由内置知识库提供，无需外部服务"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # 演示环境放开；生产请收敛为门店域名白名单
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 业务路由 ----------
app.include_router(router)


# ==================================================================
# 全局异常处理：统一返回结构化 JSON
# ==================================================================

@app.exception_handler(FoodAgentError)
async def foodagent_error_handler(request: Request, exc: FoodAgentError):
    """业务异常：返回结构化错误体，前端可直接展示 detail"""
    logger.error(f"[异常] {request.method} {request.url.path} -> {exc}")
    return JSONResponse(
        status_code=200,     # 业务异常用 200 + ok=false，便于前端统一处理
        content={"ok": False, "error": exc.__class__.__name__, "detail": str(exc)},
    )


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    """兜底异常：记录完整栈，返回不泄露内部细节的提示"""
    logger.exception(f"[异常] 未捕获错误 {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "InternalError",
                 "detail": "服务内部错误，请稍后重试或联系管理员"},
    )


# ==================================================================
# 静态前端挂载
# ==================================================================

STATIC_DIR = config.STATIC_DIR
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")

if os.path.isdir(STATIC_DIR):
    # /static/** 直接映射静态资源（css/js）。
    # 注意：若 index.html 内联样式脚本，则无需额外文件。
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    logger.info(f"[启动] 静态资源目录: {STATIC_DIR}")
else:
    logger.warning(f"[启动] 静态资源目录不存在: {STATIC_DIR}，前端页面将不可访问")


@app.get("/", include_in_schema=False)
async def index():
    """返回前端首页（单页应用）"""
    if os.path.exists(INDEX_FILE):
        return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")
    return JSONResponse(
        status_code=200,
        content={
            "service": "FoodAgent AI 智能餐饮 Agent 系统",
            "message": "前端页面未找到，请访问 /docs 使用接口文档",
            "docs": "/docs",
            "health": "/api/health",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """空 favicon，避免浏览器 404 噪音"""
    return JSONResponse(status_code=204, content=None)


# ==================================================================
# Prometheus 指标端点（未启用时返回降级说明，不影响服务）
# ==================================================================

@app.get(config.PROMETHEUS_METRICS_PATH, include_in_schema=False)
async def metrics():
    """
    暴露 Prometheus 指标（默认路径 /metrics）。

    未启用时返回 200 + 说明文本，而不是 404 —— 这样 Prometheus 抓取端
    能明确区分「服务没跑」和「指标没开」，排查更省事。
    """
    from tools.metrics import is_enabled, render

    if not is_enabled():
        return Response(
            content=("# Prometheus 指标未启用\n"
                     "# 启用方式：pip install prometheus-client 并设置 config.ini 的 "
                     "[prometheus] enabled = true\n"),
            media_type="text/plain; charset=utf-8",
        )
    body, content_type = render()
    return Response(content=body, media_type=content_type)


# ==================================================================
# 直接启动入口
# ==================================================================

if __name__ == "__main__":
    import uvicorn

    logger.info(f"以开发模式启动 uvicorn @ {config.APP_HOST}:{config.APP_PORT}")
    uvicorn.run(
        "api.app:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        reload=False,
        log_level="info",
    )
