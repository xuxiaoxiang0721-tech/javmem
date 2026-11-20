# javmem

一个独立的记忆模块：**记忆包进，查询接口出**，不依赖任何 Bot 框架。Bot 只是它的用户：把聊天记录、邮件打成记忆包投进来，要用的时候通过查询接口（MCP）问。换 Bot 框架不用动它。

## 它做什么

- **原文是唯一的真相。** 记忆包的原文归档不动；事实、关系、图、向量都从原文派生，随时能删掉重建。
- **一条处理线：** 规整、切窗口、用大模型抽出事实和关系、逐条核对（证据要在原文里找得到）、合并和判新旧、写进账本。
- **一本账：** SQLite。每条事实、关系都带着谁说的、什么时候说的、出自哪一句；被后来的说法替代的照样留着，能问“那时候是什么”。
- **一张图（可选）：** Neo4j，是账本的投影，能从账本整张重建；不开也能用。
- **五个只读的查询工具（MCP）：** `memory_search`、`entity_card`、`timeline`、`relation_at`、`raw_search`。每次调用都带身份，按分区、敏感标记过滤；看不到的和不存在的长得一样。
- **管理：** 撤回（按包或单条）、彻底删除、冻结、待确认、备份和恢复、巡检、回归跑分、删除演练和投毒演练。

两个接口已经定稿：写的一侧是 [docs/packet-spec.md](docs/packet-spec.md)（记忆包规范 `javmem-packet/1`），读的一侧是 [docs/query-api.md](docs/query-api.md)。还没发布到包仓库，照下面从仓库装。

## 装

要 Python 3.12 以上。照锁文件装：每个包的版本、每个文件的 sha256 都钉死，装出来每次一样。

Windows（PowerShell）：

```powershell
git clone <这个仓库的地址> javmem
cd javmem
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes --no-deps -r requirements\main.txt
.venv\Scripts\python -m pip install --no-deps --no-build-isolation -e .
```

Linux（macOS 应该也行，没测过）：

```bash
git clone <这个仓库的地址> javmem
cd javmem
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements/main.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

要跑测试的把 `main.txt` 换成 `dev.txt`（多了 pytest）。用 Claude 判软变化、跑 Graphiti 支线的另有两份锁文件，见 [docs/development.md](docs/development.md)。

## 先看一遍演示（不要密钥）

```powershell
.venv\Scripts\javmem demo --to $env:TEMP\javmem-demo
```

（Linux 上是 `.venv/bin/javmem demo --to /tmp/javmem-demo`。`--to` 要是仓库外一个新的空目录。）

用一套编的数据走一整圈：一个编的主人、一个编的 Bot、5 个对话包（报价改了、开幕推迟、负责人换了这类）、28 条编的金标准，加上第一次用真模型抽这几个包时录下的回复。收包、写账、五个查询工具各问一句、拿金标准打分、不用金标准的四个数（`javmem selfcheck`）、撤回一个包再问、彻底删除。不调模型、不连外面，一两分钟走完；每一步的命令和输出在那个目录的 `steps.log`。

## 自己试一试

演示做完，那个目录里就是一套能接着用的数据。设好环境变量就能自己问（身份 `mini-owner` 看得到全部）：

```powershell
$d = "$env:TEMP\javmem-demo"
$env:JAVMEM_DATA_DIR = "$d\data"
$env:JAVMEM_SOURCES_FILE = "$d\config\sources.yaml"
$env:JAVMEM_ACCESS_FILE = "$d\config\access.yaml"
$env:JAVMEM_OWNER_FILE = "$d\config\owner.yaml"
$env:JAVMEM_LEXICON_FILE = "$d\config\lexicon.yaml"
$env:JAVMEM_TIMEZONE = "+08:00"
$env:JAVMEM_GRAPH_SEARCH = "off"
$env:JAVMEM_CALLER = "mini-owner"
.venv\Scripts\javmem query memory_search '{\"query\": \"晨光一号基金放在哪家银行？\"}'
```

PowerShell 5.1（Windows 自带）交参数给程序时不转义里面的双引号，所以 JSON 里的 `"` 要写成 `\"`；PowerShell 7.3 以上和 bash 里照常写：

```bash
d=/tmp/javmem-demo
export JAVMEM_DATA_DIR=$d/data JAVMEM_SOURCES_FILE=$d/config/sources.yaml JAVMEM_ACCESS_FILE=$d/config/access.yaml \
  JAVMEM_OWNER_FILE=$d/config/owner.yaml JAVMEM_LEXICON_FILE=$d/config/lexicon.yaml JAVMEM_TIMEZONE=+08:00 \
  JAVMEM_GRAPH_SEARCH=off JAVMEM_CALLER=mini-owner
.venv/bin/javmem query memory_search '{"query": "晨光一号基金放在哪家银行？"}'
```

五个工具的入参和返回见 [docs/query-api.md](docs/query-api.md)。

想连主图也试一下的，照 [docs/deploy.md](docs/deploy.md) 第 8 节起一个 Neo4j（地址只认本机的），`javmem graph init --library test`、`javmem graph sync --library test --fake-embeddings`，再把 `JAVMEM_GRAPH_SEARCH` 设成 `on` 问。演示的账本用的是假向量，`graph sync`、`graph check` 都要带 `--fake-embeddings`。

