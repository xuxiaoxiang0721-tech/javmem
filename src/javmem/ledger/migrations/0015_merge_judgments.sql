-- J3.6（D100 第 8 条）：合并卡片上模型的看法，只当参考，不算数。同样的提议、同样送出去的文字、同一版提示词、同一个
-- 模型再出卡片，用存下的，不重调。理由是模型写的、从原话来，所以记下送了哪几句、哪几个包：撤回、彻底删除时连着删。
CREATE TABLE merge_judgments (
    judgment_key   TEXT    PRIMARY KEY,                      -- 提议里的实体、送出去的文字、提示词版本、模型
    proposal_id    TEXT    NOT NULL,
    entities       TEXT    NOT NULL,                         -- 实体编号，JSON 列表
    sentences      TEXT    NOT NULL,                         -- 送出去的原话的句子编号，JSON 列表
    packets        TEXT    NOT NULL,                         -- 那几句在哪几个包，JSON 列表
    prompt_version TEXT    NOT NULL,
    model          TEXT    NOT NULL,                         -- 名字@档位/输出方式
    same           TEXT    NOT NULL CHECK (same IN ('yes', 'no', 'unsure')),
    reason         TEXT    NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
CREATE INDEX merge_judgments_by_proposal ON merge_judgments (proposal_id);
