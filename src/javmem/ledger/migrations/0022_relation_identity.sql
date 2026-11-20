-- J4.4：L5 把关系写进账本（javmem.pipeline.write，算法在 javmem.pipeline.merge 的“关系”一节）。
--
-- 关系、关系的说法各加一列“同一条关系的键”（identity_key，不分段：可见范围、主体、关系、客体，亲属还有具体说法，
-- javmem.pipeline.merge.relation_identity）。同一条关系再说一次只加证据；一条关系中间写了结束、后来又说起，是两段、
-- 两行 relations（D115、卡 3 的 D101），几段之间、新写进来的说法要并到哪几段，都按这一列找。
--
-- J4.4 以前没有关系写进来（两张表是空的），不用补；只加列、加索引，不扫表。
ALTER TABLE relations ADD COLUMN identity_key TEXT;
ALTER TABLE relation_claims ADD COLUMN identity_key TEXT;
CREATE INDEX relations_by_identity ON relations (identity_key) WHERE identity_key IS NOT NULL;
CREATE INDEX relation_claims_by_identity ON relation_claims (identity_key);
