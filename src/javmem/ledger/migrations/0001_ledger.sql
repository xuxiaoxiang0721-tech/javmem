-- 第一版账本：设计稿 01 p3–4 的 11 张表。
-- 关系表和关系证据表到 J4.1 再建：到时加一个新的迁移文件，这个文件不再改。
--
-- 约定：
--   时间都是带时区的 ISO 8601 文本。*_epoch 是同一时刻的秒数，比较和排序用它，
--   因为不同时区的文本不能直接比。
--   列表和结构存 JSON 文本。
--   正式库和测试库是两个文件，表结构一样，所以这里没有“哪个库”的字段。


-- 1. 包登记：包号、来源 Bot、环境、覆盖时段、处理状态、是否撤回
--
-- 一个包号可以有几行，一个 revision 一行。state：
--   registered  登记了，还没做完归档和建索引。启动对账会接着做
--   current     现行的版本。查询和重跑都只认它
--   superseded  被更大的 revision 替换了，只留档
CREATE TABLE packets (
    packet_id         TEXT    NOT NULL,                      -- 包号
    revision          INTEGER NOT NULL CHECK (revision >= 1),
    source            TEXT    NOT NULL,                      -- 来源 Bot
    env               TEXT    NOT NULL CHECK (env IN ('prod', 'test')),  -- 环境
    framework         TEXT    NOT NULL,                      -- 只作记录
    channel           TEXT    NOT NULL CHECK (channel IN ('chat', 'email')),
    mailbox           TEXT,
    kind              TEXT    NOT NULL CHECK (kind IN ('daily', 'adhoc', 'backfill')),
    created_at        TEXT    NOT NULL,
    covers_from       TEXT    NOT NULL,                      -- 覆盖时段
    covers_to         TEXT    NOT NULL,
    covers_from_epoch INTEGER NOT NULL,
    covers_to_epoch   INTEGER NOT NULL,
    partitions        TEXT    NOT NULL,                      -- 分区：包头 domain，JSON 列表
    sensitive         TEXT    NOT NULL DEFAULT '[]',         -- 敏感标记：包头 sensitive，privacy 是 private 的再加 private
    privacy           TEXT    NOT NULL CHECK (privacy IN ('normal', 'private')),
    entities          TEXT    NOT NULL DEFAULT '[]',         -- 包头 entities，JSON 列表
    continues         TEXT,
    content_hash      TEXT    NOT NULL,                      -- 统一换行符、去掉 BOM 后内容的 sha256
    inbox_path        TEXT    NOT NULL,                      -- 收的时候文件在哪，相对数据目录
    archive_path      TEXT    NOT NULL,                      -- 归档在哪，相对数据目录
    state             TEXT    NOT NULL CHECK (state IN ('registered', 'current', 'superseded')),  -- 处理状态
    extraction        TEXT    NOT NULL DEFAULT 'queued'
                      CHECK (extraction IN ('queued', 'running', 'done', 'failed', 'skipped')),   -- 抽到哪了
    received_at       TEXT    NOT NULL,
    indexed_at        TEXT,
    superseded_at     TEXT,
    withdrawn_at      TEXT,                                  -- 是否撤回：有时间就是撤回了
    PRIMARY KEY (packet_id, revision)
);
CREATE UNIQUE INDEX packets_one_current ON packets (packet_id) WHERE state = 'current';
CREATE UNIQUE INDEX packets_one_registered ON packets (packet_id) WHERE state = 'registered';
CREATE INDEX packets_by_source ON packets (source, kind, state);


-- 2. 原文索引：原文分句，能按两个字的人名查到
--
-- 三个字以上走 raw_fts（FTS5 trigram），两个字对 text 用 LIKE。
-- 一句话一行；邮件头的主题、发件人这些每个字段一行，这时 field 有值，line_no、sentence_no 是空的。
CREATE TABLE raw_sentences (
    id            INTEGER PRIMARY KEY,
    sentence_id   TEXT    NOT NULL UNIQUE,                   -- 包号#小节.行.句，见 query-api 4.2
    packet_id     TEXT    NOT NULL,
    revision      INTEGER NOT NULL,
    seq           INTEGER NOT NULL,                          -- 包内的先后，取上下文用
    section_kind  TEXT    NOT NULL CHECK (section_kind IN ('raw', 'attachment')),
    section_no    INTEGER,                                   -- 第几个原话小节，附件小节是空的
    line_no       INTEGER,
    sentence_no   INTEGER,
    field         TEXT CHECK (field IN ('subject', 'from', 'to', 'cc', 'bcc', 'attachments')),
    text          TEXT    NOT NULL,                          -- 原句，逐字
    speaker       TEXT    NOT NULL,
    speaker_role  TEXT    NOT NULL CHECK (speaker_role IN ('owner', 'bot', 'other')),
    said_at       TEXT    NOT NULL,
    said_at_epoch INTEGER NOT NULL,
    origin        TEXT    NOT NULL CHECK (origin IN ('said', 'relayed', 'external', 'attachment_note')),
    quoted        INTEGER NOT NULL DEFAULT 0 CHECK (quoted IN (0, 1)),
    message_id    TEXT,                                      -- 邮件才有
    thread_id     TEXT,
    subject       TEXT,
    withdrawn_at  TEXT,                                      -- 单条撤回时标在这里
    FOREIGN KEY (packet_id, revision) REFERENCES packets (packet_id, revision)
);
CREATE INDEX raw_sentences_by_time ON raw_sentences (said_at_epoch, id);
CREATE INDEX raw_sentences_by_packet ON raw_sentences (packet_id, seq);
CREATE INDEX raw_sentences_by_thread ON raw_sentences (thread_id) WHERE thread_id IS NOT NULL;

