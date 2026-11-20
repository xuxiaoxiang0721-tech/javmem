"""打分（J3.8）：拿金标准给一边的抽取结果打分。一边可以是 javmem 自己的账本，也可以是以后的对手。

几块：

- javmem.scoring.common：对齐格式 `javmem-score/1`。哪一边都先转成它，再打分；对手写一个转换就能接进来。
- javmem.scoring.gold：读金标准（`javmem-gold-prelabel/1` 的单元和 `javmem-gold-entities/1` 的实体表），
  叠上审阅结果（D109：审过的以审的为准，没审的照预标），同一条关系跨单元合成一条。
- javmem.scoring.ledger：javmem 的账本（加 L3 的结果库算成本）转成对齐格式。只读。
- javmem.scoring.align：两边的实体对齐。名字不一样的按别名表（第一版、第二版都认）对上；对不上的、有歧义的列出来。
- javmem.scoring.match：一条一条比：事实、关系、状态变化、别名。
- javmem.scoring.report：算分、写报告。给人看的“对不上的”隐去是哪一边（甲、乙……）；给写提示词的人看的那份只有
  错误类型和数，例子只用这一边自己抽错的，不带金标准的原话和条目。

金标准、真实的抽取结果和分数都放仓库外；这里只有代码，测试用编的数据。用法见 docs/development.md 的“打分”。
"""
