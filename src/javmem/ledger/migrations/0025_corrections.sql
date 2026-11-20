-- J4.6 对话里纠正关系（D7、D83；做法 J4.6-做法.md，主人 11-17 选择卡定的五题）。只加列（加上待确认表上一个小索引），不重建表、
-- 不改以前写的：以前写的都是空的，照旧。
--
-- 1. relation_claims.correction：这一处说法是主人在纠正（javmem.pipeline.correct.correction_sentence）：带纠正词的那一句的
--    编号。软变化的候选（soft_sentence 有的）不算，软变化优先。写说法时算，重写窗口、撤回那一句时跟着变。
-- 2. relations.corrected_at、correction_sentence：这一段是被主人纠正掉的（被纠正的那一处结束了、顶掉了，或者主人在待确认
--    里认了“抽取没抽出来的纠正”）：说那一句的时间、那一句的编号。从说法算，重算时跟着变；纠正撤回了就没了。
-- 3. review_queue.code、answer：D83（第 2 段）。code 是这一条待确认的短编号（javmem.pipeline.answers.code_of，按内容算，
--    整本重建也不变），Bot 问的时候写在问的那句话里，写账时补上没写的；answer 记主人在对话里答的：问话、回答各是哪一句，
--    认成什么（认不出的也记一笔），撤回时照它撤。以前的都是空的，写账时补编号。
--    review_queue_uncoded 只索引还没编号的几行（待确认表本来就小），每写一个窗口补编号时不用扫整张表。
ALTER TABLE relation_claims ADD COLUMN correction TEXT;
ALTER TABLE relations ADD COLUMN corrected_at TEXT;
ALTER TABLE relations ADD COLUMN correction_sentence TEXT;
ALTER TABLE review_queue ADD COLUMN code TEXT;
ALTER TABLE review_queue ADD COLUMN answer TEXT;
CREATE INDEX review_queue_uncoded ON review_queue (review_id) WHERE code IS NULL;
