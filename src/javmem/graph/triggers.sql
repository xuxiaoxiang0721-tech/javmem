-- 主图同步（J5.2）记欠账的触发器。不在账本的迁移里：javmem graph init 接上图的时候才装（javmem.graph.dirty.install_triggers），
-- init --reset 拆掉。没接上图的账本（金标准重建、测试）一个都没有，写账一点不受影响。
-- 改了这里要把 javmem.graph.dirty.TRIGGERS_VERSION 加一：同步时认出装着的是旧的，换成新的、整张同步一遍。
--
-- 下面这些表有新增、改动、删除，就在 graph_dirty 记一笔：同一条已经欠着的，只把它改成这一轮
-- （INSERT … SELECT 带着 WHERE，SQLite 才认得出后面的 ON CONFLICT）。

-- 实体
CREATE TRIGGER graph_mark_entity_insert AFTER INSERT ON entities
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', NEW.entity_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_entity_update AFTER UPDATE ON entities
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', NEW.entity_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', OLD.entity_id, gen FROM graph_state
        WHERE id = 1 AND OLD.entity_id IS NOT NEW.entity_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_entity_delete AFTER DELETE ON entities
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', OLD.entity_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 别名
CREATE TRIGGER graph_mark_alias_insert AFTER INSERT ON aliases
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', NEW.alias_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_alias_update AFTER UPDATE ON aliases
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', NEW.alias_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', OLD.alias_id, gen FROM graph_state
        WHERE id = 1 AND OLD.alias_id IS NOT NEW.alias_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_alias_delete AFTER DELETE ON aliases
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', OLD.alias_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 证据：只看别名的出处（别名的可见范围从出处所在的包来）。事实的证据不进图
CREATE TRIGGER graph_mark_evidence_insert AFTER INSERT ON evidence
WHEN NEW.alias_id IS NOT NULL BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', NEW.alias_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_evidence_update AFTER UPDATE ON evidence
WHEN (NEW.alias_id IS NOT NULL OR OLD.alias_id IS NOT NULL) BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', NEW.alias_id, gen FROM graph_state
        WHERE id = 1 AND NEW.alias_id IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', OLD.alias_id, gen FROM graph_state
        WHERE id = 1 AND OLD.alias_id IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_evidence_delete AFTER DELETE ON evidence
WHEN OLD.alias_id IS NOT NULL BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'alias', OLD.alias_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 事实：被替代的两头都记。这一条有什么改动（状态、撤回……），替代它的那条“替代了谁”都可能跟着变，所以不管
-- superseded_by 变没变，有就记（审阅第 6 条）
CREATE TRIGGER graph_mark_fact_insert AFTER INSERT ON facts
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.superseded_by, gen FROM graph_state
        WHERE id = 1 AND NEW.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_update AFTER UPDATE ON facts
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state
        WHERE id = 1 AND OLD.fact_id IS NOT NEW.fact_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.superseded_by, gen FROM graph_state
        WHERE id = 1 AND OLD.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.superseded_by, gen FROM graph_state
        WHERE id = 1 AND NEW.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_delete AFTER DELETE ON facts
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.superseded_by, gen FROM graph_state
        WHERE id = 1 AND OLD.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 事实和实体、事实的说法（最早有人说的时刻）、事实的向量：都记成这条事实欠着
