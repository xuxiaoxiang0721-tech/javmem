"""中文规则（J2.14）：几处都要用的中文写法，全是纯函数，不碰账本，不调模型。

| 模块 | 是什么 | 谁用 |
| --- | --- | --- |
| `numbers` | 中文的数（两千五、一万二、三点五万、百分之三十）、币种、金额和日期的统一写法 | L4 的 G2，L5 的值 |
| `clock` | 几点几分的各种写法（下午2:00、7:29 PM、三点一刻、3 点 15） | L4 的 G6 |
| `search` | 原文检索的统一写法（D77）：空格、全角半角不算差别，数两头加记号 | 原文层、raw_search |
| `lexicon` | 仓库外的别名表和词典（`JAVMEM_LEXICON_FILE`） | L5 的实体对齐、问话切词 |
| `segment` | 问话切词（jieba 加词典），memory_search 回退原文层用（query-api 5.1） | J5.5 |

真实的人名、公司名只在仓库外的配置里，代码和测试里没有。
"""

from javmem.zh.segment import query_words

__all__ = ["query_words"]
