-- Vibe-Trading A股数据 DuckDB schema
-- 对应 A_SHARE_ARCHITECTURE.md v1.2 Section 2.5

-- 股票基本信息
CREATE TABLE IF NOT EXISTS symbol_meta (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    list_date   INTEGER,
    board       TEXT,
    delist_date INTEGER,
    update_ts   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 退市股列表
CREATE TABLE IF NOT EXISTS delisted_stocks (
    symbol       TEXT PRIMARY KEY,
    delist_date  INTEGER,
    delist_reason TEXT,
    source       TEXT,
    update_ts    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ST状态变更记录
CREATE TABLE IF NOT EXISTS st_status_events (
    symbol     TEXT,
    event_date INTEGER,
    st_type    INTEGER,
    source     TEXT,
    update_ts  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, event_date)
);

-- 停牌记录
CREATE TABLE IF NOT EXISTS suspension_records (
    symbol        TEXT,
    suspend_date  INTEGER,
    resume_date   INTEGER,
    suspend_type  TEXT,
    reason        TEXT,
    trading_halt_ratio REAL,
    source        TEXT,
    update_ts     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, suspend_date)
);

-- 新股/退市整理期记录
CREATE TABLE IF NOT EXISTS new_listing_records (
    symbol              TEXT PRIMARY KEY,
    list_date           INTEGER,
    board               TEXT,
    first_limit_up_date INTEGER,
    source              TEXT,
    update_ts           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 历史涨停记录
CREATE TABLE IF NOT EXISTS limit_up_history (
    symbol       TEXT,
    trade_date   INTEGER,
    limit_up_type INTEGER,
    close        REAL,
    amount       REAL,
    seal_strength REAL,
    source       TEXT,
    update_ts    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);

-- 申万行业分类
CREATE TABLE IF NOT EXISTS sw_industry (
    stock_symbol    TEXT,
    industry_code   TEXT,
    industry_name   TEXT,
    level           INTEGER,
    effective_date  INTEGER,
    source          TEXT DEFAULT 'sw',
    update_ts       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_symbol, effective_date)
);

-- 东财概念板块成员
CREATE TABLE IF NOT EXISTS concept_board_members (
    board_code   TEXT,
    board_name   TEXT,
    stock_symbol TEXT,
    join_date    INTEGER,
    leave_date   INTEGER,
    source       TEXT DEFAULT 'em',
    update_ts    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (board_code, stock_symbol, join_date)
);

-- 板块日频资金流向
CREATE TABLE IF NOT EXISTS sector_daily_flow (
    board_code      TEXT,
    board_name      TEXT,
    trade_date      INTEGER,
    turnover        REAL,
    flow_pct        REAL,
    limit_up_count  INTEGER,
    source          TEXT,
    update_ts       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (board_code, trade_date)
);

-- 北向资金行业配置
CREATE TABLE IF NOT EXISTS north_flow_industry (
    trade_date     INTEGER,
    industry_code  TEXT,
    industry_name  TEXT,
    net_inflow     REAL,
    net_inflow_pct REAL,
    update_ts      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, industry_code)
);

-- 指数元数据
CREATE TABLE IF NOT EXISTS index_meta (
    symbol     TEXT PRIMARY KEY,
    name       TEXT,
    board      TEXT,
    base_date  INTEGER,
    base_point REAL,
    update_ts  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 指数日线
CREATE TABLE IF NOT EXISTS index_daily (
    symbol      TEXT,
    trade_date  INTEGER,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    change_pct  REAL,
    source      TEXT,
    update_ts   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);

-- 数据版本快照
CREATE TABLE IF NOT EXISTS data_snapshots (
    snapshot_date     DATE PRIMARY KEY,
    akshare_version  TEXT,
    tdx_export_date  TEXT,
    duckdb_version   TEXT,
    note             TEXT
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_symbol_list_date ON symbol_meta(list_date);
CREATE INDEX IF NOT EXISTS idx_symbol_delist_date ON symbol_meta(delist_date);
CREATE INDEX IF NOT EXISTS idx_st_status_date ON st_status_events(event_date);
CREATE INDEX IF NOT EXISTS idx_suspension_resume ON suspension_records(resume_date);
CREATE INDEX IF NOT EXISTS idx_limit_up_date ON limit_up_history(trade_date);
CREATE INDEX IF NOT EXISTS idx_sw_industry_effective ON sw_industry(effective_date);
CREATE INDEX IF NOT EXISTS idx_concept_join ON concept_board_members(join_date);
CREATE INDEX IF NOT EXISTS idx_sector_flow_date ON sector_daily_flow(trade_date);
CREATE INDEX IF NOT EXISTS idx_north_flow_date ON north_flow_industry(trade_date);
CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(trade_date);
