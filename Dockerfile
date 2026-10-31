# ============================================================
#  FoodAgent AI 智能餐饮 Agent 系统 · Docker 镜像构建
#  构建：docker build -t foodagent:1.0.0 .
#  运行：见 docs/deploy/docker-compose.yml
# ============================================================
FROM python:3.11-slim

# 时区与基础工具
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先复制依赖清单，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 运行时目录
RUN mkdir -p logs data/memory_store

EXPOSE 8090

# 健康检查（/api/health 返回 200 即视为健康）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=4).status==200 else 1)"

# 启动服务
CMD ["python", "main.py", "--no-banner"]
