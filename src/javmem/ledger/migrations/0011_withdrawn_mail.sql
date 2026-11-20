-- D73 第 17 条、D79 第 10 条：邮件包的单条撤回记录带上是哪一封信，只挡同一封信里一字不差的话。
--
-- 原来的记录是“这个包、这个人、这句话”：一个邮箱包里同一个人的几十封信，发件人、收件人、附件名那一行，称呼、署名
-- 都一字不差，撤一封，别的信的这几行也跟着标上，附件名那一行跟着标上的，那封信的附件也撤了。
--
-- message_id：邮件的句子（邮件头、正文、附件的正文）是那封信的 Message-ID；对话包是空字符串。空字符串的记录照旧挡
-- 整个包里同一个人一字不差的话：对话包本来就这样，这一版以前记下的邮件包的记录也这样（不知道当时点的是哪一封，
-- 宁可多挡）。SQLite 改不了主键，建一张新表把旧记录搬过去。
CREATE TABLE withdrawn_sentences_v11 (
    packet_id     TEXT NOT NULL,
    message_id    TEXT NOT NULL DEFAULT '',                  -- 邮件是那封信的 Message-ID，对话包和以前的记录是空的
    sentence_hash TEXT NOT NULL,                             -- 说话人加原句的 sha256
    withdrawn_at  TEXT NOT NULL,
    cause         TEXT,
    PRIMARY KEY (packet_id, message_id, sentence_hash)
);
INSERT INTO withdrawn_sentences_v11 (packet_id, message_id, sentence_hash, withdrawn_at, cause)
    SELECT packet_id, '', sentence_hash, withdrawn_at, cause FROM withdrawn_sentences;
DROP TABLE withdrawn_sentences;
ALTER TABLE withdrawn_sentences_v11 RENAME TO withdrawn_sentences;

-- 证据那一句是哪封信的（邮件才有）：冻结事实的证据按指纹认撤回时，也只认同一封信的记录。
-- 已经写了的按原文索引补；查不到的（替换前旧版本的句子）留空，照旧按整个包认
ALTER TABLE evidence ADD COLUMN message_id TEXT;
UPDATE evidence SET message_id = (
    SELECT s.message_id FROM raw_sentences AS s WHERE s.sentence_id = evidence.sentence_id AND s.revision = evidence.revision
);

-- D73 第 16 条：按事实撤回了发件人那一句、信里别的句子还在的信，发件人写成“[已撤回]”。那以前存进 L3 结果库的窗口，
-- 抬头里还写着发件人（长信后面几段的窗口清单里没有发件人那一句，按撤回了的句子认不出来），这封信的窗口都要删。
-- 撤回的那个事务里记一行，结果库删完了记 store_cleaned_at；撤回中途断了，再跑一次接着删。只有编号，没有原文
CREATE TABLE masked_mails (
    packet_id        TEXT    NOT NULL,
    revision         INTEGER NOT NULL,
    message_id       TEXT    NOT NULL,
    masked_at        TEXT    NOT NULL,
    store_cleaned_at TEXT,
    PRIMARY KEY (packet_id, revision, message_id)
);
