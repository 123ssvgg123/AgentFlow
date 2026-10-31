"""
    记忆系统数据表定义：memory/schemas.py

    职责：定义长期记忆的 6 张核心表结构，并生成建表 SQL。
        这是全项目「表结构唯一来源」——docs/deploy/schema.sql 由本文件导出。

    对应需求文档 3.6 的「典型表结构」：
        store_preferences   门店菜品偏好表
        customer_profiles   顾客画像表
        review_analysis     评价分析记录表
        ingredient_usage    食材消耗记录表（需求 3.6 ③「食材消耗追踪」）
        compliance_log      合规留痕表（需求 3.8 Q8「过敏原订单留痕 3 年」）
        order_status_log    订单状态流转表（需求 3.7「全链路可追溯」）

    隔离约定（需求 3.6 ③）：
        所有表都含 tenant_id（门店 ID），查询强制加 WHERE tenant_id = ?
        员工级数据额外含 user_id，查询加 AND user_id = ?
"""

# ==================================================================
# 门店菜品偏好表
# ==================================================================
STORE_PREFERENCES_DDL = """
CREATE TABLE IF NOT EXISTS store_preferences (
    id                       SERIAL PRIMARY KEY,
    tenant_id                VARCHAR(64) NOT NULL,           -- 门店 ID
    popular_dishes           TEXT[]      DEFAULT '{}',       -- 热门菜品
    seasonal_preferences     JSONB       DEFAULT '{}',       -- 季节偏好
    customer_feedback_summary JSONB      DEFAULT '{}',       -- 顾客反馈汇总
    avg_order_value          FLOAT       DEFAULT 0,          -- 平均客单价
    peak_hours               JSONB       DEFAULT '[]',       -- 高峰时段
    updated_at               TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    created_at               TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_store_pref_tenant ON store_preferences (tenant_id);
-- 唯一约束：一个门店只保留一行偏好（PostgresStore.save_store_preferences 走 ON CONFLICT 幂等写入，
-- 否则每次保存都会追加一行，表无限膨胀且读取要 ORDER BY 取最新）
CREATE UNIQUE INDEX IF NOT EXISTS uq_store_pref_tenant ON store_preferences (tenant_id);
"""

