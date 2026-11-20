"""`javmem gold review`（U3，D141 ③）：把一份金标准（比如 `javmem gold prelabel` 出的草稿）做成一页本机打开的审阅页面。

- 一条一张选择卡：对、错、改（错、改的写一句为什么、改成什么）；每个单元最后一个“补一条”，写漏了的。
  “存下来”在浏览器里下载一份 `审阅.json`，就是打分认的审阅结果（docs/gold-format.md 第 5 节）：
  `javmem score --gold 目录 --review 审阅.json …`。改了的、补的只有一句话的，打分时先单列、不算分，等改成结构再算。
- 页面是一个 HTML 文件，样式、脚本都写在里面，不连外面、不拉别的文件；用浏览器直接打开就行。审到一半关了，
  浏览器里记着进度（只在这台电脑的这个浏览器里）；给了 `--review`（上一次存下的）的，页面照它先选好，接着审。
- 设了数据目录的，卡片上带着出处那几句原话（从账本只读取）；没设的只有句子编号。
- 页面里有原话：输出放仓库外。卡片上的字一律照文字写进页面（textContent），不当 HTML。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

TYPE_NAMES = {"fact": "事实", "relation": "关系", "change": "变化", "alias": "别名"}


def _embed(value: object) -> str:
    """放进 <script type="application/json"> 的 JSON：</ 写成 <\\/，页面里的字截不断脚本。"""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def page_data(units: Sequence[Mapping[str, object]], sentences: Mapping[str, str]) -> list[dict[str, object]]:
    """页面要的：每个单元的编号、标题，每条的编号、种类、内容、要先看的标记、出处（句子编号和原话）。"""
    out = []
    for unit in units:
        rows = []
        for item in unit.get("items") or []:  # type: ignore[union-attr]
            if not isinstance(item, dict):
                continue
            details = []
            subject = item.get("subject") or {}
            if isinstance(subject, dict) and subject.get("name"):
                details.append(f"主体：{subject.get('name')}（{subject.get('type')}）")
            if item.get("type") == "relation":
                obj = item.get("object") or {}
                details.append(f"关系：{item.get('relation')} → {obj.get('name') if isinstance(obj, dict) else obj}；状态：{item.get('state')}")
            if item.get("type") == "fact" and (item.get("attribute") or item.get("value")):
                details.append(f"属性：{item.get('attribute')}；值：{item.get('value')}")
            when = item.get("when") or {}
            if isinstance(when, dict) and (when.get("from") or when.get("to")):
                details.append(f"时间：{when.get('from') or '?'} 到 {when.get('to') or '现在'}（{when.get('precision')}）")
            evidence = [{"sid": str(one.get("sid")), "text": sentences.get(str(one.get("sid")), ""), "quote": str(one.get("quote") or ""),
                         "role": str(one.get("role") or "")}
                        for one in item.get("evidence") or [] if isinstance(one, dict)]
            look = [word for word, flag in (("拿不准", item.get("unsure")), ("影响大", item.get("important"))) if flag]
            if item.get("note"):
                look.append(f"备注：{item.get('note')}")
            rows.append({"id": str(item.get("id")), "kind": TYPE_NAMES.get(str(item.get("type")), str(item.get("type"))),
                          "content": str(item.get("content") or ""), "details": details, "look": look, "evidence": evidence})
        out.append({"unit": str(unit.get("unit")), "title": str(unit.get("title") or ""), "items": rows})
    return out


def build_page(units: Sequence[Mapping[str, object]], sentences: Mapping[str, str], earlier: Mapping[str, object] | None) -> str:
    data = page_data(units, sentences)
    total = sum(len(unit["items"]) for unit in data)  # type: ignore[arg-type]
    return PAGE.replace("@@TOTAL@@", str(total)).replace("@@DATA@@", _embed(data)).replace(
        "@@EARLIER@@", _embed(earlier or {"reviews": {}, "additions": []}))


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>金标准审阅</title>
<style>
:root { --bg: #fbfaf7; --fg: #1f2328; --muted: #5d6470; --card: #ffffff; --line: #d9dde3; --ok: #1a7f37; --bad: #cf222e; --fix: #9a6700; }
@media (prefers-color-scheme: dark) { :root { --bg: #161a1f; --fg: #e6e8eb; --muted: #9aa3ad; --card: #1f252c; --line: #343c46; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.6 system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; }
header { position: sticky; top: 0; background: var(--bg); border-bottom: 1px solid var(--line); padding: 12px 16px; z-index: 1; }
main { max-width: 860px; margin: 0 auto; padding: 16px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 28px 0 8px; }
.muted { color: var(--muted); font-size: 14px; }
.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
button, .choice label { font: inherit; border: 1px solid var(--line); background: var(--card); color: var(--fg); border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button.primary { background: var(--fg); color: var(--bg); }
article { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 10px 0; }
article.done-ok { border-left: 5px solid var(--ok); } article.done-wrong { border-left: 5px solid var(--bad); } article.done-fix { border-left: 5px solid var(--fix); }
.id { font-family: ui-monospace, Consolas, monospace; font-size: 13px; color: var(--muted); }
.content { font-size: 17px; margin: 4px 0; }
.look { color: var(--fix); font-size: 14px; }
blockquote { margin: 6px 0; padding: 4px 10px; border-left: 3px solid var(--line); color: var(--muted); font-size: 14px; }
.choice { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.choice input { position: absolute; opacity: 0; }
.choice input:checked + span { font-weight: 700; text-decoration: underline; }
textarea { width: 100%; box-sizing: border-box; margin-top: 8px; font: inherit; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; background: var(--bg); color: var(--fg); }
.hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>金标准审阅</h1>
  <div class="muted">一共 @@TOTAL@@ 条。每条点“对”“错”“改”；错的、改的写一句为什么、改成什么。漏了的在每个单元最后补。审完点“存下来”，下载的 审阅.json 交给 javmem score --review。</div>
  <div class="bar">
    <span id="progress" class="muted"></span>
    <button type="button" data-show="all">全部</button>
    <button type="button" data-show="todo">只看没审的</button>
    <button type="button" data-show="look">只看要先看的</button>
    <button type="button" class="primary" id="save">存下来</button>
  </div>
</header>
<main id="units"></main>
<script type="application/json" id="data">@@DATA@@</script>
<script type="application/json" id="earlier">@@EARLIER@@</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("data").textContent);
  var earlier = JSON.parse(document.getElementById("earlier").textContent);
  var KEY = "javmem-gold-review:" + data.map(function (u) { return u.unit; }).join(",");
  var state = { reviews: earlier.reviews || {}, additions: earlier.additions || [] };
  try { var kept = JSON.parse(localStorage.getItem(KEY) || "null"); if (kept && kept.reviews) { state = kept; } } catch (e) { /* 浏览器不让存：照样能审 */ }
  function keep() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* 存不了不要紧 */ } }
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) { n.className = cls; } if (text !== undefined) { n.textContent = text; } return n; }
  var root = document.getElementById("units");
  var total = 0;
  data.forEach(function (unit) {
    var section = el("section"); section.appendChild(el("h2", "", unit.unit + "　" + unit.title)); root.appendChild(section);
    unit.items.forEach(function (card) {
      total += 1;
      var box = el("article"); box.dataset.id = card.id; box.dataset.look = card.look.length ? "1" : "";
      box.appendChild(el("div", "id", card.id + "　" + card.kind));
      box.appendChild(el("div", "content", card.content));
      card.details.forEach(function (line) { box.appendChild(el("div", "muted", line)); });
      if (card.look.length) { box.appendChild(el("div", "look", "要先看：" + card.look.join("；"))); }
      card.evidence.forEach(function (one) {
        var q = el("blockquote", "", (one.role ? "（" + (one.role === "before" ? "变以前" : "变以后") + "）" : "") + (one.text || "（没有数据目录，看不到原话）"));
        q.appendChild(el("div", "id", one.sid)); box.appendChild(q);
      });
      var choice = el("div", "choice");
      [["ok", "对"], ["wrong", "错"], ["fix", "改"]].forEach(function (pair) {
        var label = el("label"); var input = el("input"); input.type = "radio"; input.name = "v-" + card.id; input.value = pair[0];
        label.appendChild(input); label.appendChild(el("span", "", pair[1])); choice.appendChild(label);
        input.addEventListener("change", function () { set(card.id, pair[0]); });
      });
      box.appendChild(choice);
      var text = el("textarea", "hidden"); text.rows = 2; text.placeholder = "为什么不对、改成什么";
      text.addEventListener("input", function () { var r = state.reviews[card.id]; if (r) { r.text = text.value; keep(); } });
      box.appendChild(text);
      section.appendChild(box);
      var r = state.reviews[card.id];
      if (r) { var picked = box.querySelector('input[value="' + r.v + '"]'); if (picked) { picked.checked = true; } text.value = r.text || ""; paint(box, r.v); }
    });
    var add = el("article"); add.appendChild(el("div", "content", "补一条（这个单元里漏了的）"));
    var addText = el("textarea"); addText.rows = 2; addText.placeholder = "比如：漏了一条，谁在哪天说了什么"; add.appendChild(addText);
    var addButton = el("button", "", "加上"); addButton.type = "button"; add.appendChild(addButton);
    var list = el("ul"); add.appendChild(list); section.appendChild(add);
    function showAdded() { list.textContent = ""; state.additions.filter(function (a) { return a.unit === unit.unit; }).forEach(function (a) { list.appendChild(el("li", "", a.text || "（带结构的一条）")); }); }
    addButton.addEventListener("click", function () { var t = addText.value.trim(); if (!t) { return; } state.additions.push({ unit: unit.unit, text: t }); addText.value = ""; keep(); showAdded(); });
    showAdded();
  });
  function paint(box, v) {
    box.classList.remove("done-ok", "done-wrong", "done-fix"); box.classList.add("done-" + v);
    box.querySelector("textarea").classList.toggle("hidden", v === "ok");
  }
  function set(id, v) {
    var r = state.reviews[id] || {}; r.v = v; state.reviews[id] = r; keep();
    paint(document.querySelector('article[data-id="' + id + '"]'), v); progress();
  }
  function progress() { document.getElementById("progress").textContent = "审了 " + Object.keys(state.reviews).length + " / " + total + " 条"; }
  progress();
  document.querySelectorAll("[data-show]").forEach(function (button) {
    button.addEventListener("click", function () {
      var how = button.dataset.show;
      document.querySelectorAll("article[data-id]").forEach(function (box) {
        var hide = (how === "todo" && state.reviews[box.dataset.id]) || (how === "look" && !box.dataset.look);
        box.classList.toggle("hidden", Boolean(hide));
      });
    });
  });
  document.getElementById("save").addEventListener("click", function () {
    var blob = new Blob([JSON.stringify({ reviews: state.reviews, additions: state.additions }, null, 1)], { type: "application/json" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "审阅.json"; document.body.appendChild(a); a.click(); a.remove();
  });
})();
</script>
</body>
</html>
"""
