-- J2.9：L5 合并和替代、L6 写账、向量。
--
-- 写账的单位是“一处说法”（fact_claims）：L3 在某个窗口里抽出、过了 L4 的一条事实。
-- 事实（facts）是说法按“同一事实的键”合起来的，事实的每一列、证据、事实和实体都从它的说法算出来，
-- 所以同一个窗口重写一遍、几个窗口换个先后写，结果都一样；撤回（J2.11）删掉说法再重算就行。


-- 事实：同一事实的键、判新旧的组、新旧、谁替代了它、核对和抽取的版本
ALTER TABLE facts ADD COLUMN merge_key TEXT;                          -- 同一事实的键（sha256）：可见范围加主体、属性、值，没有属性或值的用事实文字
ALTER TABLE facts ADD COLUMN group_key TEXT;                          -- 判新旧的组（sha256）：可见范围加主体、属性。没有主体、属性，或者属性是多值的，是空的
ALTER TABLE facts ADD COLUMN as_of TEXT;                              -- 最后一次有人说这件事：证据里最晚的说话时间
ALTER TABLE facts ADD COLUMN as_of_epoch INTEGER;
ALTER TABLE facts ADD COLUMN superseded_by TEXT REFERENCES facts (fact_id);  -- 被哪条替代了，现行的是空的
ALTER TABLE facts ADD COLUMN superseded_at TEXT;                      -- 替代它的那条的 as_of
ALTER TABLE facts ADD COLUMN notes TEXT NOT NULL DEFAULT '[]';        -- L4 的记号，JSON 列表，比如 G6:time_elsewhere
ALTER TABLE facts ADD COLUMN extracted_by TEXT;                       -- 提示词版本和模型，比如 l3-v1 某模型@auto/json_schema
ALTER TABLE facts ADD COLUMN rule_version TEXT;                       -- L4、L5 的规则版本，比如 l4-v1 l5-v1
CREATE UNIQUE INDEX facts_by_merge_key ON facts (merge_key) WHERE merge_key IS NOT NULL;
CREATE INDEX facts_by_group ON facts (group_key) WHERE group_key IS NOT NULL;
CREATE INDEX facts_current ON facts (subject_entity_id) WHERE superseded_by IS NULL AND withdrawn_at IS NULL;


-- 一处说法：L3 在某个窗口抽出、过了 L4 的一条事实。事实是说法合起来的
CREATE TABLE fact_claims (
    claim_id       TEXT PRIMARY KEY,                                 -- 窗口编号#第几条（从 0 数，L3 结果里的顺序）
    fact_id        TEXT NOT NULL REFERENCES facts (fact_id) ON DELETE CASCADE,
    window_id      TEXT NOT NULL,
    cache_key      TEXT NOT NULL,                                    -- L3 结果库的键
    extracted_by   TEXT NOT NULL,                                    -- 提示词版本和模型
    l4_version     TEXT NOT NULL,                                    -- 哪一版 L4 核的
    content        TEXT NOT NULL,                                    -- 这一处的事实文字
    subject_entity_id TEXT REFERENCES entities (entity_id),
    attribute      TEXT,
    value          TEXT,
    occurred_at    TEXT,                                             -- L4 核过的（降过精度、去掉了对不上的）
    time_precision TEXT NOT NULL,
    certainty      TEXT NOT NULL CHECK (certainty IN ('stated', 'inferred', 'vague')),
    status         TEXT NOT NULL CHECK (status IN ('verified', 'inferred')),
    speaker        TEXT NOT NULL,                                    -- 这一处最早那条证据的说话人
    speaker_role   TEXT NOT NULL CHECK (speaker_role IN ('owner', 'bot', 'other')),
    said_from      TEXT NOT NULL,                                    -- 这一处证据里最早、最晚的说话时间
    said_from_epoch INTEGER NOT NULL,
    said_to        TEXT NOT NULL,
    said_to_epoch  INTEGER NOT NULL,
    proofs         TEXT NOT NULL,                                    -- 过了 G1 的证据，JSON 列表：sentence_id、packet_id、revision、quote
    entities       TEXT NOT NULL DEFAULT '[]',                       -- 涉及的实体和角色，JSON 列表：entity_id、role
    notes          TEXT NOT NULL DEFAULT '[]',                       -- L4 的记号
    created_at     TEXT NOT NULL
);
CREATE INDEX fact_claims_by_fact ON fact_claims (fact_id);
CREATE INDEX fact_claims_by_window ON fact_claims (window_id);


-- 证据、隔离、别名的出处：记下是哪个窗口写的，重写这个窗口时先清掉
ALTER TABLE evidence ADD COLUMN window_id TEXT;
CREATE UNIQUE INDEX evidence_one_per_fact_sentence ON evidence (fact_id, sentence_id) WHERE fact_id IS NOT NULL;
CREATE UNIQUE INDEX evidence_one_per_alias_sentence ON evidence (alias_id, sentence_id) WHERE alias_id IS NOT NULL;
CREATE INDEX evidence_by_window ON evidence (window_id) WHERE window_id IS NOT NULL;
CREATE INDEX evidence_by_sentence ON evidence (sentence_id);

ALTER TABLE quarantine ADD COLUMN window_id TEXT;
CREATE INDEX quarantine_by_window ON quarantine (window_id) WHERE window_id IS NOT NULL;


-- 实体和别名：对齐用的写法（NFKC、不分大小写、去掉空白）
ALTER TABLE entities ADD COLUMN name_key TEXT;
CREATE UNIQUE INDEX entities_by_type_and_key ON entities (type, name_key) WHERE name_key IS NOT NULL;
ALTER TABLE aliases ADD COLUMN alias_key TEXT;
CREATE INDEX aliases_by_key ON aliases (alias_key) WHERE alias_key IS NOT NULL;


-- 事实的向量：Qwen3-Embedding-4B，2560 维，定了不改。float32 小端，按事实文字算
CREATE TABLE fact_vectors (
    fact_id     TEXT PRIMARY KEY REFERENCES facts (fact_id) ON DELETE CASCADE,
    model       TEXT    NOT NULL,
    dim         INTEGER NOT NULL CHECK (dim = 2560),
    text_sha256 TEXT    NOT NULL,                                    -- 向量是哪段文字的：事实文字的 sha256
    vector      BLOB    NOT NULL CHECK (length(vector) = 4 * 2560),
    created_at  TEXT    NOT NULL
);
