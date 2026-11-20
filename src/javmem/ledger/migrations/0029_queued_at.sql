-- 巡检要 javmem 改的第 1 样（D211，11-19）：每个包最近一回进排队（extraction 改成 queued）的时刻。巡检照它算
-- “收进来多久还没进记忆”：received_at 不行，撤回、重发、合并会让旧包重新排队。
--
-- 进排队时写现在：收件登记（和 received_at 同一个时刻）、写完了（done）又改回排队、解除撤回。本来就在排队的
-- （queued、failed：标了 failed 的取包时照样取）接着算，不动；done 的留着上一回的。写法和 received_at 一样，
-- 带时区的 ISO 8601，到秒。private 不抽的（skipped）是空的。
--
-- 只加一列，不扫表：以前的行空着，不知道它们是从什么时候开始排的。
ALTER TABLE packets ADD COLUMN queued_at TEXT;
