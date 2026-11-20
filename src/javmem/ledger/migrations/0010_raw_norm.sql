-- J2.14（D77）：原文检索按统一写法比。
--
-- raw_sentences.norm 是原句的统一写法（javmem.zh.search.searchable：NFKC、不分大小写、去掉空白，数两头加记号，
-- 月日、带单位的金额换成统一的样子），raw_norm_fts 是它的全文索引（trigram）。raw_search 查这一份；
-- raw_fts（原句的）照旧跟着走，不删。
--
-- 升到这一版时，javmem.ledger 在同一个事务里接着跑 Python 的那一步：先把已有的句子按现在的 norm（空的）登进
-- raw_norm_fts，再调 javmem.raw.index.fill_norm 逐句算出来（触发器把空的删掉、新的加上）。这样每一句都在索引里，
-- 删的时候总对得上。统一写法改了（zh.search.VERSION）要再加一个迁移，调 fill_norm 重算。
ALTER TABLE raw_sentences ADD COLUMN norm TEXT NOT NULL DEFAULT '';

CREATE VIRTUAL TABLE raw_norm_fts USING fts5 (
    norm,
    content = 'raw_sentences',
    content_rowid = 'id',
    tokenize = 'trigram'
);
CREATE TRIGGER raw_sentences_norm_after_insert AFTER INSERT ON raw_sentences BEGIN
    INSERT INTO raw_norm_fts (rowid, norm) VALUES (new.id, new.norm);
END;
CREATE TRIGGER raw_sentences_norm_after_delete AFTER DELETE ON raw_sentences BEGIN
    INSERT INTO raw_norm_fts (raw_norm_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
END;
CREATE TRIGGER raw_sentences_norm_after_update AFTER UPDATE OF norm ON raw_sentences BEGIN
    INSERT INTO raw_norm_fts (raw_norm_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
    INSERT INTO raw_norm_fts (rowid, norm) VALUES (new.id, new.norm);
END;
