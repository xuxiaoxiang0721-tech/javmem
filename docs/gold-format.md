# 金标准的格式

金标准是“这几个包里本来应该抽出什么”的标准答案：人一条一条定下来，`javmem score` 拿它给抽取结果打分。格式就是打分读的那一种；`javmem gold check` 照它核，审阅页面（`javmem gold review`）照它出选择卡。整套怎么做见 [calibration.md](calibration.md)。

例子都取自仓库里那份编的金标准（`tests/fixtures/regress-mini/gold/`，`javmem demo` 用的就是它）。

## 1. 一个目录里放什么

| 文件 | 是什么 |
| --- | --- |
| `<单元>.json`，比如 `D1.json` | 一个单元一个：几个包、里面应该抽出的条目。格式 `javmem-gold-prelabel/1` |
| `实体表.json` | 这一份金标准里出现的人、公司、项目……的标准名和别名。格式 `javmem-gold-entities/1` |
| 审阅结果（可选，名字随便） | 人审过以后的结论：哪条对、哪条错、哪条改成什么。打分时用 `--review` 给 |

## 2. 单元

```json
{
  "schema": "javmem-gold-prelabel/1",
  "unit": "D1",
  "unit_kind": "dialog",
  "title": "编的小套件的 5 个包",
  "packets": ["2026-10-05_2300_demo-bot", "2026-10-12_2300_demo-bot"],
  "revisions": {"2026-10-05_2300_demo-bot": 1, "2026-10-12_2300_demo-bot": 1},
  "not_labeled": [],
  "new_entities": [],
  "items": []
}
```

- `packets`：这个单元管哪几个包；`revisions`：标的是每个包的第几版（版本对不上的单元不打分）。
- `not_labeled`：不标的句子：抽到了不算错，没抽到不扣分。`{"range": [起, 止], "reason": …}` 是一段（含两头），`{"sids": […], "reason": …}` 是这几句。
- `new_entities`：实体表里还没有、这个单元里新出现的（`{"name", "type"}`）。
- `items`：条目，一条一个对象，编号在一个单元里不重复。

## 3. 条目

四种：事实 `fact`、关系 `relation`、变化 `change`、别名 `alias`。每条都有：

| 字段 | 说明 |
| --- | --- |
| `id` | 编号，比如 `D1-F001`（单元号、种类、序号） |
| `type` | `fact`、`relation`、`change`、`alias` |
| `content` | 一句能直接读的话，给人看、给审阅页面用 |
| `subject` | 主体 `{"name", "type"}`：名字是实体表里的标准名；类型是 人、公司或机构、项目、订单或合同、地点、资产、Bot 之一 |
| `evidence` | 出处：`[{"sid": 句子编号, "quote": 原话片段（可以空）}]`。句子编号是 `包号#小节.行.句`，和查询接口返回的一样；写了原话片段的，要是那一句的一段 |
| `when` | 什么时候：`{"from", "to", "precision", "basis"}`。精度是 `minute`、`day`、`month`、`quarter`、`year`、`unknown` 之一；`basis` 写这个日子是怎么来的（比如“说话那天”“不清楚”） |
| `certainty`、`speaker` | 确定程度、谁说的（编的那份都是“原话”“主人”），可以不写 |
| `unsure`、`important`、`note` | 标的人拿不准、影响大、备注：审阅时先看这些 |

**事实**另有：`fact_class`（事实、主人的偏好和规矩、主人的决定和计划、Bot 的建议和判断、和主人有关的行情，五种之一）、`attribute`、`value`。

```json
{"id": "D1-F008", "type": "fact", "content": "晨光一号基金这周的净值是 1.032。",
 "subject": {"name": "晨光一号基金", "type": "资产"}, "fact_class": "和主人有关的行情", "attribute": "净值", "value": "1.032",
 "evidence": [{"sid": "2026-10-20_2300_demo-bot#1.4.1", "quote": ""}],
 "when": {"from": "2026-10-20", "to": null, "precision": "day", "basis": "说话那天"}}
```

**关系**另有：`relation`（持有、托管于、负责、任职于、客户是、亲属、隶属于，七种之一）、`object`（客体 `{"name", "type"}`）、`state`（生效、已结束、暂停）、`fields`（亲属写具体的关系，比如 `{"kinship": "表姐"}`）。两头的类型要是允许的那几种：比如“负责”是人对项目、订单、人、公司，“任职于”是人对公司或机构。

```json
{"id": "D1-R001", "type": "relation", "content": "祝明远负责梧桐路展厅项目（2026-10-05 起，2026-11-02 交给田甜）。",
 "subject": {"name": "祝明远", "type": "人"}, "relation": "负责", "object": {"name": "梧桐路展厅项目", "type": "项目"},
 "state": "已结束", "evidence": [{"sid": "2026-10-05_2300_demo-bot#1.1.1", "quote": ""}],
 "when": {"from": "2026-10-05", "to": "2026-11-02", "precision": "day", "basis": "说话那天"}}
```

**变化**另有：`change`（`kind` 哪一种变化、`target` 变的是什么、`before`、`after`、`soft` 是不是说得不干脆的那种、`is_change` 算不算变化）、`links`（变的是哪几条事实）。出处里 `role` 写 `before`、`after`，分清哪一句是变以前、哪一句是变以后。

**别名**另有：`alias`（`mention` 原文里的叫法、`entity` 它指的标准名、`entity_type`、`kind` 比如“简称”）。

## 4. 实体表

```json
{"schema": "javmem-gold-entities/1",
 "entities": [
   {"name": "主人", "type": "人", "aliases": ["林青", "包里说话人“我”"], "from": "编的"},
   {"name": "南岭纸业", "type": "公司或机构", "aliases": ["南岭"], "from": "编的"}
 ]}
```

条目里用的名字都要是这里的标准名（或者单元里 `new_entities` 写着的）。打分时两边的名字按这张表和别名表对齐。

## 5. 审阅结果

```json
{"reviews": {
   "D1-F001": {"v": "ok"},
   "D1-F002": {"v": "wrong", "text": "这一句说的是别家"},
   "D1-R002": {"v": "fix", "text": "开始日子应该是 10-12", "item": {"id": "D1-R002", "…": "改好的整条"}}
 },
 "additions": [{"unit": "D1", "text": "漏了一条：北渡物流每周二、周五送货"}]}
```

- `ok` 照留；`wrong` 去掉；`fix` 带着改好的整条（`item`，和条目一样的结构）的换成它，只有 `text` 的先不算分、单列，等改成结构再算。
- `additions` 是补的：带 `item` 的加进来，只有 `text` 的单列。
- 没审的照原样。

## 6. 核一遍

```bash
javmem gold check --gold <金标准目录> --library test
```

结构不对就读不了（退出码 2）；读得了的一条一条核：出处在不在这个单元的包里、账本里有没有这一句、原话片段是不是那一句的一段、名字在不在实体表里、类型、关系、分类、精度写得对不对。只打条目编号和哪一种不对（退出码 1），不抄内容。设了 `JAVMEM_DATA_DIR` 才核账本里的句子。