CREATE VIRTUAL TABLE raw_fts USING fts5 (
    text,
    content = 'raw_sentences',
    content_rowid = 'id',
    tokenize = 'trigram'
);
CREATE TRIGGER raw_sentences_after_insert AFTER INSERT ON raw_sentences BEGIN
    INSERT INTO raw_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER raw_sentences_after_delete AFTER DELETE ON raw_sentences BEGIN
    INSERT INTO raw_fts (raw_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER raw_sentences_after_update AFTER UPDATE OF text ON raw_sentences BEGIN
    INSERT INTO raw_fts (raw_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO raw_fts (rowid, text) VALUES (new.id, new.text);
END;


-- 3. 实体：编号、标准名、类型
CREATE TABLE entities (
    entity_id      TEXT PRIMARY KEY,                         -- 编号，E 开头
    canonical_name TEXT NOT NULL,                            -- 标准名
    type           TEXT NOT NULL
                   CHECK (type IN ('人', '公司或机构', '项目', '订单或合同', '地点', '资产')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX entities_by_name ON entities (canonical_name COLLATE NOCASE);


-- 4. 别名：各种叫法对到实体编号，附出处（出处在 evidence）
CREATE TABLE aliases (
    alias_id     TEXT PRIMARY KEY,                           -- 编号，A 开头
    entity_id    TEXT NOT NULL REFERENCES entities (entity_id),
    alias        TEXT NOT NULL,                              -- 叫法
    status       TEXT NOT NULL CHECK (status IN ('verified', 'inferred', 'pending')),
    withdrawn_at TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (entity_id, alias)
);
CREATE INDEX aliases_by_name ON aliases (alias COLLATE NOCASE);


-- 5. 事实：事实文字、主体、属性、值、发生时间和精度、确定程度、说话人、冻结标记
--
-- 不同可见范围的事实不合并（D30），所以每条事实都带自己的可见范围。
CREATE TABLE facts (
    fact_id           TEXT PRIMARY KEY,                      -- 编号，F 开头
    content           TEXT NOT NULL,                         -- 事实文字
    subject_entity_id TEXT REFERENCES entities (entity_id),  -- 主体
    attribute         TEXT,                                  -- 属性
    value             TEXT,                                  -- 值
    occurred_from     TEXT,                                  -- 发生时间，写到 time_precision 那一级
    occurred_to       TEXT,                                  -- 失效时间，含 from 不含 to
    time_precision    TEXT NOT NULL DEFAULT 'unknown'        -- 精度
                      CHECK (time_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown')),
    status            TEXT NOT NULL CHECK (status IN ('verified', 'inferred', 'pending')),  -- 确定程度
    pending_reason    TEXT CHECK (pending_reason IN ('ambiguous_entity', 'frozen_conflict', 'soft_change',
                                                     'witness_mismatch', 'manual_review', 'other')),
    speaker           TEXT,                                  -- 说话人
    speaker_role      TEXT CHECK (speaker_role IN ('owner', 'bot', 'other')),
    frozen            INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1)),  -- 冻结标记
    scope_partitions  TEXT NOT NULL,                         -- 可见范围：分区，JSON 列表
    scope_sensitive   TEXT NOT NULL DEFAULT '[]',            -- 可见范围：敏感标记，JSON 列表
    withdrawn_at      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX facts_by_subject ON facts (subject_entity_id);


-- 6. 事实和实体：每条事实涉及哪些实体，各自的角色：主体、对象、提及
CREATE TABLE fact_entities (
    fact_id   TEXT NOT NULL REFERENCES facts (fact_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities (entity_id),
    role      TEXT NOT NULL CHECK (role IN ('subject', 'object', 'mention')),
    PRIMARY KEY (fact_id, entity_id, role)
);
CREATE INDEX fact_entities_by_entity ON fact_entities (entity_id);


-- 7. 证据：每条事实出自哪个包、哪句原话、在什么位置（别名的出处也在这里）
--
-- 01 里证据管事实和关系。关系证据按导航到 J4.1 单独建表，这张表现在只管事实和别名。
-- 说话人、时间、origin 这些按 sentence_id 从原文索引取。原文索引能重建，句子编号不变。
CREATE TABLE evidence (
    evidence_id INTEGER PRIMARY KEY,
    fact_id     TEXT REFERENCES facts (fact_id) ON DELETE CASCADE,
    alias_id    TEXT REFERENCES aliases (alias_id) ON DELETE CASCADE,
    packet_id   TEXT    NOT NULL,                            -- 哪个包
    revision    INTEGER NOT NULL,
    sentence_id TEXT    NOT NULL,                            -- 哪一句
    quote       TEXT    NOT NULL,                            -- 哪句原话，逐字
    position    TEXT    NOT NULL,                            -- 在什么位置：query-api 4.5 的 position，JSON
    created_at  TEXT    NOT NULL,
    FOREIGN KEY (packet_id, revision) REFERENCES packets (packet_id, revision),
    CHECK ((fact_id IS NULL) <> (alias_id IS NULL))
);
CREATE INDEX evidence_by_fact ON evidence (fact_id) WHERE fact_id IS NOT NULL;
CREATE INDEX evidence_by_alias ON evidence (alias_id) WHERE alias_id IS NOT NULL;
CREATE INDEX evidence_by_packet ON evidence (packet_id, revision);


-- 8. 隔离：L4 没过的，附没过的原因
CREATE TABLE quarantine (
    quarantine_id INTEGER PRIMARY KEY,
    packet_id     TEXT    NOT NULL,
    revision      INTEGER NOT NULL,
    item_kind     TEXT    NOT NULL,                          -- 没过的是什么：事实、实体、别名，J4.1 起还有关系
    payload       TEXT    NOT NULL,                          -- 抽取结果原样，JSON
    reason        TEXT    NOT NULL,                          -- 没过的原因
    rule_id       TEXT,                                      -- 哪条规则没过，比如 G1
    rule_version  TEXT,
    created_at    TEXT    NOT NULL,
    FOREIGN KEY (packet_id, revision) REFERENCES packets (packet_id, revision)
);
CREATE INDEX quarantine_by_packet ON quarantine (packet_id, revision);


-- 9. 待确认：待主人定的事：类型、几种说法、原文、决定和时间
CREATE TABLE review_queue (
    review_id  INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL                                 -- 类型
               CHECK (kind IN ('ambiguous_entity', 'frozen_conflict', 'soft_change',
                               'witness_mismatch', 'manual_review', 'other')),
    claims     TEXT NOT NULL,                                -- 几种说法，JSON
    quotes     TEXT NOT NULL,                                -- 原文，JSON
    decision   TEXT,                                         -- 决定
    decided_at TEXT,                                         -- 决定的时间
    created_at TEXT NOT NULL
);
CREATE INDEX review_queue_open ON review_queue (created_at) WHERE decided_at IS NULL;


-- 10. 规则：规则编号、版本、落在哪一层、来自哪几道题
CREATE TABLE rules (
    rule_id        TEXT NOT NULL,                            -- 规则编号，比如 G1
    version        TEXT NOT NULL,                            -- 版本
    layer          TEXT NOT NULL,                            -- 落在哪一层，比如 L4
    from_questions TEXT NOT NULL DEFAULT '[]',               -- 来自哪几道题，JSON 列表
    description    TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);


-- 11. 留痕：投影检查和裁判每次信了谁、依据哪段原文、概率多少
CREATE TABLE audit_log (
    audit_id    INTEGER PRIMARY KEY,
    check_kind  TEXT NOT NULL,                               -- 哪种判定：投影检查、裁判
    item_kind   TEXT NOT NULL,                               -- 判的是什么
    item_id     TEXT NOT NULL,
    trusted     TEXT NOT NULL,                               -- 信了谁
    basis       TEXT,                                        -- 依据哪段原文
    packet_id   TEXT,
    revision    INTEGER,
    probability REAL CHECK (probability IS NULL OR probability BETWEEN 0 AND 1),  -- 概率多少
    created_at  TEXT NOT NULL
);
CREATE INDEX audit_log_by_item ON audit_log (item_kind, item_id);
