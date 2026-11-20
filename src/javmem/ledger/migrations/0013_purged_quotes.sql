-- 第四轮审阅（D101）：彻底删除以后，只在引用里看到过的话照样挡住撤回了的内容。
--
-- 取包时（javmem.pipeline.batch）按原文索引认撤回了的话：同一句话有一份撤回了，别的引用不当证据；这封信回的那一封
-- 撤回了，也算在（折了行的引用和原文一字不差对不上）。彻底删除把原文索引里的句子删了，这两样就认不出了。
-- 彻底删除时（javmem.withdraw.purge）在删以前记下这两样，只有指纹和 Message-ID，没有原文。撤回过的才会彻底删除，
-- 所以都只挡、不放。搬到新账本时不导出（和 withdrawn_texts 认不出截短的引用一样，见开发说明“撤回”）。
CREATE TABLE purged_mails (
    message_id TEXT NOT NULL PRIMARY KEY,                    -- 彻底删除的信：回它的信里的引用不当证据
    purged_at  TEXT NOT NULL
);
CREATE TABLE purged_copies (
    copy_key  TEXT NOT NULL PRIMARY KEY,                     -- 彻底删除的句子的比引用用的指纹：同一句话别的引用不当证据
    purged_at TEXT NOT NULL
);
