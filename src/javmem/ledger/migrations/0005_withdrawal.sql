-- J2.11：撤回和彻底删除。
--
-- 撤回默认只标记（packets.withdrawn_at、raw_sentences.withdrawn_at），派生的说法、事实、别名、隔离、向量删掉。
-- 彻底删除（javmem purge）才删归档文件和原文索引里的句子，删完在这里记一笔：包登记那一行留着，
-- 这样撤回过的包号还是不再收，巡检也不会把删掉的归档报成丢了。
ALTER TABLE packets ADD COLUMN purged_at TEXT;                        -- 这个版本的归档和原文已经彻底删除