CREATE TRIGGER graph_mark_fact_entity_insert AFTER INSERT ON fact_entities
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_entity_update AFTER UPDATE ON fact_entities
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state
        WHERE id = 1 AND OLD.fact_id IS NOT NEW.fact_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_entity_delete AFTER DELETE ON fact_entities
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_claim_insert AFTER INSERT ON fact_claims
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_claim_update AFTER UPDATE ON fact_claims
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state
        WHERE id = 1 AND OLD.fact_id IS NOT NEW.fact_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_claim_delete AFTER DELETE ON fact_claims
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_vector_insert AFTER INSERT ON fact_vectors
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_vector_update AFTER UPDATE ON fact_vectors
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', NEW.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state
        WHERE id = 1 AND OLD.fact_id IS NOT NEW.fact_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_fact_vector_delete AFTER DELETE ON fact_vectors
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'fact', OLD.fact_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 关系：被替代的两头都记（同事实）
CREATE TRIGGER graph_mark_relation_insert AFTER INSERT ON relations
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', NEW.relation_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', NEW.superseded_by, gen FROM graph_state
        WHERE id = 1 AND NEW.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_relation_update AFTER UPDATE ON relations
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', NEW.relation_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', OLD.relation_id, gen FROM graph_state
        WHERE id = 1 AND OLD.relation_id IS NOT NEW.relation_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', OLD.superseded_by, gen FROM graph_state
        WHERE id = 1 AND OLD.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', NEW.superseded_by, gen FROM graph_state
        WHERE id = 1 AND NEW.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_relation_delete AFTER DELETE ON relations
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', OLD.relation_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', OLD.superseded_by, gen FROM graph_state
        WHERE id = 1 AND OLD.superseded_by IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_relation_claim_insert AFTER INSERT ON relation_claims
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', NEW.relation_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_relation_claim_update AFTER UPDATE ON relation_claims
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', NEW.relation_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', OLD.relation_id, gen FROM graph_state
        WHERE id = 1 AND OLD.relation_id IS NOT NEW.relation_id
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_relation_claim_delete AFTER DELETE ON relation_claims
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'relation', OLD.relation_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 合并跳转：新旧两个实体都记（新的那个记着并进来的旧编号）
CREATE TRIGGER graph_mark_redirect_insert AFTER INSERT ON entity_redirects
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', NEW.new_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', NEW.old_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_redirect_update AFTER UPDATE ON entity_redirects
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', NEW.new_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', OLD.new_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', NEW.old_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', OLD.old_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_redirect_delete AFTER DELETE ON entity_redirects
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', OLD.new_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', OLD.old_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 待确认里的“实体有歧义”：开着的那个范围里，这个实体的别名先不算（待确认的不放），开了、定了都要重算
CREATE TRIGGER graph_mark_ambiguity_insert AFTER INSERT ON review_queue
WHEN NEW.kind = 'ambiguous_entity' BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', CASE WHEN json_valid(NEW.claims) THEN json_extract(NEW.claims, '$.entity_id') END, gen FROM graph_state
        WHERE id = 1 AND CASE WHEN json_valid(NEW.claims) THEN json_extract(NEW.claims, '$.entity_id') END IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_ambiguity_update AFTER UPDATE ON review_queue
WHEN (NEW.kind = 'ambiguous_entity' OR OLD.kind = 'ambiguous_entity') BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', CASE WHEN json_valid(NEW.claims) THEN json_extract(NEW.claims, '$.entity_id') END, gen FROM graph_state
        WHERE id = 1 AND NEW.kind = 'ambiguous_entity' AND CASE WHEN json_valid(NEW.claims) THEN json_extract(NEW.claims, '$.entity_id') END IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', CASE WHEN json_valid(OLD.claims) THEN json_extract(OLD.claims, '$.entity_id') END, gen FROM graph_state
        WHERE id = 1 AND OLD.kind = 'ambiguous_entity' AND CASE WHEN json_valid(OLD.claims) THEN json_extract(OLD.claims, '$.entity_id') END IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_ambiguity_delete AFTER DELETE ON review_queue
WHEN OLD.kind = 'ambiguous_entity' BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'entity', CASE WHEN json_valid(OLD.claims) THEN json_extract(OLD.claims, '$.entity_id') END, gen FROM graph_state
        WHERE id = 1 AND CASE WHEN json_valid(OLD.claims) THEN json_extract(OLD.claims, '$.entity_id') END IS NOT NULL
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;

-- 包登记：换了版本、撤回、彻底删除，出处在它里面的别名要重算
CREATE TRIGGER graph_mark_packet_update AFTER UPDATE OF state, withdrawn_at, purged_at ON packets
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'packet', NEW.packet_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
CREATE TRIGGER graph_mark_packet_delete AFTER DELETE ON packets
BEGIN
    INSERT INTO graph_dirty (kind, item_id, gen) SELECT 'packet', OLD.packet_id, gen FROM graph_state WHERE id = 1
        ON CONFLICT (kind, item_id) DO UPDATE SET gen = excluded.gen WHERE excluded.gen > graph_dirty.gen;
END;
