-- J3.6（D13、D92、D100）：Bot 是第七类实体。实体的类型检查加上 Bot。
--
-- SQLite 改不了 CHECK，照它自己的办法重建这张表：建新表、搬数据、删旧表、改名，索引重建。别的表按名字引用 entities，
-- 改完名照旧对得上。这个文件跑的时候外键是关着的，跑完在同一个事务里查一遍外键，不对就整个撤销（javmem.ledger.migrate）。
CREATE TABLE entities_new (
    entity_id      TEXT PRIMARY KEY,                         -- 编号，E 开头
    canonical_name TEXT NOT NULL,                            -- 标准名
    type           TEXT NOT NULL
                   CHECK (type IN ('人', '公司或机构', '项目', '订单或合同', '地点', '资产', 'Bot')),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    name_key       TEXT                                      -- 对齐用的写法（NFKC、不分大小写、去掉空白）
);
INSERT INTO entities_new (entity_id, canonical_name, type, created_at, updated_at, name_key)
    SELECT entity_id, canonical_name, type, created_at, updated_at, name_key FROM entities;
DROP TABLE entities;
ALTER TABLE entities_new RENAME TO entities;
CREATE INDEX entities_by_name ON entities (canonical_name COLLATE NOCASE);
CREATE UNIQUE INDEX entities_by_type_and_key ON entities (type, name_key) WHERE name_key IS NOT NULL;
