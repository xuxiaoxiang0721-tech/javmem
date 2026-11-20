-- J2.13：来源和信任。每条事实、每条证据都记下哪个 Bot（来源）、原话还是推断、是不是外部内容。
--
-- 包一级早就有：packets.source 是哪个 Bot（哪个邮箱），channel 是 email 的整包都算外部内容；原文索引每一句都有
-- origin（said、relayed、external、attachment_note）和 speaker_role（owner、bot、other）。
-- 证据和事实原来要按句子编号回原文索引去查；替换以后旧版本的句子不在原文索引里了，冻结事实的证据就查不到。
-- 所以写证据时把这几样抄下来，事实再按它的证据算好。查询接口（J5.5）直接读这些列。

-- 证据：这一句的来源、origin、说话人是谁
ALTER TABLE evidence ADD COLUMN source TEXT;                          -- 来源编号（哪个 Bot、哪个邮箱）
ALTER TABLE evidence ADD COLUMN origin TEXT                           -- said 说话人自己说的；relayed 转发；external 外部内容（整个邮件包也是）；attachment_note 附件说明
    CHECK (origin IS NULL OR origin IN ('said', 'relayed', 'external', 'attachment_note'));
ALTER TABLE evidence ADD COLUMN speaker_role TEXT                     -- owner 主人；bot 这个来源自己；other 别人（邮件的发件人一律算 other）
    CHECK (speaker_role IS NULL OR speaker_role IN ('owner', 'bot', 'other'));

-- 事实：按它的证据算
ALTER TABLE facts ADD COLUMN sources TEXT NOT NULL DEFAULT '[]';      -- 证据来自哪些来源，JSON 列表
ALTER TABLE facts ADD COLUMN origins TEXT NOT NULL DEFAULT '[]';      -- 证据有哪几种 origin，JSON 列表
ALTER TABLE facts ADD COLUMN secondhand INTEGER NOT NULL DEFAULT 0    -- 证据全都不是说话人自己的话（外部内容、转发、附件说明）：当转述，不当事实
    CHECK (secondhand IN (0, 1));
ALTER TABLE facts ADD COLUMN stated INTEGER NOT NULL DEFAULT 0        -- 原话直接说的（有一处说法 verified）；0 是推断出来的。待确认的也照样记
    CHECK (stated IN (0, 1));
ALTER TABLE facts ADD COLUMN owner_said INTEGER NOT NULL DEFAULT 0    -- 证据里有主人自己的话（speaker_role owner、origin said）
    CHECK (owner_said IN (0, 1));

-- 已经写了的补上：证据按原文索引、包登记补，查不到的（旧版本的句子）留空
UPDATE evidence SET
    source = (SELECT p.source FROM packets AS p WHERE p.packet_id = evidence.packet_id AND p.revision = evidence.revision),
    origin = (SELECT s.origin FROM raw_sentences AS s WHERE s.sentence_id = evidence.sentence_id AND s.revision = evidence.revision),
    speaker_role = (SELECT s.speaker_role FROM raw_sentences AS s WHERE s.sentence_id = evidence.sentence_id AND s.revision = evidence.revision);

UPDATE facts SET
    sources = coalesce((SELECT json_group_array(source) FROM (SELECT DISTINCT v.source FROM evidence AS v
        WHERE v.fact_id = facts.fact_id AND v.source IS NOT NULL ORDER BY v.source)), '[]'),
    origins = coalesce((SELECT json_group_array(origin) FROM (SELECT DISTINCT v.origin FROM evidence AS v
        WHERE v.fact_id = facts.fact_id AND v.origin IS NOT NULL ORDER BY v.origin)), '[]'),
    secondhand = CASE WHEN EXISTS (SELECT 1 FROM evidence AS v WHERE v.fact_id = facts.fact_id AND v.origin IS NOT NULL)
        AND NOT EXISTS (SELECT 1 FROM evidence AS v WHERE v.fact_id = facts.fact_id AND (v.origin IS NULL OR v.origin = 'said'))
        THEN 1 ELSE 0 END,
    stated = CASE WHEN EXISTS (SELECT 1 FROM fact_claims AS c WHERE c.fact_id = facts.fact_id AND c.status = 'verified')
        OR (NOT EXISTS (SELECT 1 FROM fact_claims AS c WHERE c.fact_id = facts.fact_id) AND facts.status = 'verified')
        THEN 1 ELSE 0 END,
    owner_said = CASE WHEN EXISTS (SELECT 1 FROM evidence AS v WHERE v.fact_id = facts.fact_id
        AND v.origin = 'said' AND v.speaker_role = 'owner') THEN 1 ELSE 0 END;
