-- 抽取失败的包为什么失败（排队修，11-17）：写账把被“重调也没用”的窗口挡住的包标成 extraction = 'failed'，
-- 这一列记原因：种类和段数，比如 format_error=1;unredacted=2，不抄原文。改回 queued、改成 done 时清空。
-- 巡检照它说是哪一种（packet-spec 第 12 节“抽取失败的包”）。
ALTER TABLE packets ADD COLUMN extraction_note TEXT;
