-- J4.1：账本加关系表和关系证据表（设计稿 01 p3–4 的“关系”“证据”，04 p4 的 L6），规则表、留痕表加新字段，
-- 事实加“说法出自谁”。
--
-- 关系照事实的样子写（见 0004）：写账的单位是一处说法（relation_claims）：L3 在某个窗口抽出、过了 L4 的一条关系。
-- 关系（relations）是同一可见范围里同一条关系、同一段的几处说法合起来的（D115，和打分的合法一样：两头和关系一样，
-- 亲属还要具体说法一样；开始日子取有日子的里面最早的，同一天的取最准的；中间写了结束、后来又接回的另起一段，D101），
-- 关系的每一列、证据都从它的说法算出来，所以同一个窗口重写一遍、几个窗口换个先后写，结果都一样。
-- 这一版只建表：关系的核对（J4.3）、合并和判新旧（J4.4）还没写，现在不会有关系写进来。
--
-- 说话人有两个意思（J3.9 报告第 4 节第 8 条），分两列放：
--   speaker、speaker_role  证据那一句是谁说的，取最早那一句（和事实原来的 speaker 一样；和查询接口 4.5 每条出处的
--                          speaker 是一个意思，可这一列只是最早那一句的，每条出处的说话人按它那一句取）
--   attributed_to          这条说法出自谁，就是 L3 写的 speaker：主人说的（包括 Bot 转述、复述的主人的话）写主人，
--                          Bot 自己的判断写它的名字，转述的外部内容写原出处。模型写的，只当参考
-- 事实和事实的说法也加上 attributed_to（L6 照 L3 的写）。以前写的是空的，整本重建以后才有。
--
-- 只加表、加列，旧表的数据都不动。加的列都可以空或者有默认值，旧账本升到这一版不用扫表（留痕表那一列带检查，
-- 要把留痕表扫一遍，它现在是空的）。