# ==================================================================
# 顾客画像表
# ==================================================================
CUSTOMER_PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS customer_profiles (
    id                   SERIAL PRIMARY KEY,
    customer_id          VARCHAR(64) NOT NULL,
    tenant_id            VARCHAR(64) NOT NULL,               -- 常去门店 ID
    user_id              VARCHAR(64) DEFAULT '',             -- 录入员工 ID（员工级隔离）
    name                 VARCHAR(64) DEFAULT '',
    dietary_restrictions TEXT[]      DEFAULT '{}',           -- 饮食限制（素食/清真/低卡等）
    allergens            TEXT[]      DEFAULT '{}',           -- 过敏原
    favorite_dishes      TEXT[]      DEFAULT '{}',           -- 喜欢的菜品
    visit_count          INT         DEFAULT 0,              -- 到店次数
    avg_spending         FLOAT       DEFAULT 0,              -- 平均消费
    member_level         VARCHAR(20) DEFAULT '普通',         -- 会员等级
    last_visit_date      DATE,
    created_at           TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_customer_tenant ON customer_profiles (tenant_id, customer_id);
-- 唯一约束：同一门店下同一顾客只有一条画像（PostgresStore.upsert_customer_profile 依赖它做 UPSERT，
-- 否则纯 INSERT 会堆积重复行，而读取是 LIMIT 1 且无 ORDER BY，可能读到旧画像）
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_tenant ON customer_profiles (tenant_id, customer_id);
"""

# ==================================================================
# 评价分析记录表
# ==================================================================
REVIEW_ANALYSIS_DDL = """
CREATE TABLE IF NOT EXISTS review_analysis (
    id               SERIAL PRIMARY KEY,
    tenant_id        VARCHAR(64)  NOT NULL,
    user_id          VARCHAR(64)  DEFAULT '',
    review_id        VARCHAR(128) NOT NULL,                  -- 平台评价 ID
    platform         VARCHAR(20),                            -- meituan/dianping/douyin
    rating           INT,                                    -- 评分（1-5）
    sentiment        VARCHAR(10),                            -- positive/neutral/negative
    sentiment_score  FLOAT,                                  -- 情感强度
    topics           TEXT[]       DEFAULT '{}',              -- 关键主题（口味/服务/环境/价格）
    summary          TEXT,                                   -- 摘要
    suggested_reply  TEXT,                                   -- 建议回复
    reply_status     VARCHAR(20)  DEFAULT 'pending',         -- pending/replied/escalated
    review_date      DATE,
    created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_review_tenant_date ON review_analysis (tenant_id, review_date);
CREATE INDEX IF NOT EXISTS idx_review_sentiment ON review_analysis (tenant_id, sentiment);
"""

# ==================================================================
# 食材消耗记录表
# ==================================================================
INGREDIENT_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS ingredient_usage (
    id            SERIAL PRIMARY KEY,
    tenant_id     VARCHAR(64) NOT NULL,
    usage_date    DATE        NOT NULL,
    ingredient    VARCHAR(64) NOT NULL,
    used_amount   FLOAT       DEFAULT 0,                     -- 消耗量
    unit          VARCHAR(16) DEFAULT 'g',
    order_count   INT         DEFAULT 0,                     -- 关联订单数
    created_at    TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ingredient_tenant_date ON ingredient_usage (tenant_id, usage_date);
-- 唯一约束：同门店 + 同日期 + 同食材只留一行，供 add_ingredient_usage 做原子累加
-- （PostgresStore.add_ingredient_usage 用 ON CONFLICT DO UPDATE SET used_amount = 旧值 + 增量）
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingredient_tenant_date_name
    ON ingredient_usage (tenant_id, usage_date, ingredient);
"""

# ==================================================================
# 订单状态流转表（需求 3.7「全链路可追溯」）
# ==================================================================
ORDER_STATUS_LOG_DDL = """
CREATE TABLE IF NOT EXISTS order_status_log (
    id            SERIAL PRIMARY KEY,
    tenant_id     VARCHAR(64) NOT NULL,                      -- 门店 ID
    user_id       VARCHAR(64) DEFAULT '',                    -- 操作员工 ID
    order_id      VARCHAR(64) NOT NULL,                      -- 订单号
    from_status   VARCHAR(32),                               -- 变更前状态（首个事件为 NULL）
    to_status     VARCHAR(32) NOT NULL,                      -- 变更后状态
    operator      VARCHAR(64) DEFAULT 'system',              -- 变更来源（agent/user/system）
    reason        TEXT,                                      -- 变更原因
    meta          JSONB       DEFAULT '{}',                  -- 附加信息（菜品/金额/工位等）
    created_at    TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_status_order ON order_status_log (tenant_id, order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_order_status_to ON order_status_log (tenant_id, to_status);
"""

# ==================================================================
# 合规留痕表（需求 3.8 Q8：过敏原订单留痕保存 3 年）
# ==================================================================
COMPLIANCE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS compliance_log (
    id                SERIAL PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    user_id           VARCHAR(64) DEFAULT '',
    order_id          VARCHAR(64),
    declared_allergens TEXT[]     DEFAULT '{}',              -- 顾客声明的过敏原
    detected_allergens TEXT[]     DEFAULT '{}',              -- 检出并标记的过敏原
    recommended_dishes TEXT[]     DEFAULT '{}',              -- 推荐菜品
    isolation_action  TEXT,                                  -- 后厨隔离处理措施
    action            VARCHAR(64),                           -- trigger_food_safety_review / normal
    -- 口头告知留痕（需求 3.8 Q8：过敏原须「口头告知」顾客并留痕可追溯）
    notice_channel    VARCHAR(32) DEFAULT 'system',          -- 告知渠道：verbal(口头)/sms/system
    notice_content    TEXT,                                  -- 告知内容原文
    noticed_at        TIMESTAMP,                             -- 告知时间
    confirmed_by      VARCHAR(64) DEFAULT '',                -- 确认人（服务员 ID）
    confirmed_at      TIMESTAMP,                             -- 顾客口头确认时间
    customer_ack      BOOLEAN     DEFAULT FALSE,             -- 顾客是否已确认知悉
    retention_years   INT         DEFAULT 3,
    created_at        TIMESTAMP   DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_compliance_tenant ON compliance_log (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_compliance_order ON compliance_log (tenant_id, order_id);
"""

# 全部建表语句（顺序无关，均带 IF NOT EXISTS）
ALL_DDL = [
    STORE_PREFERENCES_DDL,
    CUSTOMER_PROFILES_DDL,
    REVIEW_ANALYSIS_DDL,
    INGREDIENT_USAGE_DDL,
    COMPLIANCE_LOG_DDL,
    ORDER_STATUS_LOG_DDL,
]

# 表清单（供 /health 与文档使用）
TABLES = {
    "store_preferences": "门店菜品偏好表",
    "customer_profiles": "顾客画像表",
    "review_analysis": "评价分析记录表",
    "ingredient_usage": "食材消耗记录表",
    "compliance_log": "合规留痕表",
    "order_status_log": "订单状态流转表",
}


def init_sql() -> str:
    """导出完整建表 SQL（供 docs/deploy/schema.sql 生成）"""
    header = f"""-- FoodAgent 数据库建表脚本
-- 由 memory/schemas.py 自动导出，请勿手工修改
-- 包含 {len(TABLES)} 张表：{'、'.join(TABLES.values())}

"""
    return header + "\n".join(ALL_DDL)


if __name__ == '__main__':
    print(init_sql())
