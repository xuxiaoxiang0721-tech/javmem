-- J2.12：冻结和待确认队列的管理命令，巡检的警告标“看过了”。
--
-- 冻结标记早就有（facts.frozen），待确认就是 review_queue。这里只补两样：谁定的，看过了没有。

-- 待确认是谁定的：owner 主人用管理命令定的；auto 30 天没人处理，按“两边都留”关掉的（D6）
ALTER TABLE review_queue ADD COLUMN decided_by TEXT CHECK (decided_by IS NULL OR decided_by IN ('owner', 'auto'));

-- 巡检的警告：主人用管理命令标了“看过了”就不再报。同样的警告又来一次（seen_count 加一）重新报
ALTER TABLE patrol_warnings ADD COLUMN acknowledged_at TEXT;

-- 巡检现算的提醒（现在只有 daily 包的缺口）标了“看过了”的。key 是这条提醒的指纹：来源和缺口的起止
CREATE TABLE patrol_acks (
    kind            TEXT NOT NULL,
    key             TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
