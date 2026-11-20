-- D101：只在引用里看到过的话。
--
-- raw_sentences.copy_key 是比引用用的写法（去掉开头的 >、压空白、去掉结尾的句末标点，javmem.raw.index.copy_form）
-- 的指纹，8 个字以上才有。取包时（javmem.pipeline.batch）按它找账本里同样的话：有能当证据的原话，引用只当上文；
-- 没有的，最早的那一份引用当证据。升到这一版时，javmem.ledger 在同一个事务里接着跑 Python 的那一步
-- （javmem.raw.index.fill_copy_key），补上已有的句子。
ALTER TABLE raw_sentences ADD COLUMN copy_key TEXT;
CREATE INDEX raw_sentences_by_copy ON raw_sentences (copy_key) WHERE copy_key IS NOT NULL;
-- 这封信回的那一封（In-Reply-To）在不在账本里，按 Message-ID 找
CREATE INDEX raw_sentences_by_message ON raw_sentences (message_id) WHERE message_id IS NOT NULL;

-- 每一句只在引用里看到过的话（指纹加可见范围），现在是哪一份引用在当证据。写账时（run_write）记；
-- 下一批按它看谁要换：以前当证据的那一份不当了、新当证据的那一份在已经写完了的包里，那个包改回排队。
-- 只有编号和指纹，没有原文
CREATE TABLE quote_holders (
    copy_key    TEXT    NOT NULL,
    scope       TEXT    NOT NULL,                            -- 可见范围：{"partitions": […], "sensitive": […]}
    sentence_id TEXT    NOT NULL,                            -- 当证据的那一句
    packet_id   TEXT    NOT NULL,
    revision    INTEGER NOT NULL,
    in_reply_to TEXT,                                        -- 那一句所在的信回的那一封：它到了，这一份就不当了
    PRIMARY KEY (copy_key, scope)
);
CREATE INDEX quote_holders_by_packet ON quote_holders (packet_id);
CREATE INDEX quote_holders_by_reply ON quote_holders (in_reply_to) WHERE in_reply_to IS NOT NULL;
