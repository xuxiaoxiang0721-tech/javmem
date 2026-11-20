-- 单条撤回的记录（packet-spec 第 10 节：替换不动单条撤回的记录，撤回的内容不会因为替换又回来）。
--
-- raw_sentences 是能重建的索引，包也能换版本，所以记录单独放，不跟着它们变。
-- 只存指纹，不存原文：彻底删除原文之后这里也没有留下那句话。辅助表，不算 01 里的账本表。
CREATE TABLE withdrawn_sentences (
    packet_id     TEXT NOT NULL,
    sentence_hash TEXT NOT NULL,                             -- 说话人加原句的 sha256
    withdrawn_at  TEXT NOT NULL,
    PRIMARY KEY (packet_id, sentence_hash)
);