-- 1. 关系：主体、关系、客体、生效和失效时间及精度、状态、被谁替代、确定程度、说话人、暂停条件、抽取模型和提示词版本、
--    规则版本（01 p3），定义卡上另加的字段（D118），别的照事实
--
-- 不同可见范围的关系不合并（D30），所以每条关系都带自己的可见范围。
-- 关系名不加取值检查：关系清单是配置的（query-api 5.4），现在是定义卡的六种（D13：持有、托管于、负责、任职于、客户是、
-- 亲属），以后加了不用重建表。
CREATE TABLE relations (
    relation_id          TEXT PRIMARY KEY,                       -- 编号，R 开头（query-api 4.2）
    subject_entity_id    TEXT NOT NULL REFERENCES entities (entity_id),  -- 主体
    relation_type        TEXT NOT NULL,                          -- 关系：关系清单里的名字，比如“负责”
    object_entity_id     TEXT NOT NULL REFERENCES entities (entity_id),  -- 客体
    -- 定义卡上另加的字段（D118，定义卡允许表第三节）：哪种关系用哪几个照卡，用不着的是空的
    kinship              TEXT,                                   -- 亲属：主体是客体的什么人（一头是主人时主人当客体）
    account              TEXT,                                   -- 托管于：放在哪个账户
    role                 TEXT,                                   -- 负责：什么角色（客户经理、对接人……）
    org_entity_id        TEXT REFERENCES entities (entity_id),   -- 负责：代表哪一家（主人这边和 Bot 是主人）
    position             TEXT,                                   -- 任职于：职位
    department           TEXT,                                   -- 任职于：部门
    service              TEXT,                                   -- 客户是：哪项服务
    valid_from           TEXT,                                   -- 生效时间，写到 valid_from_precision 那一级；不知道是空的
    valid_to             TEXT,                                   -- 失效时间，含 from 不含 to；还有效、不知道是空的
    -- 精度，开始、结束各一个：两个日子常常来自不同的说法。和 query-api 4.4 一样六种
    valid_from_precision TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (valid_from_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown')),
    valid_to_precision   TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (valid_to_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown')),
    -- 日子是怎么来的（定义卡卡 1“日子”，D90、D92）：空的是原文给的那个日子（持有用交割日，单子用起息日、到期或敲出那天）；
    -- trade_date 按成交日（只知道成交日，单子只知道下单那天；交割日、起息日来了再改）；
    -- approx 约（两个都不知道，用说这句话那天或结单日）
    valid_from_basis     TEXT CHECK (valid_from_basis IS NULL OR valid_from_basis IN ('trade_date', 'approx')),
    valid_to_basis       TEXT CHECK (valid_to_basis IS NULL OR valid_to_basis IN ('trade_date', 'approx')),
    state                TEXT NOT NULL                           -- 状态（query-api 4.2）：active 生效、paused 暂停、ended 已结束
                         CHECK (state IN ('active', 'paused', 'ended')),
    pause_condition      TEXT,                                   -- 暂停条件：等什么；不是暂停的是空的
    superseded_by        TEXT REFERENCES relations (relation_id),  -- 被谁替代：“同一时间一个”的，新的来了旧的失效
    superseded_at        TEXT,                                   -- 替代它的那条的 as_of
    status               TEXT NOT NULL CHECK (status IN ('verified', 'inferred', 'pending')),  -- 确定程度
    pending_reason       TEXT CHECK (pending_reason IN ('ambiguous_entity', 'frozen_conflict', 'soft_change',
                                                        'witness_mismatch', 'manual_review', 'other')),
    speaker              TEXT,                                   -- 说话人：证据那一句是谁说的
    speaker_role         TEXT CHECK (speaker_role IN ('owner', 'bot', 'other')),
    attributed_to        TEXT,                                   -- 这条说法出自谁（L3 的 speaker）
    frozen               INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1)),  -- 冻结标记（D12：主人用管理命令标）
    scope_partitions     TEXT NOT NULL,                          -- 可见范围：分区，JSON 列表
    scope_sensitive      TEXT NOT NULL DEFAULT '[]',             -- 可见范围：敏感标记，JSON 列表
    merge_key            TEXT,                                   -- 同一条关系、同一段的键（sha256）：亲属带具体说法，接回的另起一段（J4.4 定怎么算）
    group_key            TEXT,                                   -- 判新旧的组（sha256）：“同一时间一个”的才有，托管于按主体、负责按客体加 org
    as_of                TEXT,                                   -- 最后一次有人说这条关系：证据里最晚的说话时间
    as_of_epoch          INTEGER,
    notes                TEXT NOT NULL DEFAULT '[]',             -- L3、L4 的记号，JSON 列表
    extracted_by         TEXT,                                   -- 抽取模型和提示词版本
    rule_version         TEXT,                                   -- 规则版本（L4、L5）
    -- 来源和信任（J2.13），和事实一样按证据算
    sources              TEXT NOT NULL DEFAULT '[]',             -- 证据来自哪些来源，JSON 列表
    origins              TEXT NOT NULL DEFAULT '[]',             -- 证据有哪几种 origin，JSON 列表
    secondhand           INTEGER NOT NULL DEFAULT 0 CHECK (secondhand IN (0, 1)),  -- 证据全都不是说话人自己的话：当转述
    stated               INTEGER NOT NULL DEFAULT 0 CHECK (stated IN (0, 1)),      -- 原话直接说的（有一处说法 verified）
    owner_said           INTEGER NOT NULL DEFAULT 0 CHECK (owner_said IN (0, 1)),  -- 证据里有主人自己的话
    withdrawn_at         TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (state = 'paused' OR pause_condition IS NULL)
);
CREATE UNIQUE INDEX relations_by_merge_key ON relations (merge_key) WHERE merge_key IS NOT NULL;
CREATE INDEX relations_by_group ON relations (group_key) WHERE group_key IS NOT NULL;
CREATE INDEX relations_by_subject ON relations (subject_entity_id, relation_type);
CREATE INDEX relations_by_object ON relations (object_entity_id, relation_type);  -- 某个时间点某种关系是谁（relation_at）
-- 外键的另一头删行时（删实体、删关系）要按这两列找，不建索引就整张扫
CREATE INDEX relations_by_org ON relations (org_entity_id) WHERE org_entity_id IS NOT NULL;
CREATE INDEX relations_by_superseded ON relations (superseded_by) WHERE superseded_by IS NOT NULL;