## 真用起来

1. **配置**（真实的配置放仓库外，格式照演示目录里 `config/` 那几份）：来源登记 `JAVMEM_SOURCES_FILE`（谁能投包）、身份 `JAVMEM_ACCESS_FILE`（谁能看什么）、主人的名字 `JAVMEM_OWNER_FILE`（正式写账要设）、别名表 `JAVMEM_LEXICON_FILE`（可选）、时区 `JAVMEM_TIMEZONE`、数据目录 `JAVMEM_DATA_DIR`。
2. **模型：** 抽取要一个 OpenAI 兼容的接口（`JAVMEM_LLM_BASE_URL`、`JAVMEM_LLM_API_KEY`、`JAVMEM_LLM_MODEL`），事实的向量要一个 OpenAI 兼容的 `/embeddings`（`JAVMEM_EMBEDDING_BASE_URL` 这几样，可以是本机的服务）。密钥只放环境变量，javmem 不打印、不写文件。提示词的版本和别的设置见开发说明“配置”那一节；换模型、换提示词以前，先拿自己的数据量一遍。
3. **跑：**

   ```bash
   javmem init                          # 建数据目录、账本、来源目录
   # 打包方把记忆包投进 <数据目录>/inbox/<来源>/（试跑的投 inbox-test/<来源>/）
   javmem intake                        # 收件：校验、去重、登记、归档
   javmem extract --library prod --dry-run   # 先看要调几个窗口、多少字（不调模型）
   javmem extract --library prod        # 抽取（调模型，花钱）
   javmem write --library prod          # 核对、合并、写账、算向量
   javmem serve                         # 查询服务（MCP，stdio），由 Bot 的 MCP 客户端起
   ```

4. **主图（可选）：** 照 [docs/deploy.md](docs/deploy.md) 第 8 节：照指纹拉 Neo4j 的镜像，`docker compose` 起，`javmem graph init`。
5. **部署、按时要跑的**（每天的数量提醒、巡检、备份，每月的恢复演练、删除演练）都在 [docs/deploy.md](docs/deploy.md)。

## 接 Bot

Bot 的 MCP 客户端起 `javmem serve`（stdio，不开端口），身份写在给它的环境变量 `JAVMEM_CALLER` 里，一个 Bot 一个身份。比如（路径、身份都是编的）：

```json
{
  "mcpServers": {
    "javmem": {
      "command": "/srv/javmem/.venv/bin/javmem",
      "args": ["serve"],
      "env": {
        "JAVMEM_DATA_DIR": "/srv/javmem/data",
        "JAVMEM_ACCESS_FILE": "/srv/javmem/config/access.yaml",
        "JAVMEM_LEXICON_FILE": "/srv/javmem/config/lexicon.yaml",
        "JAVMEM_TIMEZONE": "+08:00",
        "JAVMEM_CALLER": "desk-bot"
      }
    }
  }
}
```

**身份靠部署：** stdio 的查询服务跑在 Bot 的系统用户下，Bot 要是改得了自己的 MCP 配置、读得到数据目录，就绕得过身份。现在这一版只给信得过的 Bot 用，见 [docs/security.md](docs/security.md) 第 1 节。

## 数据往哪去

- 原文、账本、结果库、向量缓存都在你自己的数据目录里。
- 抽取、核对时，窗口里的原文会发给你配的模型；包头写 `privacy: private` 的包不发，没配本机的模型就只进原文层。
- javmem 自己不往外报使用数据；compose 里 Neo4j 的使用数据上报也关着。
- 撤回、彻底删除管到原文层、账本、结果库、向量缓存、软变化判定和主图；删不到的（已经发给模型的、旧备份里的、Neo4j 磁盘上的残留）怎么办，见 [docs/security.md](docs/security.md) 第 4、5 节。

## 文档

- [docs/packet-spec.md](docs/packet-spec.md)：写接口，记忆包规范。
- [docs/query-api.md](docs/query-api.md)：读接口，五个查询工具。
- [docs/deploy.md](docs/deploy.md)：部署，谁能碰什么，按时要跑的。
- [docs/security.md](docs/security.md)：安全：信任边界、一套一个主人、删到哪、哪些删不到。
- [docs/calibration.md](docs/calibration.md)：校准：在自己的数据上量准 javmem 的先后（演示、金标准、不用金标准的四个数、拿题量）。
- [docs/definitions.md](docs/definitions.md)：定义卡：事实、关系、变化、别名和七类实体，各算什么、不算什么。
- [docs/gold-format.md](docs/gold-format.md)：金标准的格式。
- [docs/questions.md](docs/questions.md)：自己的题怎么写、怎么拿 `javmem regress` 量证据命中率。
- [docs/development.md](docs/development.md)：开发说明：配置、命令、处理线每一层怎么做的、测试。

## 许可证

MIT，见 [LICENSE](LICENSE)。主图用的 Neo4j 社区版是 GPLv3：它是一个单独的服务，镜像由你自己拉，javmem 只通过它的协议连它，不带它的代码。每个依赖的许可证见 [docs/licenses.md](docs/licenses.md)。

作者的 X：[@eric_0721](https://x.com/eric_0721)
