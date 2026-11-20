-- 第五轮第 50 条：只在引用里看到过的话（D101），取包时要知道账本里同一句话是原话还是引用。以前要把有这句话的包都
-- 读进来做一遍 L1，常见的话处处都有，一个包一批也要读大半本。改成写账时（javmem write，写窗口以前）记下来，查表。
--
-- copy_role、reply_to 是写账时 L1 认的，不是收件时算的；没写过账的是 NULL。quote_roles 记每个包是哪一版、
-- 哪一版 L1 认的：版本对不上（包换了版本、L1 升了版本）的不算数，取包时照旧读那个包。只有编号和标记，没有原文。
ALTER TABLE raw_sentences ADD COLUMN copy_role INTEGER;      -- 1 能当原话、2 可以当证据的引用、0 都不是（有 copy_key 的才记）
ALTER TABLE raw_sentences ADD COLUMN reply_to TEXT;          -- 可以当证据的引用：它那封信回的哪一封（In-Reply-To）
CREATE TABLE quote_roles (
    packet_id  TEXT    NOT NULL PRIMARY KEY,
    revision   INTEGER NOT NULL,                             -- 记的是哪一版
    l1_version TEXT    NOT NULL                              -- 哪一版 L1 认的
);