-- 2. 关系的一处说法：L3 在某个窗口抽出、过了 L4 的一条关系。关系是说法合起来的（照 fact_claims）
CREATE TABLE relation_claims (
    claim_id             TEXT PRIMARY KEY,                       -- 窗口编号#结果库键的前 12 位#第几条（L3 结果里关系的顺序，从 0 数）
    relation_id          TEXT NOT NULL REFERENCES relations (relation_id) ON DELETE CASCADE,
    window_id            TEXT NOT NULL,
    cache_key            TEXT NOT NULL,                          -- L3 结果库的键
    extracted_by         TEXT NOT NULL,                          -- 提示词版本和模型
    l4_version           TEXT NOT NULL,                          -- 哪一版 L4 核的（核的方式）
    subject_entity_id    TEXT NOT NULL REFERENCES entities (entity_id),
    relation_type        TEXT NOT NULL,
    object_entity_id     TEXT NOT NULL REFERENCES entities (entity_id),
    kinship              TEXT,
    account              TEXT,
    role                 TEXT,
    org_entity_id        TEXT REFERENCES entities (entity_id),
    position             TEXT,
    department           TEXT,
    service              TEXT,
    state                TEXT NOT NULL CHECK (state IN ('active', 'paused', 'ended')),  -- 这一处说的状态
    pause_condition      TEXT,
    valid_from           TEXT,                                   -- L4 核过的
    valid_to             TEXT,
    valid_from_precision TEXT NOT NULL
                         CHECK (valid_from_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown')),
    valid_to_precision   TEXT NOT NULL
                         CHECK (valid_to_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown')),
    valid_from_basis     TEXT CHECK (valid_from_basis IS NULL OR valid_from_basis IN ('trade_date', 'approx')),
    valid_to_basis       TEXT CHECK (valid_to_basis IS NULL OR valid_to_basis IN ('trade_date', 'approx')),
    certainty            TEXT NOT NULL CHECK (certainty IN ('stated', 'inferred', 'vague')),  -- L3 写的确定程度
    status               TEXT NOT NULL CHECK (status IN ('verified', 'inferred')),
    speaker              TEXT NOT NULL,                          -- 这一处最早那条证据的说话人
    speaker_role         TEXT NOT NULL CHECK (speaker_role IN ('owner', 'bot', 'other')),
    attributed_to        TEXT,                                   -- 这条说法出自谁（L3 的 speaker）
    said_from            TEXT NOT NULL,                          -- 这一处证据里最早、最晚的说话时间
    said_from_epoch      INTEGER NOT NULL,
    said_to              TEXT NOT NULL,
    said_to_epoch        INTEGER NOT NULL,
    proofs               TEXT NOT NULL,                          -- 过了核对的证据，JSON 列表：sentence_id、packet_id、revision、quote……
    notes                TEXT NOT NULL DEFAULT '[]',             -- L3、L4 的记号
    created_at           TEXT NOT NULL,
    CHECK (state = 'paused' OR pause_condition IS NULL)
);
CREATE INDEX relation_claims_by_relation ON relation_claims (relation_id);
CREATE INDEX relation_claims_by_window ON relation_claims (window_id);
CREATE INDEX relation_claims_by_subject ON relation_claims (subject_entity_id);
CREATE INDEX relation_claims_by_object ON relation_claims (object_entity_id);
CREATE INDEX relation_claims_by_org ON relation_claims (org_entity_id) WHERE org_entity_id IS NOT NULL;


-- 3. 关系证据：每条关系出自哪个包、哪句原话、在什么位置（04 p4 的关系证据表；01 p4 的“证据”里关系的那一半）
--
-- 照事实的证据（evidence）：说话人、时间这些按 sentence_id 从原文索引取；来源、origin、说话人的身份、指纹、哪一封信
-- 写的时候抄下来（J2.11、J2.13、D73 第 17 条），替换、彻底删除以后也认得出。
CREATE TABLE relation_evidence (
    evidence_id   INTEGER PRIMARY KEY,
    relation_id   TEXT    NOT NULL REFERENCES relations (relation_id) ON DELETE CASCADE,  -- 哪条关系
    packet_id     TEXT    NOT NULL,                              -- 哪个包
    revision      INTEGER NOT NULL,
    sentence_id   TEXT    NOT NULL,                              -- 哪一句
    quote         TEXT    NOT NULL,                              -- 哪句原话，逐字
    position      TEXT    NOT NULL,                              -- 在什么位置，JSON，写法和 query-api 4.5 出处里那一项一样
    window_id     TEXT,                                          -- 哪个窗口写的
    source        TEXT,                                          -- 来源编号（哪个 Bot、哪个邮箱）
    origin        TEXT CHECK (origin IS NULL OR origin IN ('said', 'relayed', 'external', 'attachment_note')),
    speaker_role  TEXT CHECK (speaker_role IS NULL OR speaker_role IN ('owner', 'bot', 'other')),
    sentence_hash TEXT,                                          -- 说话人加原句的指纹，认撤回用
    message_id    TEXT,                                          -- 邮件才有：哪一封信
    created_at    TEXT    NOT NULL,
    FOREIGN KEY (packet_id, revision) REFERENCES packets (packet_id, revision),
    UNIQUE (relation_id, sentence_id)                            -- 一条关系一句只记一次
);
CREATE INDEX relation_evidence_by_packet ON relation_evidence (packet_id, revision);
CREATE INDEX relation_evidence_by_window ON relation_evidence (window_id) WHERE window_id IS NOT NULL;
CREATE INDEX relation_evidence_by_sentence ON relation_evidence (sentence_id);


-- 4. 事实也分开放：原来的 speaker 是证据那一句是谁说的，这里加上 L3 写的“这条说法出自谁”
ALTER TABLE fact_claims ADD COLUMN attributed_to TEXT;              -- 这一处 L3 写的 speaker；旧样子的结果没有，是空的
ALTER TABLE facts ADD COLUMN attributed_to TEXT;                    -- 取代表这条事实的那一处说法的（和 speaker 一样，最早说的那一处）


-- 5. 规则：04 p8“每条规则记下：规则本身、落在哪一层、来自哪几道题和哪份数据、正反例原句、谁定的”。
-- 01 p4 列了前三样，第一版就有（description、layer、from_questions）；这里补后三样，已登记的规则这三列是空的
ALTER TABLE rules ADD COLUMN from_data TEXT NOT NULL DEFAULT '[]';  -- 来自哪份数据，JSON 列表，比如记忆包、业务员邮箱、公开数据
-- 正反例原句，JSON 列表：每一项 {"kind": "positive" 或 "negative", "sentence_id", "packet_id", "revision", "sentence_hash"}
-- （和证据一样记版本和指纹：句子编号只是位置，包重发以后可能指到另一句），别处的例句写 "ref"（出处）。
-- 只记编号和指纹，不抄原句：原文撤回、彻底删除时不用另清
ALTER TABLE rules ADD COLUMN examples TEXT NOT NULL DEFAULT '[]';
ALTER TABLE rules ADD COLUMN decided_by TEXT;                       -- 谁定的：主人、CC、几位同事答得一致……


-- 6. 留痕：02 p6“每次判决记下信了谁、哪一级判的、依据哪段原文、概率多少”。01 p4 少了“哪一级判的”，这里补上，
-- 另记是哪条规则、哪个模型判的（02 p6：判的模型要锁版本，换了版本要重跑回归集）。
-- 一级一级照 02 p5：规则判得了的不调模型，判断模型判不了的才让大模型深读原文，财务投资、冻结的人工定
ALTER TABLE audit_log ADD COLUMN tier TEXT                          -- 哪一级判的：rule 规则、judge 判断模型（只答是非、几选一的）、
    CHECK (tier IS NULL OR tier IN ('rule', 'judge', 'model', 'owner'));  -- model 大模型读原文、owner 主人
ALTER TABLE audit_log ADD COLUMN judge TEXT;                        -- 哪条规则（编号和版本），或者哪个模型（名字@档位）、哪一版提示词
