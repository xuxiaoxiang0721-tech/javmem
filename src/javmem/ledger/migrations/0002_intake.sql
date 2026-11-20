-- 收件要的两张辅助表。不算 01 里的账本表，巡检（packet-spec 第 12 节）读它们。


-- 收件时记下的警告。只写包号、行号和类型，不抄原文。
-- 同样的警告再来一次只加次数，不多一行。
CREATE TABLE patrol_warnings (
    warning_id    INTEGER PRIMARY KEY,
    kind          TEXT    NOT NULL,
    packet_id     TEXT    NOT NULL DEFAULT '',
    revision      INTEGER NOT NULL DEFAULT 0,
    line          INTEGER NOT NULL DEFAULT 0,
    detail        TEXT    NOT NULL DEFAULT '',
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    resolved_at   TEXT,                                  -- 这个版本被替换了，警告就不用再提醒
    UNIQUE (kind, packet_id, revision, line, detail)
);
CREATE INDEX patrol_warnings_open ON patrol_warnings (last_seen_at) WHERE resolved_at IS NULL;


-- 处理任务上一次什么时候跑的。巡检看它，太久没跑就提醒。
CREATE TABLE task_runs (
    task        TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
