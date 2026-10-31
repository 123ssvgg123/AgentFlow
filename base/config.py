"""
    配置管理模块：base/config.py

    统一管理全项目配置，读取优先级：
        环境变量(.env / shell) > config.ini > 代码内 fallback 默认值

    设计对齐 FoodSafetySystem 的 base/config.py：
        · 同样走「.env + config.ini 双轨」+ 集中式属性访问
        · 业务代码一律 `from base.config import config` 引用，杜绝散落硬编码
        · 额外新增 FoodAgent 专有配置：PostgreSQL / JWT / 排队阈值

    密钥管理说明（对应需求「做好密钥管理与异常兜底」）：
        · API Key 只从 .env / 环境变量读取，代码内 fallback 为空串而非明文
        · .env 已被 .gitignore 排除，仓库只提交 .env.example 模板
        · 运行期缺失密钥时的兜底见 tools/llm_client.py（降级为规则引擎 + 明确报错）
"""
# 导入配置解析库
import configparser
# 导入路径操作库
import os

# 项目根目录（base/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 尝试加载 .env（未安装 python-dotenv 时静默跳过，不影响 config.ini 读取）
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, '.env'))
except ImportError:
    pass


class Config:
    """全项目配置的唯一来源"""

    def __init__(self, config_file=None):
        # 配置解析器，启用扩展插值（支持 ${section:key} 引用）
        self.config = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
        # 各类目录常量
        self.PROJECT_ROOT = PROJECT_ROOT
        self.LOG_DIR = os.path.join(self.PROJECT_ROOT, 'logs')
        # 日志文件路径
        self.LOG_FILE = os.path.join(self.LOG_DIR, 'app.log')
        self.DATA_DIR = os.path.join(self.PROJECT_ROOT, 'data')
        self.STATIC_DIR = os.path.join(self.PROJECT_ROOT, 'static')
        self.PROMPTS_DIR = os.path.join(self.PROJECT_ROOT, 'prompts')

        if config_file is None:
            config_file = os.path.join(self.PROJECT_ROOT, 'config.ini')
        self.config.read(config_file, encoding='utf-8')

        # ==================== 服务器配置 ====================
        # 监听地址（0.0.0.0 = 监听所有网卡，浏览器仍用 127.0.0.1 访问）
        self.APP_HOST = os.getenv('APP_HOST', self.config.get('server', 'host', fallback='0.0.0.0'))
        # 监听端口（与 FoodSafetySystem 错开：该系统用 8080，本系统用 8090）
        self.APP_PORT = int(os.getenv('APP_PORT', self.config.getint('server', 'port', fallback=8090)))

        # ==================== LLM 配置（DeepSeek） ====================
        # 模型名：DeepSeek 平台对话模型
        self.LLM_MODEL = os.getenv('LLM_MODEL', self.config.get('llm', 'model', fallback='deepseek-chat'))
        # API 密钥：只从环境变量 / .env 读取，代码内不给明文兜底
        self.LLM_API_KEY = os.getenv('LLM_API_KEY', self.config.get('llm', 'api_key', fallback=''))
        # 密钥名称（DeepSeek 控制台上的 key 名称，仅作标识，不参与鉴权）
        self.LLM_API_KEY_NAME = os.getenv(
            'LLM_API_KEY_NAME', self.config.get('llm', 'api_key_name', fallback='tool'))
        # API 地址（OpenAI 兼容协议）
        self.LLM_BASE_URL = os.getenv('LLM_BASE_URL',
                                      self.config.get('llm', 'base_url', fallback='https://api.deepseek.com/v1'))
        # 调用超时（秒）：超时后触发降级（见 tools/llm_client.py）
        self.LLM_TIMEOUT = int(os.getenv('LLM_TIMEOUT', self.config.getint('llm', 'timeout', fallback=30)))
        # 是否启用 LLM（关闭后所有 LLM 工具直接走规则降级，便于离线联调）
        self.LLM_ENABLED = os.getenv('LLM_ENABLED',
                                     self.config.get('llm', 'enabled', fallback='true')).lower() == 'true'

        # ==================== 运行策略（远程模型最小验证） ====================
        # ⚠️ 对话模型是 DeepSeek 远程 API，不是本地部署、按 token 计费。
        #    默认只做「最小验证」，不跑完整一轮全量流程；硬限制见 base/run_policy.py
        #    （样本上限 / 远程调用预算 / 全量双闸门）。
        # 是否放开全量：默认 False，只有 ALLOW_FULL_RUN=1 才允许
        self.ALLOW_FULL_RUN = os.getenv(
            'ALLOW_FULL_RUN', self.config.get('run_policy', 'allow_full_run', fallback='0')
        ).strip().lower() in ('1', 'true', 'yes', 'on')
        # 单次运行样本上限（条）
        self.SMOKE_MAX_SAMPLES = int(os.getenv(
            'SMOKE_MAX_SAMPLES',
            self.config.getint('run_policy', 'max_samples', fallback=2)))
        # 单次运行远程模型调用上限（次）
        self.SMOKE_MAX_LLM_CALLS = int(os.getenv(
            'SMOKE_MAX_LLM_CALLS',
            self.config.getint('run_policy', 'max_llm_calls', fallback=6)))

        # ---- 备用模型（需求「主模型不可用 → 切换备用模型」） ----
        # 切换时机：主模型重试耗尽后，若备用模型已配置且开关打开，则用备用模型再试一次；
        #           仍失败才抛 LLMUnavailableError / 走既有降级路径（见 tools/llm_client.py）。
        # 是否启用备用模型切换
        self.LLM_FALLBACK_ENABLED = os.getenv(
            'LLM_FALLBACK_ENABLED',
            self.config.get('llm', 'fallback_enabled', fallback='true')).lower() == 'true'
        # 备用模型名（空串 = 未配置备用模型，行为与切换前完全一致，零回归）
        self.LLM_FALLBACK_MODEL = os.getenv(
            'LLM_FALLBACK_MODEL', self.config.get('llm', 'fallback_model', fallback=''))
        # 备用模型 API 地址（空串 = 沿用主 base_url）
        self.LLM_FALLBACK_BASE_URL = os.getenv(
            'LLM_FALLBACK_BASE_URL', self.config.get('llm', 'fallback_base_url', fallback=''))
        # 备用模型 API 密钥（空串 = 沿用主 api_key；同样是密钥，只从 .env 读取）
        self.LLM_FALLBACK_API_KEY = os.getenv(
            'LLM_FALLBACK_API_KEY', self.config.get('llm', 'fallback_api_key', fallback=''))

        # ==================== PostgreSQL 配置（长期记忆 + Checkpointer） ====================
        self.PG_HOST = os.getenv('PG_HOST', self.config.get('postgres', 'host', fallback='localhost'))
        self.PG_PORT = int(os.getenv('PG_PORT', self.config.getint('postgres', 'port', fallback=5432)))
        self.PG_USER = os.getenv('PG_USER', self.config.get('postgres', 'user', fallback='postgres'))
        self.PG_PASSWORD = os.getenv('PG_PASSWORD', self.config.get('postgres', 'password', fallback='postgres'))
        self.PG_DATABASE = os.getenv('PG_DATABASE', self.config.get('postgres', 'database', fallback='foodagent'))
        # 统一 DSN（psycopg / langgraph checkpointer 都用它）
        self.PG_DSN = (f"postgresql://{self.PG_USER}:{self.PG_PASSWORD}"
                       f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}")
        # 是否启用 PostgreSQL 持久化（关闭时降级为内存 Checkpointer + 文件记忆）
        self.PG_ENABLED = os.getenv('PG_ENABLED',
                                    self.config.get('postgres', 'enabled', fallback='false')).lower() == 'true'

        # ==================== Milvus 配置（菜品/评价/食安向量检索） ====================
        self.MILVUS_HOST = os.getenv('MILVUS_HOST', self.config.get('milvus', 'host', fallback='localhost'))
        self.MILVUS_PORT = int(os.getenv('MILVUS_PORT', self.config.getint('milvus', 'port', fallback=19530)))
        self.MILVUS_DATABASE_NAME = os.getenv('MILVUS_DATABASE_NAME',
                                              self.config.get('milvus', 'database_name', fallback='foodagent'))
        # 菜品知识库集合
        self.MILVUS_MENU_COLLECTION = os.getenv('MILVUS_MENU_COLLECTION',
                                                self.config.get('milvus', 'menu_collection',
                                                                fallback='foodagent_menu_kb'))

        # ==================== JWT 认证配置 ====================
        # 签名密钥：生产环境务必通过 .env 覆盖
        self.JWT_SECRET = os.getenv('JWT_SECRET', self.config.get('jwt', 'secret', fallback='foodagent-dev-secret-change-me'))
        self.JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', self.config.get('jwt', 'algorithm', fallback='HS256'))
        # Token 有效期（分钟），需求文档约定 2 小时
        self.JWT_EXPIRE_MINUTES = int(os.getenv('JWT_EXPIRE_MINUTES',
                                                self.config.getint('jwt', 'expire_minutes', fallback=120)))

        # ==================== 后厨调度阈值 ====================
        # 排队订单数超过该阈值时触发 interrupt，暂停等后厨主管确认（Human-in-the-Loop）
        self.KITCHEN_INTERRUPT_THRESHOLD = int(os.getenv(
            'KITCHEN_INTERRUPT_THRESHOLD',
            self.config.getint('kitchen', 'interrupt_threshold', fallback=10)))
        # 后厨产能：各工位并发上限（用于 assign_stations 分配）
        self.KITCHEN_STATIONS = os.getenv(
            'KITCHEN_STATIONS',
            self.config.get('kitchen', 'stations', fallback='凉菜间,热菜间,面点间,出菜口')).split(',')

        # ==================== 食品安全合规 ====================
        # 留痕日志保留年限（需求：3 年备查）
        self.COMPLIANCE_LOG_YEARS = int(os.getenv('COMPLIANCE_LOG_YEARS',
                                                  self.config.getint('compliance', 'log_years', fallback=3)))
        # 模型输出合规标识（过敏原提示前缀），前端据此高亮
        self.ALLERGEN_ALERT_PREFIX = os.getenv(
            'ALLERGEN_ALERT_PREFIX',
            self.config.get('compliance', 'allergen_alert_prefix', fallback='[过敏原警示]'))

        # ==================== 记忆保留与归档（需求 3.6.5「记忆清理策略」） ====================
        # 需求原文：对话 90 天归档 / 门店偏好永久 / 顾客画像永久 / 评价分析 2 年
        # 约定：值为 0 表示「永久保留，不清理」（用于门店偏好、顾客画像）
        # 生效位置：memory/store.py 的 BaseStore.purge_expired()，统一入口
        #           scripts/purge_expired.py（挂外部定时任务见 docs/OBSERVABILITY.md）
        # 对话记忆（LangGraph Checkpointer，按 thread_id 隔离）归档口径：90 天
        self.MEMORY_RETENTION_CONVERSATION_DAYS = int(os.getenv(
            'MEMORY_RETENTION_CONVERSATION_DAYS',
            self.config.getint('memory', 'conversation_retention_days', fallback=90)))
        # 评价分析（review_analysis）保留：730 天 = 2 年
        self.MEMORY_RETENTION_REVIEW_DAYS = int(os.getenv(
            'MEMORY_RETENTION_REVIEW_DAYS',
            self.config.getint('memory', 'review_retention_days', fallback=730)))
        # 订单状态流转日志（order_status_log）保留：与对话同归档口径 90 天
        self.MEMORY_RETENTION_ORDER_LOG_DAYS = int(os.getenv(
            'MEMORY_RETENTION_ORDER_LOG_DAYS',
            self.config.getint('memory', 'order_log_retention_days', fallback=90)))
        # 门店偏好保留天数：0 = 永久保留（需求明确「门店偏好永久」，默认 0 且不应改）
        self.MEMORY_RETENTION_STORE_PREF_DAYS = int(os.getenv(
            'MEMORY_RETENTION_STORE_PREF_DAYS',
            self.config.getint('memory', 'store_pref_retention_days', fallback=0)))
        # 顾客画像保留天数：0 = 永久保留（需求明确「顾客画像永久」，默认 0 且不应改）
        self.MEMORY_RETENTION_CUSTOMER_DAYS = int(os.getenv(
            'MEMORY_RETENTION_CUSTOMER_DAYS',
            self.config.getint('memory', 'customer_retention_days', fallback=0)))

        # ==================== 应用配置 ====================
        self.SERVICE_PHONE = os.getenv('SERVICE_PHONE',
                                       self.config.get('app', 'service_phone', fallback='400-000-0000'))
        # 对话历史保留轮数
        self.HISTORY_ROUNDS = int(os.getenv('HISTORY_ROUNDS',
                                            self.config.getint('app', 'history_rounds', fallback=10)))
        # 菜品推荐默认数量
        self.DEFAULT_RECOMMEND_TOP_K = int(os.getenv('DEFAULT_RECOMMEND_TOP_K',
                                                     self.config.getint('app', 'recommend_top_k', fallback=8)))
        # 联网搜索失败时的缓存 TTL（秒），默认 24 小时
        self.WEB_SEARCH_CACHE_TTL = int(os.getenv('WEB_SEARCH_CACHE_TTL',
                                                  self.config.getint('app', 'web_search_cache_ttl', fallback=86400)))

        # ==================== 可选重型组件（默认全关，渐进增强） ====================
        # 设计原则：所有重型组件的开关默认 false，不装依赖也能跑通主流程。
        #           依赖清单与启用步骤见 docs/OBSERVABILITY.md

        # ---- LangSmith 全链路追踪 ----
        self.LANGSMITH_ENABLED = os.getenv(
            'LANGSMITH_ENABLED',
            self.config.get('langsmith', 'enabled', fallback='false')).lower() == 'true'
        self.LANGSMITH_API_KEY = os.getenv(
            'LANGSMITH_API_KEY', self.config.get('langsmith', 'api_key', fallback=''))
        self.LANGSMITH_PROJECT = os.getenv(
            'LANGSMITH_PROJECT', self.config.get('langsmith', 'project', fallback='foodagent-dev'))
        self.LANGSMITH_ENDPOINT = os.getenv(
            'LANGSMITH_ENDPOINT',
            self.config.get('langsmith', 'endpoint', fallback='https://api.smith.langchain.com'))
        self.LANGSMITH_SAMPLE_RATE = float(os.getenv(
            'LANGSMITH_SAMPLE_RATE',
            self.config.get('langsmith', 'sample_rate', fallback='1.0')))

        # ---- Redis 缓存 ----
        self.REDIS_ENABLED = os.getenv(
            'REDIS_ENABLED',
            self.config.get('redis', 'enabled', fallback='false')).lower() == 'true'
        self.REDIS_HOST = os.getenv('REDIS_HOST',
                                    self.config.get('redis', 'host', fallback='localhost'))
        self.REDIS_PORT = int(os.getenv('REDIS_PORT',
                                        self.config.getint('redis', 'port', fallback=6379)))
        self.REDIS_DB = int(os.getenv('REDIS_DB',
                                      self.config.getint('redis', 'db', fallback=0)))
        self.REDIS_PASSWORD = os.getenv(
            'REDIS_PASSWORD', self.config.get('redis', 'password', fallback=''))
        self.REDIS_DEFAULT_TTL = int(os.getenv(
            'REDIS_DEFAULT_TTL',
            self.config.getint('redis', 'default_ttl', fallback=300)))
        self.REDIS_KEY_PREFIX = os.getenv(
            'REDIS_KEY_PREFIX', self.config.get('redis', 'key_prefix', fallback='foodagent:'))

        # ---- Prometheus 指标 ----
        self.PROMETHEUS_ENABLED = os.getenv(
            'PROMETHEUS_ENABLED',
            self.config.get('prometheus', 'enabled', fallback='false')).lower() == 'true'
        self.PROMETHEUS_METRICS_PATH = os.getenv(
            'PROMETHEUS_METRICS_PATH',
            self.config.get('prometheus', 'metrics_path', fallback='/metrics'))
        self.PROMETHEUS_NAMESPACE = os.getenv(
            'PROMETHEUS_NAMESPACE',
            self.config.get('prometheus', 'namespace', fallback='foodagent'))
        self.PROMETHEUS_NODE_LEVEL = os.getenv(
            'PROMETHEUS_NODE_LEVEL',
            self.config.get('prometheus', 'node_level_metrics', fallback='true')).lower() == 'true'

        # ---- Locust 压测 ----
        self.LOADTEST_ENABLED = os.getenv(
            'LOADTEST_ENABLED',
            self.config.get('loadtest', 'enabled', fallback='false')).lower() == 'true'
        self.LOADTEST_USERS = int(os.getenv(
            'LOADTEST_USERS', self.config.getint('loadtest', 'users', fallback=50)))
        self.LOADTEST_SPAWN_RATE = int(os.getenv(
            'LOADTEST_SPAWN_RATE', self.config.getint('loadtest', 'spawn_rate', fallback=5)))
        self.LOADTEST_RUN_TIME = int(os.getenv(
            'LOADTEST_RUN_TIME', self.config.getint('loadtest', 'run_time', fallback=120)))
        self.LOADTEST_TENANT_ID = os.getenv(
            'LOADTEST_TENANT_ID',
            self.config.get('loadtest', 'tenant_id', fallback='store_loadtest'))
        self.LOADTEST_USER_ID = os.getenv(
            'LOADTEST_USER_ID',
            self.config.get('loadtest', 'user_id', fallback='loadtest_bot'))

        # ---- Kubernetes 部署参数 ----
        self.K8S_ENABLED = os.getenv(
            'K8S_ENABLED',
            self.config.get('k8s', 'enabled', fallback='false')).lower() == 'true'
        self.K8S_NAMESPACE = os.getenv(
            'K8S_NAMESPACE', self.config.get('k8s', 'namespace', fallback='foodagent'))
        self.K8S_REPLICAS = int(os.getenv(
            'K8S_REPLICAS', self.config.getint('k8s', 'replicas', fallback=2)))
        self.K8S_CPU_REQUEST = os.getenv(
            'K8S_CPU_REQUEST', self.config.get('k8s', 'cpu_request', fallback='500m'))
        self.K8S_CPU_LIMIT = os.getenv(
            'K8S_CPU_LIMIT', self.config.get('k8s', 'cpu_limit', fallback='2000m'))
        self.K8S_MEMORY_REQUEST = os.getenv(
            'K8S_MEMORY_REQUEST', self.config.get('k8s', 'memory_request', fallback='1Gi'))
        self.K8S_MEMORY_LIMIT = os.getenv(
            'K8S_MEMORY_LIMIT', self.config.get('k8s', 'memory_limit', fallback='2Gi'))
        self.K8S_LIVENESS_PATH = os.getenv(
            'K8S_LIVENESS_PATH', self.config.get('k8s', 'liveness_path', fallback='/health'))
        self.K8S_READINESS_PATH = os.getenv(
            'K8S_READINESS_PATH', self.config.get('k8s', 'readiness_path', fallback='/health'))
        self.K8S_HPA_ENABLED = os.getenv(
            'K8S_HPA_ENABLED',
            self.config.get('k8s', 'hpa_enabled', fallback='true')).lower() == 'true'
        self.K8S_HPA_MIN = int(os.getenv(
            'K8S_HPA_MIN', self.config.getint('k8s', 'hpa_min_replicas', fallback=2)))
        self.K8S_HPA_MAX = int(os.getenv(
            'K8S_HPA_MAX', self.config.getint('k8s', 'hpa_max_replicas', fallback=10)))
        self.K8S_HPA_CPU_TARGET = int(os.getenv(
            'K8S_HPA_CPU_TARGET', self.config.getint('k8s', 'hpa_cpu_target', fallback=70)))

        # ==================== 意图分类：9 类餐饮意图 ====================
        # 全项目唯一来源：Orchestrator 路由、意图识别工具、前端下拉均由此派生
        self.INTENT_ROUTES = {
            'dish_recommend': {'agent': 'order', 'complex': False, 'desc': '菜品推荐'},
            'smart_order': {'agent': 'order', 'complex': True, 'desc': '智能点餐'},
            'allergen_query': {'agent': 'order', 'complex': False, 'desc': '过敏原查询'},
            'kitchen_dispatch': {'agent': 'kitchen', 'complex': True, 'desc': '后厨调度'},
            'inventory_check': {'agent': 'kitchen', 'complex': False, 'desc': '食材库存检查'},
            'review_analysis': {'agent': 'review', 'complex': True, 'desc': '评价分析'},
            'improvement_advice': {'agent': 'review', 'complex': False, 'desc': '改进建议'},
            'food_safety_query': {'agent': 'safety', 'complex': False, 'desc': '食安法规问答'},
            'nutrition_query': {'agent': 'safety', 'complex': False, 'desc': '营养健康问答'},
        }
        # 可能的默认兜底意图（无法识别时路由到 OrderAgent）
        self.DEFAULT_INTENT = os.getenv('DEFAULT_INTENT', 'dish_recommend')

        # ==================== 预设过敏原库（50+ 种常见过敏原） ====================
        # 食品安全规则引擎的判定基准，需求文档要求「预设过敏原库 + 精确匹配」
        self.ALLERGEN_LIBRARY = [
            # 八大类致敏原（国标 GB 7718 参考）
            '花生', '坚果', '大豆', '小麦', '麸质', '乳制品', '牛奶', '鸡蛋', '蛋类',
            '鱼类', '甲壳类', '虾', '蟹', '贝类', '牡蛎', '蛤蜊', '鱿鱼', '章鱼',
            # 其他常见致敏原
            '芝麻', '亚硫酸盐', '二氧化硫', '芹菜', '芥末', '羽扇豆', '软体动物',
            '腰果', '杏仁', '核桃', '榛子', '开心果', '松子', '栗子', '夏威夷果',
            '芒果', '菠萝', '猕猴桃', '桃子', '草莓', '荔枝', '龙眼',
            '香菜', '葱', '蒜', '洋葱', '韭菜',
            '羊肉', '牛肉', '猪肉', '鸡肉', '鸭肉',
            '食用色素', '味精', '防腐剂', '甜味剂', '酒精',
            '蘑菇', '木耳', '笋', '茄子', '番茄', '黄瓜',
            '辣椒', '花椒', '八角', '桂皮',
        ]
        # 去重后保持稳定（供随时扩展）
        self.ALLERGEN_LIBRARY = list(dict.fromkeys(self.ALLERGEN_LIBRARY))

        # 过敏原衍生物表：配料表里常见的加工形态 → 对应的基础过敏原
        #
        # 为什么单独建这张表：顾客声明的往往是基础过敏原（「我对花生过敏」），
        # 而配方里写的是加工形态（「花生油」「花生酱」）。只做字符交集会全部漏判，
        # 属于典型的安全侧漏——实测 5 条衍生物对抗用例曾 5/5 漏判。
        # 取向：宁可误报不可漏判，命中任一衍生物即视为含对应过敏原，由人工确认最终处置。
        self.ALLERGEN_DERIVATIVES = {
            '花生': ['花生油', '花生酱', '花生碎', '花生米', '花生粉', '花生仁'],
            '坚果': ['坚果碎', '混合坚果', '坚果酱', '果仁'],
            '大豆': ['豆浆', '豆腐', '豆干', '豆皮', '腐竹', '酱油', '生抽', '老抽',
                     '豆豉', '味噌', '黄豆', '毛豆', '豆芽', '豆乳', '纳豆'],
            '小麦': ['面粉', '面条', '面包', '馒头', '饺子皮', '面筋', '淀粉', '挂面', '油条'],
            '麸质': ['面粉', '面条', '面包', '馒头', '面筋', '麦麸', '挂面', '油条'],
            '乳制品': ['黄油', '奶油', '芝士', '奶酪', '炼乳', '酸奶', '奶粉', '淡奶油',
                       '鲜奶', '起司', '奶昔'],
            '牛奶': ['黄油', '奶油', '芝士', '奶酪', '炼乳', '酸奶', '奶粉', '淡奶油',
                     '鲜奶', '起司', '奶昔'],
            '鸡蛋': ['蛋黄酱', '沙拉酱', '蛋清', '蛋黄', '蛋液', '鸡蛋液', '皮蛋', '咸蛋', '蛋白霜'],
            '甲壳类': ['蚝油', '虾酱', '虾米', '虾皮', '虾仁', '蟹黄', '蟹肉', '虾滑'],
            '贝类': ['蚝油', '蚝豉', '干贝', '瑶柱', '扇贝', '蛤蜊肉'],
            '鱼类': ['鱼露', '鱼丸', '鱼干', '鱼片', '鳕鱼', '三文鱼', '金枪鱼', '鱼豆腐'],
            '芝麻': ['芝麻油', '香油', '芝麻酱', '麻酱', '芝麻粉'],
            '芥末': ['芥末酱', '芥末油', '山葵'],
            '亚硫酸盐': ['二氧化硫', '焦亚硫酸钠', '焦亚硫酸钾'],
            '二氧化硫': ['亚硫酸盐', '焦亚硫酸钠', '焦亚硫酸钾'],
        }
        # 反向索引：衍生物 → [基础过敏原]，供判定时 O(1) 归一化
        self.ALLERGEN_DERIVATIVE_INDEX = {}
        for _base, _derivs in self.ALLERGEN_DERIVATIVES.items():
            for _d in _derivs:
                self.ALLERGEN_DERIVATIVE_INDEX.setdefault(_d, []).append(_base)

        # 过敏原风险等级划分
        self.HIGH_RISK_ALLERGENS = ['花生', '坚果', '腰果', '杏仁', '核桃', '榛子', '开心果',
                                    '甲壳类', '虾', '蟹', '贝类', '芝麻', '鱼类']

    # ==================== 便捷方法 ====================

    def get_agent_for_intent(self, intent: str) -> str:
        """根据意图名返回应路由的 Agent 标识（order/kitchen/review/safety）"""
        route = self.INTENT_ROUTES.get(intent)
        if route:
            return route['agent']
        return self.INTENT_ROUTES[self.DEFAULT_INTENT]['agent']

    def is_complex_intent(self, intent: str) -> bool:
        """判断意图是否为「复杂」类型（复杂意图支持多 Agent 串联 Pipeline）"""
        route = self.INTENT_ROUTES.get(intent)
        return bool(route and route['complex'])

    def memory_retention(self) -> dict:
        """
        长期记忆各数据域的保留天数（需求 3.6.5「记忆清理策略」的唯一来源）。

        约定：值为 0 表示「永久保留，不清理」。
            conversation      对话记忆（Checkpointer）90 天归档
            review_analysis   评价分析 730 天（2 年）
            order_status_log  订单状态流转日志 90 天（与对话同归档口径）
            compliance_log    合规留痕保留年限（3 年，由 COMPLIANCE_LOG_YEARS 换算）
            store_preferences 门店偏好 = 0（永久）
            customer_profiles 顾客画像 = 0（永久）
        """
        return {
            'conversation': self.MEMORY_RETENTION_CONVERSATION_DAYS,
            'review_analysis': self.MEMORY_RETENTION_REVIEW_DAYS,
            'order_status_log': self.MEMORY_RETENTION_ORDER_LOG_DAYS,
            'compliance_log': self.COMPLIANCE_LOG_YEARS,
            'store_preferences': self.MEMORY_RETENTION_STORE_PREF_DAYS,
            'customer_profiles': self.MEMORY_RETENTION_CUSTOMER_DAYS,
        }

    def validate(self) -> dict:
        """
        启动期配置自检：返回各类配置的健康状态，供 /health 接口与启动日志使用。
        不抛异常——缺失项以 warning 呈现，保证「缺配置也能起来，只是降级」。
        """
        return {
            'llm': {
                'enabled': self.LLM_ENABLED,
                'api_key_present': bool(self.LLM_API_KEY and self.LLM_API_KEY.startswith('sk-')),
                'model': self.LLM_MODEL,
                'base_url': self.LLM_BASE_URL,
                # 备用模型（主模型不可用时的切换目标；空串 = 未配置）
                'fallback_enabled': self.LLM_FALLBACK_ENABLED,
                'fallback_model': self.LLM_FALLBACK_MODEL,
            },
            'postgres': {'enabled': self.PG_ENABLED, 'dsn': f"{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"},
            'milvus': {'host': f"{self.MILVUS_HOST}:{self.MILVUS_PORT}", 'collection': self.MILVUS_MENU_COLLECTION},
            'memory_retention': self.memory_retention(),
            'intents': list(self.INTENT_ROUTES.keys()),
            'allergen_count': len(self.ALLERGEN_LIBRARY),
        }


# 全局单例配置对象
config = Config()


if __name__ == '__main__':
    print('=' * 60)
    print('FoodAgent 配置自检')
    print('=' * 60)
    import json as _json

    print(_json.dumps(config.validate(), ensure_ascii=False, indent=2))
    print('APP_PORT      =', config.APP_PORT)
    print('INTENT_ROUTES =', list(config.INTENT_ROUTES.keys()))
    print('过敏原库条数   =', len(config.ALLERGEN_LIBRARY))
