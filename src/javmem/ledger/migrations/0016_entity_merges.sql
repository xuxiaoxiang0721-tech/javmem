-- J3.6（D100）：合并工具在这本账上合过、拆过的，和旧编号跳到新编号。
--
-- 合并记录本身在账本外面（别名表旁边的 lexicon.merges.jsonl），整本重建也丢不了；这两张表记的是“这本账上已经生效的”，
-- 给跳转、--history 用，整本重建以后 javmem entities --sync 按合并记录重新算出来。
CREATE TABLE entity_merges (
    merge_id   TEXT PRIMARY KEY,                             -- 合并记录的编号，M 开头
    kind       TEXT NOT NULL CHECK (kind IN ('merge', 'apart')),
    new_id     TEXT,                                         -- 合并以后的实体（apart 是空的）
    old_ids    TEXT NOT NULL DEFAULT '[]',                   -- 并进去的旧编号，JSON 列表
    applied_at TEXT NOT NULL,
    undone_at  TEXT                                          -- 撤销以后生效的时间
);

-- 旧编号跳到新编号（query-api 第 5 节 entity_id）：调用方手里的旧编号，照合并以后的实体答
CREATE TABLE entity_redirects (
    old_id   TEXT PRIMARY KEY,
    new_id   TEXT NOT NULL,
    merge_id TEXT NOT NULL
);
CREATE INDEX entity_redirects_by_new ON entity_redirects (new_id);
