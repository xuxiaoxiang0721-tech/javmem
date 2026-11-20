-- 巡检盯着写账变慢（巡检加快慢检查，11-16）：javmem write 每次写完记一行，用了多久、写了几个包几个窗口。只有数，
-- 不抄原文，不记名字和包里的内容，所以撤回、彻底删除都不用管它。辅助表，不算 01 里的账本表。
--
-- 一个包、一个窗口都没写的不记；跑到一半账本写不进去、崩了的不记。巡检拿最近一次和前面窗口数差不多的几次比，
-- 怎么比、哪几次不拿来比（停下了的……）见开发说明“巡检”。
CREATE TABLE write_runs (
    run_id            INTEGER PRIMARY KEY,
    started_at        TEXT    NOT NULL,                                    -- 拿到写账锁的时候
    seconds           REAL    NOT NULL CHECK (seconds >= 0),               -- 拿到锁到出完汇总，一共几秒
    embedding_seconds REAL    NOT NULL CHECK (embedding_seconds >= 0),     -- 其中等向量服务的：调用（连同出错的）、重试前等的
    packets           INTEGER NOT NULL CHECK (packets >= 0),               -- 这次写完、改成 done 的包
    windows           INTEGER NOT NULL CHECK (windows >= 0),               -- 这次过了一遍的窗口：写了的，加上同样的结果写过、跳过的
    facts             INTEGER NOT NULL CHECK (facts >= 0),                 -- 写完以后账本里一共多少条事实
    stopped           INTEGER NOT NULL DEFAULT 0 CHECK (stopped IN (0, 1)) -- 向量服务调不通，停下了
);
