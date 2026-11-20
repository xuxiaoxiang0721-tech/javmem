-- J4.4 第 2 到 4 段、判断 C 接上以后加的几列（0022 照第 1 段交过审阅的那一份不改，后来加的都在这里）。
--
-- 1. 暂停那天（paused_at，D153 第 3 条）：说停的那一小句里写了从哪天起停（“下周一起先停”）用那天，没写的用说这句话那天；
--    软变化判出来的暂停用判的那天。写法和精度照 valid_from（query-api 4.4 的六种精度）。只有暂停的才有：关系的是这一段
--    最后连着的暂停里最早那一处的，说法的是这一处自己的。查询接口 timeline 的 change: paused 要它。
-- 2. 软变化的候选句（soft_sentence，J4.4 第 4 段）：事实、关系的说法各加一列，这一处说法的证据里原文里新写的、有犹豫词的
--    那一句（javmem.soft.words，前三组的先挑；L3 标了暂停、含糊的，一句都没有就取第一句新写的话）。没有的是空的。写账时
--    有它、又会改到组里以前说的，就先挡住、进待确认（soft_change），判了再照结论（javmem.soft.rules.action）。
-- 3. 判断 C 拿不准的（judge_hold，主会话 11-17）：判断 C（javmem.pipeline.reljudge）第二档拿不准（llm_unsure）、回不了
--    （llm_failed）的关系，以前进隔离表，现在照常写成一处说法、记下 C 的原因，关系是待确认（manual_review），主人说留了
--    才算。别的说法是空的。
-- 4. soft_apply：写账最后一步照软变化的结论执行，上一次看的 soft.db 是什么样子（几行结论、最后一行建的时间）：没变就不再
--    出一遍题找结论（出题要读整本账）。只有一行。
--
-- 以前写的说法这几列都是空的，照旧；只加列、加表，不扫表。
ALTER TABLE relations ADD COLUMN paused_at TEXT;
ALTER TABLE relations ADD COLUMN paused_at_precision TEXT NOT NULL DEFAULT 'unknown'
    CHECK (paused_at_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown'));
ALTER TABLE relation_claims ADD COLUMN paused_at TEXT;
ALTER TABLE relation_claims ADD COLUMN paused_at_precision TEXT NOT NULL DEFAULT 'unknown'
    CHECK (paused_at_precision IN ('minute', 'day', 'month', 'quarter', 'year', 'unknown'));

ALTER TABLE relation_claims ADD COLUMN soft_sentence TEXT;
ALTER TABLE relation_claims ADD COLUMN judge_hold TEXT;
ALTER TABLE fact_claims ADD COLUMN soft_sentence TEXT;

CREATE TABLE soft_apply (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    signature  TEXT NOT NULL,                                    -- soft.db 的样子：提示词版本、模型、几行结论、最后一行建的时间
    applied_at TEXT NOT NULL
);
