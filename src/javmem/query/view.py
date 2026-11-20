"""账本视图（J5.5，query-api 第 3、4 节）：事实、关系、别名、实体从账本只读地查。

- **过滤都写在 SQL 里**（3.2）：看得到的才命中、才排序、才数进 evidence_total，不先取前 N 条再滤。
- **挑事实、关系的条件只在两处：** `fact_conditions`、`relation_conditions`。每个查事实、关系的地方都用它们；J5.3 进图前
  检查合进来时只在这两处加它的条件（主会话 11-18）。
- **一次调用一个 LedgerView：** 连接已经开了读事务（开发说明“原文层”：一次查询的所有读都在一个读事务里），名字、
  实体看不看得到在这一次调用里缓存。
- **名字**（规矩题 1，主会话 11-18 照建议定，明早问主人）：标准名有看得到的出处，或者是主人配置里写的名字（别名表
  第二版的组、Bot 名单，组算数的分区和这个身份看得到的有交集）、主人，就写标准名；不然写这个身份看得到的、最早说到的
  那个别名；一个都没有写 NO_NAME（“[没有看得到的叫法]”），不退回去写标准名。出处被撤回了的标准名不算“没出处”：
  不然撤回“老周就是周建华”那一包（或者只撤那一句）以后，别的包里“老周”的事实会把全名带出来（审阅第 1 条）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from functools import lru_cache
from typing import Any

from javmem.access import Identity, has_any_in, sees, visible_packet, visible_scope
from javmem.config import ConfigError, Settings
from javmem.pipeline.merge import OWNER_NAME, entity_id_of, name_key
from javmem.pipeline.precheck import enabled as precheck_enabled
from javmem.pipeline.precheck import view_condition as precheck_condition
from javmem.pipeline.verify import fold_map
from javmem.raw.search import SENTENCE_COLUMNS, evidence_of, masked_fields
from javmem.timeutil import epoch, parse_aware
from javmem.zh.lexicon import LEDGER_TYPES, Lexicon, load_lexicon

MAX_EVIDENCE = 5  # 4.2：evidence 最多回 5 条，新的在前
NO_NAME = "[没有看得到的叫法]"  # 标准名不能写、一个看得到的别名都没有时写它（审阅第 1 条）
MAX_REDIRECTS = 20  # 合并的跳转最多跟几步（跳转表本来是直接对到最后那个的，这是保险）

# 关系另带的字段（D118）写进 content 那一句（规矩题 5，主会话 11-18 照建议定）
_DETAILS = (("role", "角色"), ("position", "职位"), ("department", "部门"), ("account", "账户"), ("service", "服务"))
_BASIS_TEXT = {"approx": "约", "trade_date": "按成交日"}

_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)
_MONTH = re.compile(r"(\d{4})-(\d{2})", re.ASCII)
_QUARTER = re.compile(r"(\d{4})-Q([1-4])", re.ASCII)
_YEAR = re.compile(r"(\d{4})", re.ASCII)


def period(text: object, tz: tzinfo) -> tuple[int, int] | None:
    """一个照精度写的时间（2026、2026-Q4、2026-11、2026-11-09、带时分的）换成一段：[起, 止)，秒。只写日期、没带时区的按
    服务配置的时区算。认不出返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    try:
        if match := _DAY.fullmatch(value):
            start = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=tz)
            return epoch(start), epoch(start + timedelta(days=1))
        if match := _MONTH.fullmatch(value):
            year, month = int(match[1]), int(match[2])
            start = datetime(year, month, 1, tzinfo=tz)
            end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=tz)
            return epoch(start), epoch(end)
        if match := _QUARTER.fullmatch(value):
            year, quarter = int(match[1]), int(match[2])
            start = datetime(year, quarter * 3 - 2, 1, tzinfo=tz)
            end = datetime(year + quarter // 4, (quarter * 3) % 12 + 1, 1, tzinfo=tz)
            return epoch(start), epoch(end)
        if match := _YEAR.fullmatch(value):
            start = datetime(int(match[1]), 1, 1, tzinfo=tz)
            return epoch(start), epoch(datetime(int(match[1]) + 1, 1, 1, tzinfo=tz))
        moment = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        return None
    if moment.utcoffset() is None:
        moment = moment.replace(tzinfo=tz)
    return epoch(moment), epoch(moment) + 60


@lru_cache(maxsize=65536)
def name_key_of(name: str) -> str:
    """名字的比对写法（L5 的 name_key），实体多的时候每次查都要把标准名过一遍，记住算过的。"""
    return name_key(name)


_key = name_key_of


_LATIN = re.compile(r"[A-Za-z0-9]")  # 拉丁字母、数字：这种写法的叫法要在词的边界上才算


def mask_names(text: str, hidden: Mapping[str, str], protected: Collection[str]) -> str:
    """text 里 hidden 的那几个叫法（比对写法 → 换成什么）换掉，返回换过的原文（复审 A、B、C）。

    - 照比对写法找（verify.fold_map：NFKC、不分大小写、去掉空白，和 L4、L5 认名字一样），所以原文里写成“Tran Mai”、
      “周 建华”、“Strauß”的都认得出；换掉的是原文里对上的那一段，别处原样不动。
    - protected 是看得到的名字：藏起来的叫法整个落在一个看得到的名字里面的不换（“周建”在“周建华”里），不然看得到的
      名字被拦腰换掉，一对原句就看得出藏的是什么。看得到的名字落在藏起来的里面的照换（“Mai”在“Tran Mai”里）。
    - 一遍换完，换上去的名字不会再被换。"""
    keys = sorted((key for key in hidden if len(key) >= 2 and key not in protected), key=lambda key: (-len(key), key))
    if not text or not keys:
        return text
    # 先拿比对写法（有缓存）粗查：它就是 fold_map 那一份去掉两头的标点，藏起来的叫法两头没有这些标点，对不对得上一样；
    # 对不上的不用一个字一个字地 fold_map（按词找时一页上万条，它占了一半的时间，J5.5 隐私两处的审阅 R3）
    plain = name_key_of(text)
    if not any(key in plain for key in keys):
        return text
    folded, where = fold_map(text)
    guards: list[tuple[int, int]] = []
    for name in protected:
        start = folded.find(name) if name else -1
        while start >= 0:
            guards.append((start, start + len(name)))
            start = folded.find(name, start + 1)
    pieces: list[str] = []
    last = 0
    for match in re.finditer("|".join(re.escape(key) for key in keys), folded):
        low, high = match.span()
        # 和看得到的名字叠住了的不换：整个在它里面的、只叠住一半的都不换；看得到的名字整个在这一段里面的照换（“Mai”在
        # “Tran Mai”里）
        if any(start < high and low < end and not (low <= start and end <= high) for start, end in guards):
            continue
        begin, finish = where[low], where[high - 1] + 1
        if begin < last:
            continue
        if _LATIN.search(match.group(0)) and (_LATIN.match(text[begin - 1 : begin]) or _LATIN.match(text[finish : finish + 1])):
            continue  # 英文的叫法要在词的边界上：“email”里的“mai”、“Linda”里的“li”、跨词凑出来的“maia”都不是它
        pieces.append(text[last:begin])
        pieces.append(hidden[match.group(0)])
        last = finish
    if not pieces:
        return text
    pieces.append(text[last:])
    return "".join(pieces)


@dataclass(frozen=True)
class NameMatch:
    entity_id: str
    matched_alias: str | None  # 按别名对上的是哪个；按标准名对上的是 None


def _scope_json(partitions: str, sensitive: str) -> dict[str, list[str]]:
    return {"partitions": json.loads(partitions), "sensitive": json.loads(sensitive)}


class LedgerView:
    """一次调用里的账本视图。conn 是只读连接，已经开了读事务；partitions 是入参 partitions（已经核过不越权），
    None 是身份看得到的全部。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        identity: Identity,
        settings: Settings,
        *,
        as_of: datetime,
        partitions: Sequence[str] | None = None,
    ) -> None:
        self.conn = conn
        self.identity = identity
        self.settings = settings
        self.as_of = as_of
        self.as_of_epoch = epoch(as_of)
        self.partitions = None if partitions is None else tuple(partitions)
        self._names: dict[str, str] = {}
        self._visible: dict[str, bool] = {}
        self._config: set[tuple[str, str]] | None = None
        self._lexicon: Lexicon | None | bool = False  # False 是还没读
        self._names_of: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        self._prechecked: bool | None = None  # 开没开进图前检查（J5.3）：一次调用读一次配置

    # ---------- 看得到的条件 ----------

    def scope_conditions(self, partitions: str, sensitive: str) -> tuple[list[str], list[object]]:
        """一条东西自己的可见范围看得到，再加入参 partitions：结果的分区和它有交集就留下（第 5 节）。"""
        conditions, params = visible_scope(self.identity, partitions, sensitive)
        if self.partitions is not None:
            sql, names = has_any_in(partitions, self.partitions)
            conditions.append(sql)
            params.extend(names)
        return conditions, params

    def packet_conditions(self, packet: str = "p", *, requested: bool = True) -> tuple[list[str], list[object]]:
        """一个包看得到（3.2 的判定规则），再加入参 partitions（requested 是 False 的不加：只看身份）。"""
        conditions, params = visible_packet(self.identity, packet)
        if requested and self.partitions is not None:
            sql, names = has_any_in(f"{packet}.partitions", self.partitions)
            conditions.append(sql)
            params.extend(names)
        return conditions, params

    def fact_conditions(self, fact: str = "f") -> tuple[list[str], list[object]]:
        """挑事实的条件（看得到、没撤回）。所有查事实的地方都用它；J5.3 只在这里加它的条件：开了进图前检查的，不是待确认的
        要谁认过（judged_by），和进图的口子一样（javmem.pipeline.precheck.view_condition）。"""
        conditions, params = self.scope_conditions(f"{fact}.scope_partitions", f"{fact}.scope_sensitive")
        return [f"{fact}.withdrawn_at IS NULL", *conditions, *self._precheck(fact)], params

    def relation_conditions(self, relation: str = "r") -> tuple[list[str], list[object]]:
        """挑关系的条件（看得到、没撤回）。所有查关系的地方都用它；J5.3 只在这里加它的条件（和事实一样）。"""
        conditions, params = self.scope_conditions(f"{relation}.scope_partitions", f"{relation}.scope_sensitive")
        return [f"{relation}.withdrawn_at IS NULL", *conditions, *self._precheck(relation)], params

    def _precheck(self, alias: str) -> list[str]:
        """进图前检查（J5.3，JAVMEM_PRECHECK=on）的条件；没开的一条都不加，和以前一字不差。"""
        if self._prechecked is None:
            self._prechecked = precheck_enabled()
        return [precheck_condition(alias)] if self._prechecked else []

    def current_fact_condition(self, fact: str = "f") -> tuple[str, list[object]]:
        """现行的事实：没被替代，或者替代它的那条这个身份看不到（3.2：状态只认看得到的内容）。"""
        conditions, params = self.fact_conditions("g")
        return (
            f"({fact}.superseded_by IS NULL OR NOT EXISTS (SELECT 1 FROM facts AS g"
            f" WHERE g.fact_id = {fact}.superseded_by AND {' AND '.join(conditions)}))",
            params,
        )

    def lexicon(self) -> Lexicon | None:
        """仓库外的别名表和词典（JAVMEM_LEXICON_FILE）。读不了当没有：名字照出处算，只会更严；切词少几个词。"""
        if self._lexicon is False:
            try:
                self._lexicon = load_lexicon(self.settings.lexicon_file)
            except ConfigError:
                self._lexicon = None
        return self._lexicon  # type: ignore[return-value]

    def alias_visible_sql(self, alias: str = "a", *, requested: bool = True) -> tuple[str, list[object]]:
        return self._alias_visible_sql(alias, requested=requested)

    def _alias_visible_sql(self, alias: str = "a", *, requested: bool = True) -> tuple[str, list[object]]:
        """别名看得到：没撤回，至少一句出处看得到（出处所在的包看得到，这一句没撤回）。requested 是 False 的不按入参
        partitions 收窄，只看身份（切词的词典一个身份一份）。"""
        conditions, params = self.packet_conditions("p", requested=requested)
        return (
            f"{alias}.withdrawn_at IS NULL AND EXISTS (SELECT 1 FROM evidence AS v"
            " JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE v.alias_id = {alias}.alias_id AND s.withdrawn_at IS NULL AND {' AND '.join(conditions)})",
            params,
        )

    def sees_scope(self, partitions: Iterable[str], sensitive: Iterable[str]) -> bool:
        partitions = list(partitions)
        if not sees(self.identity, partitions, sensitive):
            return False
        return self.partitions is None or bool(set(partitions) & set(self.partitions))

    # ---------- 实体和名字 ----------

    def entity_visible(self, entity_id: str) -> bool:
        """3.2：至少有一条看得到的事实（主体或涉及）、关系（主体、客体、代表哪一家）或别名。"""
        if entity_id in self._visible:
            return self._visible[entity_id]
        found = False
        facts, fact_params = self.fact_conditions("f")
        relations, relation_params = self.relation_conditions("r")
        alias_sql, alias_params = self._alias_visible_sql("a")
        for sql, params in (
            (f"SELECT 1 FROM facts AS f WHERE f.subject_entity_id = ? AND {' AND '.join(facts)} LIMIT 1", fact_params),
            (
                "SELECT 1 FROM fact_entities AS x JOIN facts AS f ON f.fact_id = x.fact_id"
                f" WHERE x.entity_id = ? AND {' AND '.join(facts)} LIMIT 1",
                fact_params,
            ),
            (f"SELECT 1 FROM relations AS r WHERE r.subject_entity_id = ? AND {' AND '.join(relations)} LIMIT 1", relation_params),
            (f"SELECT 1 FROM relations AS r WHERE r.object_entity_id = ? AND {' AND '.join(relations)} LIMIT 1", relation_params),
            (f"SELECT 1 FROM relations AS r WHERE r.org_entity_id = ? AND {' AND '.join(relations)} LIMIT 1", relation_params),
            (f"SELECT 1 FROM aliases AS a WHERE a.entity_id = ? AND {alias_sql} LIMIT 1", alias_params),
        ):
            if self.conn.execute(sql, [entity_id, *params]).fetchone() is not None:
                found = True
                break
        self._visible[entity_id] = found
        return found

    def _config_names(self) -> set[tuple[str, str]]:
        """主人配置里写的标准名（类型、比对写法）：别名表第二版的组（组算数的分区和这个身份看得到的有交集）、Bot 名单，
        加上主人。别名表读不了就当没有（名字照出处算，只会更严）。"""
        if self._config is not None:
            return self._config
        names = {("人", name_key(OWNER_NAME))}
        lexicon = self.lexicon()
        if lexicon is not None:
            for group in lexicon.groups:
                if lexicon.group_seen_by(group, self.identity.partitions):  # 和切词的词典同一条（Lexicon.words_for）
                    names.add((group.type, name_key(group.standard)))
            for bot in lexicon.bots:
                names.add(("Bot", name_key(bot.standard)))
        self._config = names
        return names

    def _canonical_shown(self, entity_id: str, entity_type: str, key: str) -> bool:
        alias_sql, params = self._alias_visible_sql("a")
        row = self.conn.execute(
            f"SELECT 1 FROM aliases AS a WHERE a.entity_id = ? AND a.alias_key = ? AND {alias_sql} LIMIT 1",
            [entity_id, key, *params],
        ).fetchone()
        return row is not None or (entity_type, key) in self._config_names()

    def first_alias(self, entity_id: str) -> str | None:
        """这个身份看得到的、最早说到的那个别名（出处里最早的说话时间），一样早的按写法。"""
        conditions, params = self.packet_conditions("p")
        row = self.conn.execute(
            "SELECT a.alias, min(s.said_at_epoch) AS first FROM aliases AS a JOIN evidence AS v ON v.alias_id = a.alias_id"
            " JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE a.entity_id = ? AND a.withdrawn_at IS NULL AND s.withdrawn_at IS NULL AND {' AND '.join(conditions)}"
            " GROUP BY a.alias_id ORDER BY first, a.alias LIMIT 1",
            [entity_id, *params],
        ).fetchone()
        return None if row is None else str(row["alias"])

    def entity_name(self, entity_id: str) -> str:
        """返回里写这个实体的名字（规矩题 1，见模块开头）。"""
        if entity_id in self._names:
            return self._names[entity_id]
        row = self.conn.execute("SELECT canonical_name, type FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
        if row is None:
            name = entity_id
        else:
            canonical = str(row["canonical_name"])
            shown = self._canonical_shown(entity_id, row["type"], _key(canonical))
            # 一个看得到的别名都没有时也不退回去写标准名：它的出处看不到（或者撤回了），写出来就把它带出去了（审阅第 1 条）
            name = canonical if shown else (self.first_alias(entity_id) or NO_NAME)
        self._names[entity_id] = name
        return name

    def name_keys(self, entity_id: str) -> tuple[frozenset[str], frozenset[str]]:
        """这个实体的叫法，(这个身份看得到的, 看不到的)，都是比对写法（L5 的 name_key）。

        看不到的：不能写的标准名（规矩题 1）、出处都看不到的别名，还有编号来历的那个名字（entities.name_key：撤回以后
        标准名会换成剩下的叫法，旧的只留在这一列）。L3 写的事实文字里可能就写着它们（审阅第 2 条）。"""
        if entity_id in self._names_of:
            return self._names_of[entity_id]
        shown = self.entity_name(entity_id)
        visible = {_key(shown)} if shown != NO_NAME else set()
        hidden: set[str] = set()
        row = self.conn.execute("SELECT canonical_name, name_key FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
        if row is not None:
            (visible if shown == row["canonical_name"] else hidden).add(_key(str(row["canonical_name"])))
            if row["name_key"]:
                hidden.add(str(row["name_key"]))
        alias_sql, params = self._alias_visible_sql("a")
        for one in self.conn.execute(
            f"SELECT a.alias, a.alias_key, ({alias_sql}) AS seen FROM aliases AS a WHERE a.entity_id = ?", [*params, entity_id]
        ):
            (visible if one["seen"] else hidden).add(_key(str(one["alias"])))  # 照现在的写法算，以前的规矩存下的也对得上
        keys = (frozenset(key for key in visible if key), frozenset(key for key in hidden - visible if len(key) >= 2))
        self._names_of[entity_id] = keys
        return keys

    def shown_text(self, text: object, entity_ids: Iterable[str]) -> object:
        """L3 写的一段字（事实的文字、属性、值，关系另带的字段、暂停条件）里，这几个实体看不到的叫法换成它现在写的那个
        名字（mask_names：照比对写法找；藏起来的整个落在这几个实体看得到的名字里面的不换）。不是文字的原样回。"""
        if not isinstance(text, str) or not text:
            return text
        hidden: dict[str, str] = {}
        protected: set[str] = set()
        for entity_id in sorted(set(entity_ids)):
            visible, unseen = self.name_keys(entity_id)
            protected.update(visible)
            shown = self.entity_name(entity_id)
            for key in sorted(unseen):
                hidden.setdefault(key, shown)
        return mask_names(text, hidden, protected)

    def fact_entities(self, row: sqlite3.Row) -> list[str]:
        """一条事实提到的实体：主体加事实和实体表里的（对象、提及）。"""
        found = {str(row["subject_entity_id"])} if row["subject_entity_id"] else set()
        found.update(str(one[0]) for one in self.conn.execute("SELECT entity_id FROM fact_entities WHERE fact_id = ?", (row["fact_id"],)))
        return sorted(found)

    def facts_entities(self, rows: Sequence[sqlite3.Row]) -> dict[str, list[str]]:
        """几条事实各自提到的实体（fact_entities 的成批版：一次查 500 条）。"""
        found = {str(row["fact_id"]): ({str(row["subject_entity_id"])} if row["subject_entity_id"] else set()) for row in rows}
        ids = list(found)
        for start in range(0, len(ids), 500):
            part = ids[start : start + 500]
            marks = ", ".join("?" for _ in part)
            for one in self.conn.execute(f"SELECT fact_id, entity_id FROM fact_entities WHERE fact_id IN ({marks})", part):
                found[str(one[0])].add(str(one[1]))
        return {fact_id: sorted(entities) for fact_id, entities in found.items()}

    def fact_content(self, row: sqlite3.Row, entities: Iterable[str] | None = None) -> str:
        """事实的文字，看不到的叫法换掉了（返回、按词比、候选的提示都用这一份）。entities 是这条事实提到的实体（成批查好的），
        不给就现查。"""
        return str(self.shown_text(row["content"], self.fact_entities(row) if entities is None else entities))

    def entity_type(self, entity_id: str) -> str | None:
        row = self.conn.execute("SELECT type FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
        return None if row is None else str(row["type"])

    def entity_ref(self, entity_id: str | None) -> dict[str, str] | None:
        return None if entity_id is None else {"id": entity_id, "name": self.entity_name(entity_id)}

    def find_by_name(self, text: str) -> list[NameMatch]:
        """入参 entity：标准名或别名，照比对写法一样就算（NFKC、不分大小写、去掉空白和两头的标点，和 L5 认名字一样）。
        只认这个身份看得到的：别名要有看得到的出处；标准名要实体看得到、名字也照规矩题 1 写得出来。"""
        key = name_key(text)
        if not key:
            return []
        found: dict[str, str | None] = {}
        # 照现在的标准名算，不用 entities.name_key：那一列是编号的来历，撤回以后标准名会换（“周建华”撤回了剩“老周”），
        # 它还是旧的那个，拿撤回了的全名还对得上（J5.5 测试抓到的）
        for row in self.conn.execute("SELECT entity_id, canonical_name FROM entities"):
            if _key(row["canonical_name"]) != key:
                continue
            entity_id = str(row["entity_id"])
            if self.entity_visible(entity_id) and self.entity_name(entity_id) == row["canonical_name"]:
                found[entity_id] = None
        alias_sql, params = self._alias_visible_sql("a")
        for row in self.conn.execute(
            f"SELECT a.entity_id, a.alias FROM aliases AS a WHERE a.alias_key = ? AND {alias_sql} ORDER BY a.alias",
            [key, *params],
        ):
            found.setdefault(str(row["entity_id"]), str(row["alias"]))
        return [NameMatch(entity_id, alias) for entity_id, alias in sorted(found.items())]

    def find_by_id(self, entity_id: str) -> str | None:
        """入参 entity_id：合并过的旧编号跟着跳转表走（query-api 第 5 节）；看不到的当不存在。"""
        current = entity_id
        for _ in range(MAX_REDIRECTS):
            row = self.conn.execute("SELECT new_id FROM entity_redirects WHERE old_id = ?", (current,)).fetchone()
            if row is None or row["new_id"] == current:
                break
            current = str(row["new_id"])
        exists = self.conn.execute("SELECT 1 FROM entities WHERE entity_id = ?", (current,)).fetchone()
        if exists is None or not self.entity_visible(current):
            return None
        if current != entity_id and not self._named_by_visible(entity_id, current):
            return None  # 跳过来的旧编号不是这个身份看得到的名字算出来的：当不存在（审阅第 3 条）
        return current

    def _named_by_visible(self, old_id: str, entity_id: str) -> bool:
        """旧编号是名字算出来的（类型加比对写法，merge.entity_id_of）：这个实体有一个这个身份看得到的名字（现在写的名字、
        看得到的别名）算出来正好是它，才算这个身份认得这个旧编号。类型合并时可能改过，七类都试。"""
        alias_sql, params = self._alias_visible_sql("a")
        keys = {_key(self.entity_name(entity_id))}
        keys.update(
            str(row[0] or _key(row[1]))
            for row in self.conn.execute(
                f"SELECT a.alias_key, a.alias FROM aliases AS a WHERE a.entity_id = ? AND {alias_sql}", [entity_id, *params]
            )
        )
        return any(entity_id_of(kind, key) == old_id for kind in LEDGER_TYPES for key in keys if key)

    # ---------- 出处 ----------

    def _evidence(self, table: str, column: str, item_id: str) -> tuple[list[dict[str, Any]], int, bool | None]:
        """一条东西看得到的证据：最多 5 条、说话时间新的在前；一共几条；是不是全都不是说话人自己的话（一条都没有是 None）。"""
        conditions, params = self.packet_conditions("p")
        base = (
            f" FROM {table} AS v JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE v.{column} = ? AND s.withdrawn_at IS NULL AND {' AND '.join(conditions)}"
        )
        total, said = self.conn.execute(
            f"SELECT count(*), coalesce(sum(s.origin = 'said'), 0){base}", [item_id, *params]
        ).fetchone()
        rows = self.conn.execute(
            f"SELECT {SENTENCE_COLUMNS}{base} ORDER BY s.said_at_epoch DESC, s.id DESC LIMIT ?",
            [item_id, *params, MAX_EVIDENCE],
        ).fetchall()
        evidence = [evidence_of(row, masked_fields(self.conn, row)) for row in rows]
        return evidence, int(total), (None if not total else not said)

    def earliest_said(self, table: str, column: str, item_id: str) -> tuple[int, str] | None:
        """看得到的证据里最早的说话时间（秒、原样的写法）：时间不知道时拿它排（5.3）。"""
        conditions, params = self.packet_conditions("p")
        row = self.conn.execute(
            f"SELECT s.said_at_epoch, s.said_at FROM {table} AS v"
            " JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE v.{column} = ? AND s.withdrawn_at IS NULL AND {' AND '.join(conditions)}"
            " ORDER BY s.said_at_epoch, s.id LIMIT 1",
            [item_id, *params],
        ).fetchone()
        return None if row is None else (int(row[0]), str(row[1]))

    # ---------- 每条结果 ----------

    @staticmethod
    def _status(item: dict[str, Any], status: str, reason: str | None) -> None:
        item["status"] = status
        if status == "pending":
            item["pending_reason"] = reason or "other"

    def fact_item(self, row: sqlite3.Row) -> dict[str, Any]:
        """一条事实写成结果（4.2）。row 是 facts 的一行。被替代的另带 superseded_by、superseded_at（D66）。"""
        evidence, total, secondhand = self._evidence("evidence", "fact_id", row["fact_id"])
        entities = self.fact_entities(row)
        item: dict[str, Any] = {"id": row["fact_id"], "kind": "fact", "content": self.shown_text(row["content"], entities)}
        self._status(item, row["status"], row["pending_reason"])
        item["valid_time"] = {"from": row["occurred_from"], "to": row["occurred_to"], "precision": row["time_precision"]}
        item["evidence"] = evidence
        item["evidence_total"] = total
        item["scope"] = _scope_json(row["scope_partitions"], row["scope_sensitive"])
        item["secondhand"] = bool(row["secondhand"]) if secondhand is None else secondhand
        fact: dict[str, Any] = {
            "subject": self.entity_ref(row["subject_entity_id"]),
            "attribute": self.shown_text(row["attribute"], entities),
            "value": self.shown_text(row["value"], entities),
            "frozen": bool(row["frozen"]),
        }
        superseded = self.superseding_fact(row)
        if superseded is not None:
            fact["superseded_by"], fact["superseded_at"] = superseded, row["superseded_at"]
        item["fact"] = fact
        return item

    def superseding_fact(self, row: sqlite3.Row) -> str | None:
        """替代它的那条（这个身份看得到的）；没有、看不到的是 None。"""
        if row["superseded_by"] is None:
            return None
        conditions, params = self.fact_conditions("g")
        found = self.conn.execute(
            f"SELECT 1 FROM facts AS g WHERE g.fact_id = ? AND {' AND '.join(conditions)}", [row["superseded_by"], *params]
        ).fetchone()
        return None if found is None else str(row["superseded_by"])

    def superseding_relation(self, row: sqlite3.Row) -> str | None:
        if row["superseded_by"] is None:
            return None
        conditions, params = self.relation_conditions("g")
        found = self.conn.execute(
            f"SELECT 1 FROM relations AS g WHERE g.relation_id = ? AND {' AND '.join(conditions)}",
            [row["superseded_by"], *params],
        ).fetchone()
        return None if found is None else str(row["superseded_by"])

    def replaced_relation(self, relation_id: str) -> str | None:
        """它替代了的那条（看得到的），有几条的取最近被替代的那一条。"""
        conditions, params = self.relation_conditions("g")
        row = self.conn.execute(
            f"SELECT g.relation_id FROM relations AS g WHERE g.superseded_by = ? AND {' AND '.join(conditions)}"
            " ORDER BY g.superseded_at DESC, g.relation_id DESC LIMIT 1",
            [relation_id, *params],
        ).fetchone()
        return None if row is None else str(row[0])

    def relation_state(self, row: sqlite3.Row) -> str:
        """现在的状态（4.2）：账本里的是写账那时候的。结束日子（按精度的整段）已经过去的，现在答 ended；被看得到的那条
        替代了的也是 ended。生效日子在将来的照样 active。"""
        state = str(row["state"])
        if state == "ended":
            return state
        if self.superseding_relation(row) is not None:
            return "ended"
        end = period(row["valid_to"], self.settings.timezone)
        if end is not None and end[1] <= self.as_of_epoch:
            return "ended"
        return state

    def relation_content(self, row: sqlite3.Row) -> str:
        """“主体 关系 客体”，亲属带具体说法；另带的字段、日子怎么来的写在后面的括号里（规矩题 5）。"""
        ends = [str(one) for one in (row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]) if one]
        kinship = self.shown_text(row["kinship"], ends) if row["kinship"] else None
        kind = f"{row['relation_type']}（{kinship}）" if kinship else str(row["relation_type"])
        text = f"{self.entity_name(row['subject_entity_id'])} {kind} {self.entity_name(row['object_entity_id'])}"
        details = []
        if row["org_entity_id"]:
            details.append(f"代表 {self.entity_name(row['org_entity_id'])}")
        details.extend(f"{label} {self.shown_text(row[column], ends)}" for column, label in _DETAILS if row[column])
        for column, label in (("valid_from_basis", "开始日子"), ("valid_to_basis", "结束日子")):
            if row[column] in _BASIS_TEXT and row[column.replace("_basis", "")]:
                details.append(f"{label}是{_BASIS_TEXT[row[column]]}的")
        return text + (f"（{'，'.join(details)}）" if details else "")

    def relation_item(self, row: sqlite3.Row) -> dict[str, Any]:
        """一条关系写成结果（4.2）。row 是 relations 的一行。"""
        evidence, total, secondhand = self._evidence("relation_evidence", "relation_id", row["relation_id"])
        item: dict[str, Any] = {"id": row["relation_id"], "kind": "relation", "content": self.relation_content(row)}
        self._status(item, row["status"], row["pending_reason"])
        if row["valid_from"]:
            precision = row["valid_from_precision"]
        elif row["valid_to"]:
            precision = row["valid_to_precision"]
        else:
            precision = "unknown"
        item["valid_time"] = {"from": row["valid_from"], "to": row["valid_to"], "precision": precision}
        item["evidence"] = evidence
        item["evidence_total"] = total
        item["scope"] = _scope_json(row["scope_partitions"], row["scope_sensitive"])
        item["secondhand"] = bool(row["secondhand"]) if secondhand is None else secondhand
        state = self.relation_state(row)
        ends = [str(one) for one in (row["subject_entity_id"], row["object_entity_id"], row["org_entity_id"]) if one]
        item["relation"] = {
            "subject": self.entity_ref(row["subject_entity_id"]),
            "type": row["relation_type"],
            "object": self.entity_ref(row["object_entity_id"]),
            "state": state,
            # 暂停条件是软变化判定写的字，和 L3 写的一样可能带着看不到的叫法（复审 D）
            "pause_condition": self.shown_text(row["pause_condition"], ends) if state == "paused" else None,
            "replaces": self.replaced_relation(row["relation_id"]),
            "superseded_by": self.superseding_relation(row),
        }
        return item

    def alias_item(self, row: sqlite3.Row) -> dict[str, Any]:
        """一个别名写成结果（kind alias）：生效时间不知道；可见范围是看得到的出处所在的包合起来。"""
        evidence, total, secondhand = self._evidence("evidence", "alias_id", row["alias_id"])
        conditions, params = self.packet_conditions("p")
        partitions: set[str] = set()
        sensitive: set[str] = set()
        for scope in self.conn.execute(
            "SELECT DISTINCT p.partitions, p.sensitive FROM evidence AS v"
            " JOIN raw_sentences AS s ON s.sentence_id = v.sentence_id AND s.revision = v.revision"
            " JOIN packets AS p ON p.packet_id = s.packet_id AND p.revision = s.revision"
            f" WHERE v.alias_id = ? AND s.withdrawn_at IS NULL AND {' AND '.join(conditions)}",
            [row["alias_id"], *params],
        ):
            partitions.update(json.loads(scope[0]))
            sensitive.update(json.loads(scope[1]))
        item: dict[str, Any] = {"id": row["alias_id"], "kind": "alias", "content": row["alias"]}
        self._status(item, row["status"], None)
        item["valid_time"] = {"from": None, "to": None, "precision": "unknown"}
        item["evidence"] = evidence
        item["evidence_total"] = total
        item["scope"] = {"partitions": sorted(partitions), "sensitive": sorted(sensitive)}
        item["secondhand"] = bool(secondhand)
        return item

    def aliases_of(self, entity_id: str) -> list[sqlite3.Row]:
        """这个实体看得到的别名，最早说到的在前。"""
        alias_sql, params = self._alias_visible_sql("a")
        rows = self.conn.execute(
            f"SELECT a.* FROM aliases AS a WHERE a.entity_id = ? AND {alias_sql} ORDER BY a.alias", [entity_id, *params]
        ).fetchall()
        first = {row["alias_id"]: (self.earliest_said("evidence", "alias_id", row["alias_id"]) or (0, ""))[0] for row in rows}
        return sorted(rows, key=lambda row: (first[row["alias_id"]], row["alias"]))


def said_epoch(text: object) -> int | None:
    moment = parse_aware(text)
    return None if moment is None else epoch(moment)
