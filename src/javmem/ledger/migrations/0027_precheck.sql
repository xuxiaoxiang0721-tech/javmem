-- J5.3 进图前检查（javmem.pipeline.precheck，D178、D22）：只加列，以前的数据一行不动、不扫表。没开进图前检查
-- （JAVMEM_PRECHECK）写的账这几列都是空的，照旧（冻结的事实、关系、主人在待确认里说留下的算主人认的，照数据算，开不开都一样）。
--
-- 1. fact_claims.judge_hold：进图前检查转人工的事实说法（照 relation_claims.judge_hold，0023）：第二档拿不准（llm_unsure）、
--    答不上（llm_failed），luna 线财务投资的本机没答上来（local_no_answer）。照常写成一处说法，不参加新旧；只有这种说法的
--    事实是待确认（manual_review），主人在 javmem review 定。别的说法是空的。
-- 2. fact_claims.judged_by、relation_claims.judged_by：这一处说法是谁认的：local 本机那一档，llm 第二档（抽取模型）。开了进图前
--    检查写账时记；转人工的、没开时写的是空的。
-- 3. facts.judged_by、relations.judged_by：这一条是谁认的，照它的说法算（javmem.pipeline.write）：冻结的、主人在待确认里说留下的
--    是 owner；别的取说法里排在前面的（llm 比 local 靠前）；有一处说法没认过就是空的。进图的口子（precheck.PrecheckGate）、
--    J5.5 的账本视图只认不是空的。
ALTER TABLE fact_claims ADD COLUMN judge_hold TEXT;
ALTER TABLE fact_claims ADD COLUMN judged_by TEXT CHECK (judged_by IS NULL OR judged_by IN ('local', 'llm'));
ALTER TABLE relation_claims ADD COLUMN judged_by TEXT CHECK (judged_by IS NULL OR judged_by IN ('local', 'llm'));
ALTER TABLE facts ADD COLUMN judged_by TEXT CHECK (judged_by IS NULL OR judged_by IN ('local', 'llm', 'owner'));
ALTER TABLE relations ADD COLUMN judged_by TEXT CHECK (judged_by IS NULL OR judged_by IN ('local', 'llm', 'owner'));
