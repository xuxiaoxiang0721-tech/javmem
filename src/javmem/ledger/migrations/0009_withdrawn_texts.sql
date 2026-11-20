-- J2.11 第二遍审阅后的修复：撤回的话彻底删除以后、搬到新账本以后，别的包里一字不差引用它的行也要认得出。

-- 撤回了的话（不算引用行）的指纹：这句话去掉两头空白、结尾标点以后的 sha256。只存指纹，不存原文，
-- 原文彻底删除了也留着；收件、撤回时拿别的包里引用行的正文按同样的办法算指纹来比。
-- cause 是因为撤回哪个包才算撤回的；点名撤回的（单条）是空的
CREATE TABLE withdrawn_texts (
    key_hash     TEXT PRIMARY KEY,
    cause        TEXT,
    withdrawn_at TEXT NOT NULL
);

-- 撤回过的包号也记下彻底删除过没有：整本重建时导进来的包号，新账本里没有包登记，彻底删除、解除撤回都看这里
ALTER TABLE withdrawn_packets ADD COLUMN purged_at TEXT;
