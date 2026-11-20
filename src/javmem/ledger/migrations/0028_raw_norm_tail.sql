-- D196：两个字的词在原文层不再整张扫（javmem.raw.search.short_candidates）。三字索引（raw_norm_fts，0010）找得到后面还有字的
-- 那些地方：一定有一个以这两个字开头的三字。在整句最后的（后面没有字了，三字索引里没有它）靠这个索引：每句统一写法的最后两个字。
-- 两种合起来是候选，候选再照原来的 norm LIKE 核一遍，结果和整张扫一样。
--
-- 只建一个索引：不加列、不加表、不动三字索引和触发器，以前的数据一行不动（建索引时把已有的句子都登进去）。
CREATE INDEX raw_sentences_norm_tail ON raw_sentences (substr(norm, -2));
