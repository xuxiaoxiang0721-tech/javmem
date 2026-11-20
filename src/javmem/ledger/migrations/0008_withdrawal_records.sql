-- J2.11 审阅后的修复：撤回的记录要能搬到整本重建的新账本里，冻结事实的证据按指纹认撤回。

-- 撤回过的包号。packets.withdrawn_at 是包登记那一行上的标记；这张表只记包号，包还没收进来也能记，
-- 整本重建时先把旧账本的撤回导进来（javmem withdraw --import），收原包时这些包号照样不收
CREATE TABLE withdrawn_packets (
    packet_id    TEXT PRIMARY KEY,
    withdrawn_at TEXT NOT NULL
);
INSERT OR IGNORE INTO withdrawn_packets (packet_id, withdrawn_at)
    SELECT packet_id, min(withdrawn_at) FROM packets WHERE withdrawn_at IS NOT NULL GROUP BY packet_id;

-- 单条撤回的记录是因为什么标上的：空的是点名撤回的（连同同一个包里的复述、引用）；
-- 包号是撤回那个包的时候，别的包里引用它的行跟着标上的，解除撤回那个包时一起解除
ALTER TABLE withdrawn_sentences ADD COLUMN cause TEXT;

-- 证据那一句的指纹（说话人加原句，和 withdrawn_sentences 一样）。句子编号只是位置，换了版本可能是另一句话，
-- 冻结事实的证据还指着旧版本时，按指纹才认得出它是不是撤回了的那句
ALTER TABLE evidence ADD COLUMN sentence_hash TEXT;
