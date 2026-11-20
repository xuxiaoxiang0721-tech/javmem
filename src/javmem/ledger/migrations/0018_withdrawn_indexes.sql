-- 收件越收越慢（J3.2 邮件导入报告第 4 节第 22 条）：收件收尾时要找“撤回了的话”（withdraw._withdrawn_texts：新来的包里
-- 引用撤回内容的行跟着标上撤回）。原来一条带 OR 的查询没有能用的索引，每收一个包都把整张原文索引从头扫一遍：
-- 测试库 183 万句时一个包要 1.1 秒，库越大越慢。撤回了的句子、撤回了的包都很少，各建一个只收它们的小索引，
-- 查询拆成两段，各走各的（见 withdraw._withdrawn_texts）。
--
-- 只加索引，表和数据都不动。旧账本升到这一版时，建索引要把原文索引扫一遍，只这一次。
CREATE INDEX raw_sentences_withdrawn ON raw_sentences (withdrawn_at) WHERE withdrawn_at IS NOT NULL;
CREATE INDEX packets_withdrawn ON packets (withdrawn_at) WHERE withdrawn_at IS NOT NULL;
