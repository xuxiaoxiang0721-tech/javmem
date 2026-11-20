"""校准工具包（U3，D141）：给拿 javmem 用在自己数据上的人，在自己的数据上校准。

- `gold_check`：`javmem gold check`，核一份金标准写得对不对（只报条目编号和哪一种不对，不抄内容）。
- `prelabel`：`javmem gold prelabel`，照账本里现在抽出来的，出一份金标准的草稿给人审。
- `review`：`javmem gold review`，把金标准做成一页本机打开的审阅页面（对、错、改，补一条），存下来就是审阅结果。
- `selfcheck`：`javmem selfcheck`，不用金标准也能看的四个数：两遍一样不一样、证据在原文里找得到的比例、合在一起的
  事实里日子编号金额对不上的、同一样东西几个名字。只出数和编号。
- `quiz`：`javmem quiz`（可选，要调模型、花钱），拿自己的题让模型照查到的答一遍、再严格判一遍分（数字条件先按规则比）。

金标准的格式见 docs/gold-format.md；整套怎么用（先跑演示看懂打分，再拿自己的十来个包做金标准、打分）见
docs/calibration.md。
"""
