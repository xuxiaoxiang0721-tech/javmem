-- J5.2：账本同步进主图（javmem.graph）。账本这边记三样：这本账接没接上图、欠着没同步的、图里现在是什么样。
--
-- 只加表，以前的数据一行不动、不扫表。记欠账的触发器不在这里：javmem graph init 接上图的时候才装
-- （src/javmem/graph/triggers.sql），没接上图的账本（金标准重建、测试）一个都没有，写账一点不受影响（SQLite 每改一行都要
-- 进一次触发器，哪怕它什么都不做）。装上以后，那几张表有新增、改动、删除，就在 graph_dirty 记一笔“欠着”，下次同步
-- （javmem write 写完账接着同步，或者 javmem graph sync）补上。
--
-- 同步时怎么不丢改动（“第几轮”）：graph_state.gen 是现在第几轮。同步一开始把它加一，只同步加一以前记的；同步的这段时间里
-- 又改了的，触发器把那一行的 gen 改成新的一轮，同步完只删旧一轮的，新改的留着下次。同一条改十次也只有一行。


-- 1. 接没接上图：一行。enabled 是 javmem graph init 放好标记、装好触发器以后才打开的；marker_id 和图里的标记一样（防串库）
CREATE TABLE graph_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    enabled    INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    marker_id  TEXT,                                         -- 图里标记的编号
    gen        INTEGER NOT NULL DEFAULT 1,                   -- 现在第几轮（触发器记欠账时写它）
    triggers   TEXT,                                         -- 装着的触发器是哪一版（javmem.graph.dirty.TRIGGERS_VERSION）
    projection TEXT,                                         -- 上次整张同步时图的样子是哪一版（javmem.graph.project）
    gate       TEXT,                                         -- 上次整张同步时进图前检查是哪一版（javmem.graph.gate）
    synced_at  TEXT,                                         -- 上次同步完
    rebuilt_at TEXT                                          -- 上次整张同步（重建）完；空的是还没有过，或者上一回整张同步没做完
);
INSERT INTO graph_state (id) VALUES (1);


-- 2. 欠着没同步的：哪一样、编号、第几轮记的。packet 是包登记变了（撤回、换了版本），同步时扩成出处在它里面的别名
CREATE TABLE graph_dirty (
    kind    TEXT    NOT NULL CHECK (kind IN ('entity', 'alias', 'fact', 'relation', 'packet')),
    item_id TEXT    NOT NULL,
    gen     INTEGER NOT NULL,
    since   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),  -- 从什么时候欠着的（UTC；同一条又改了不变）
    PRIMARY KEY (kind, item_id)
);
CREATE INDEX graph_dirty_by_gen ON graph_dirty (gen);


-- 3. 图里现在是什么样：每一样上次写进图的指纹（所有字段算一个 sha256）、向量的指纹、连着哪几个实体、可见范围的编号。
--    同步时重算出来一样的不再写；图里有、账本里不该有了的才去删。只在图那边提交了以后才改这里
CREATE TABLE graph_items (
    kind        TEXT NOT NULL CHECK (kind IN ('entity', 'alias', 'fact', 'relation', 'scope')),
    item_id     TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    vector_sha  TEXT,                                        -- 事、关系的向量的 sha256；没有向量是空的
    links       TEXT NOT NULL DEFAULT '[]',                  -- 连着哪几个实体，JSON 列表：事是涉及的，关系是两头和代表哪一家，别名是它的实体
    scopes      TEXT NOT NULL DEFAULT '[]',                  -- 可见范围的编号，JSON 列表
    PRIMARY KEY (kind, item_id)
);


-- 4. 数数：每个实体、每个可见范围，图里连着它的事、关系、别名有几条。实体在不在图里、它的可见范围，就看这里大于 0 的
--    （一个实体连着几十万条事实，每次同步不用都数一遍）
CREATE TABLE graph_entity_scopes (
    entity_id TEXT    NOT NULL,
    scope_id  TEXT    NOT NULL,
    n         INTEGER NOT NULL CHECK (n > 0),
    PRIMARY KEY (entity_id, scope_id)
);

-- 每个可见范围图里有几条事、关系、别名（JmScope 节点在不在看这里）
CREATE TABLE graph_scopes (
    scope_id   TEXT    NOT NULL PRIMARY KEY,
    partitions TEXT    NOT NULL,                             -- JSON 列表
    sensitive  TEXT    NOT NULL,                             -- JSON 列表
    n          INTEGER NOT NULL CHECK (n > 0)
);
