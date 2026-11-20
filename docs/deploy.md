# 部署

真用起来（不是开发、不是演示）时，javmem 要怎么摆、谁能碰什么、按时要跑什么。安全上做到哪、删到哪见 [security.md](security.md)，命令和配置的细节见 [development.md](development.md)，两个接口见 [packet-spec.md](packet-spec.md)、[query-api.md](query-api.md)。下面的用户名、目录、身份都是编的，照你的改。

## 1. 一套只放一个主人

- javmem 一套（一个数据目录、一个查询服务、一张主图）只放一个主人的记忆。几位主人就几套：数据目录、配置、主图、系统用户、端口各是各的。
- 一套里面，正式库（`prod`）和测试库（`test`）各是一本账；一个查询身份只对一个库。身份按分区、敏感标记过滤，这是一个主人内部“哪个 Bot 能看什么”，不是几个主人之间的隔离。
- 不要让几套共用 L3 结果库、向量缓存（`--store`、`--vectors` 指到同一处）：结果库按包号认窗口，两家包号一样就混；向量缓存里存着原文的字。
- 主图里放着防串库的标记：一张图只接一个数据目录的一个库，认不出来的一个字不写。一台机器上起第二套主图，要另起一个 Neo4j：拷一份 `deploy/graph-neo4j/compose.yaml`，改掉四处（最上面的 `name`、`container_name`、两个本机端口、数据目录那个环境变量的名字），第二套的 javmem 把 `JAVMEM_GRAPH_NEO4J_URI` 指到新的 bolt 端口、密码另设一个。原来那份 compose 别改：删除演练、回归跑分照它认容器。

## 2. 每个来源一个系统用户

只有来源登记表（`JAVMEM_SOURCES_FILE`）里的来源能投包，收件时核对目录和包头（packet-spec 第 11 节）。这挡得住投错，挡不住故意冒充：要防冒充，每个来源（每个打包方、每个 Bot）用自己的系统用户跑，只能写自己的收件目录。几个打包方跑在同一个系统用户下，谁都能写谁的目录，这时目录分开只防手误。

| 目录 | 谁能写 | 谁能读 |
| --- | --- | --- |
| `inbox/<来源>/` 和它下面的 `.tmp/`（测试的是 `inbox-test/<来源>/`） | 这个来源、javmem | 这个来源、javmem |
| `failed/<来源>/`（收不了的包和回执） | javmem | 这个来源、javmem |
| 数据目录别的地方（账本、原文、结果库、向量） | javmem | javmem |

例子（Linux；每个来源一个用户、一个同名的组，javmem 的用户在这些组里）：

```bash
sudo install -d -o desk-bot -g javmem -m 2770 /srv/javmem/data/inbox/desk-bot /srv/javmem/data/inbox/desk-bot/.tmp
sudo install -d -o javmem -g desk-bot -m 2750 /srv/javmem/data/failed/desk-bot
```

例子（Windows；先在一个试的目录上试；去掉继承以后只剩这里写的几个，管理员、SYSTEM 要的话一起写上）：

```powershell
icacls Q:\javmem-data\inbox\desk-bot /inheritance:r /grant:r "javmem-svc:(OI)(CI)M" "desk-bot-svc:(OI)(CI)M" "SYSTEM:(OI)(CI)F"
icacls Q:\javmem-data\failed\desk-bot /inheritance:r /grant:r "javmem-svc:(OI)(CI)M" "desk-bot-svc:(OI)(CI)RX" "SYSTEM:(OI)(CI)F"
```

新来源接入的几步（先投测试目录、连着 3 天没有失败包再改正式）见 packet-spec 第 11 节。

**别用 root 跑**（javmem、各个打包方都是）：root 不受文件权限管，上面这张表对它不起作用，归档设成只读也拦不住它改。在容器里跑的也一样，给容器指定一个普通用户。

## 3. 查询服务和身份

- `javmem serve` 走 stdio，不开端口。Bot 的 MCP 客户端起它，身份写在它的环境变量 `JAVMEM_CALLER` 里；一个 Bot 一个进程、一个身份。谁能看什么写在 `JAVMEM_ACCESS_FILE`，每次调用都按最新的算，改了马上生效；身份不认识、配置读不了，一律拒。
- **信任边界：** stdio 的查询服务跑在 Bot 的系统用户下。Bot 要是改得了自己的 MCP 配置，就能换一个身份；它读得到的数据目录，查询服务读得到，Bot 自己也读得到，绕得过所有过滤。所以现在这一版只防“配错了”，不防“Bot 自己要越权”：只给你信得过的 Bot 用，MCP 配置、身份配置由部署的人写、Bot 改不了。不信得过的 Bot 要等查询服务能跑在 javmem 自己的用户下（走本机的网络连接、每个身份一个令牌），现在没有。
- 返回给 Bot 的实体编号是照名字算的（没加盐），拿到编号的能离线一个个试名字。在上面说的信任边界里 Bot 本来就读得到账本，所以这一版不处理，等查询服务能单独跑在 javmem 的用户下时一起改。

## 4. 正式写账要设主人的名字

`JAVMEM_OWNER_FILE` 指一份 YAML：`names: [中文名, 英文名]`。抽取时模型有时直接写主人的名字，写账时照这份挂到“主人”上；不设的话，这些事实会挂到一个同名的人身上。真实的配置都放仓库外。

## 5. 按时要跑的

javmem 自己不注册计划任务。下面这些由部署的人按时跑（Windows 用任务计划程序，Linux 用 cron 或 systemd 的定时器）；跑的那个系统用户要能读到 javmem 的环境变量（密钥放在那个用户的环境变量里，不写进任务的命令行）。

| 多久 | 跑什么 | 做什么 |
| --- | --- | --- |
| 打包方投了包以后 | `javmem intake`，再 `javmem extract --library prod`、`javmem write --library prod` | 收件、抽取（调模型，花钱）、写账 |
| 每天 | `javmem review --remind` | 待确认的数量提醒；建了 30 天还没人管的先关掉 |
| 每天 | `javmem patrol` | 巡检：太久没收包、待确认太多、磁盘快满、主图落后、备份太久没成功这类，有情况就提醒 |
| 每天 | `javmem backup --to <暂存目录> --push <推异地的程序>` | 备份（第 9 节） |
| 每周 | `javmem versions --check --only probes,served,models` | 服务商实际给的模型、探针的回答有没有变 |
| 每月 | `javmem restore-check`（恢复演练）、`javmem drill delete`（删除演练，在拷贝上做） | 备份恢复得回来、撤回删得干净 |
| 换提示词、换模型的时候 | `javmem drill poison`（投毒演练）、`javmem regress`（自己的套件） | 夹带指令的包不起作用；分数不降 |

例子（Windows，每天 8:30 的数量提醒）：

```powershell
schtasks /Create /TN "javmem\review-remind" /SC DAILY /ST 08:30 /TR "Q:\javmem\.venv\Scripts\javmem.exe review --remind"
```

例子（Linux，crontab）：

```bash
30 8 * * * /srv/javmem/.venv/bin/javmem review --remind >> /srv/javmem/logs/remind.log 2>&1
```

## 6. Windows 上读附件

附件里的 PDF、Excel、Word 在子进程里读，到点就停（一个文件 60 秒），每个子进程最多用 2 GB 内存，防做坏的压缩文件把内存吃光：Linux、macOS 上子进程自己限，Windows 上 javmem 起来以后把它放进一个作业对象限，不用另外配。javmem 自己要是已经跑在一个不让嵌套的作业对象里（少见），放不进去就照旧读、日志里记一行警告。

## 7. 升级

- 所有 javmem 的进程一起停、一起换、一起起：新代码一打开账本就升账本的版本，还开着的旧查询服务以后都报 `unavailable`。
- 升级以后存一次版本基准：`javmem versions --save`（存进数据目录的 `logs/versions-baseline.json`）。没存的话巡检每回都说少一样没查。
- 抽取、写账的规则升了版，巡检报 `rules_mixed`（账本里几版规则混着写）：当晚整本重建（不调模型，拿结果库重写，见开发说明）。
- 升级前后各跑一遍 `javmem regress`（拿你自己的套件），分数不降才留。

## 8. 主图（Neo4j，可选）

- 用的是 Neo4j 5.26 社区版，`deploy/graph-neo4j/compose.yaml` 钉着镜像的指纹，只用本机已有的镜像、不自己去拉。先照指纹拉一次：

  ```bash
  docker pull public.ecr.aws/docker/library/neo4j:5.26-community@sha256:eaee4488d580749ce332bd028f5be3917edf91f562d76c5443a51c08ec347b1f
  ```

- 起：设好 `JAVMEM_GRAPH_NEO4J_DATA`（仓库外的数据目录），`docker compose -f deploy/graph-neo4j/compose.yaml up -d`。端口只绑本机。第一次起来是默认账号，`javmem graph init --library prod` 用 bolt 改成 `JAVMEM_GRAPH_NEO4J_PASSWORD` 里的密码（密码只放环境变量）。使用数据的上报在 compose 里关着。
- **地址只认本机的：** `JAVMEM_GRAPH_NEO4J_URI`、`JAVMEM_GRAPH_TEST_NEO4J_URI` 要写成 `bolt://127.0.0.1:端口` 这样（`localhost`、`::1` 也行），别的机器、别的容器的名字一律不连。Neo4j 放在另一个容器里的，两种办法：把端口映射到本机、只绑 127.0.0.1（compose 里就是这样）；或者让它和跑 javmem 的容器共用网络空间（`docker run --network container:<javmem 的容器> …`，不给端口），javmem 那边照样写 `bolt://127.0.0.1:7687`。
- **拿演示的数据试主图：** 演示的账本用的是假向量，`javmem graph sync`、`javmem graph check` 都要带 `--fake-embeddings`（测试库：`--library test`，图的地址、密码写在 `JAVMEM_GRAPH_TEST_NEO4J_URI`、`JAVMEM_GRAPH_TEST_NEO4J_PASSWORD`）。不带的话，同步报“关系的向量欠着”，核对报“两边不一样”。
- 内存：compose 里给的是堆 1G、页缓存 512M、容器上限 4G，几万条事实够用。
- 主图是账本的投影，坏了不用恢复，从账本整张重建；所以不备份主图。
- 彻底删除以后，社区版的存储文件、事务日志里可能还留着删掉的东西：要干净就换一张新图（停容器、删它的数据目录、重起、`graph init`、整张同步）。

## 9. 备份和恢复

- `javmem backup --to <暂存目录> --push <推异地的程序>`：拿锁，SQLite 照在线备份拷，归档、配置照内容拷，记一份带 sha256 的清单，再交给你的程序去加密、推到另一台机器或另一块盘、留几份。暂存目录要在仓库外、不在数据目录里。
- 推异地的程序你自己定。比如 restic：`restic -r <备份仓库> backup <暂存目录>`，密码用 restic 的 `--password-command` 取，不写明文。
- 恢复：从异地取回一份，`javmem restore-check --from <取回来的> --to <新目录> --withdrawals <最新的撤回清单>`：核清单、恢复、照撤回清单再删一遍、和备份时的巡检比；过了再把 `JAVMEM_DATA_DIR` 指过去，主图照第 8 节换一张新图、整张同步。
- 彻底删除和旧备份：删掉的东西在旧备份里还在。恢复时会照撤回清单再删一遍；旧备份到期才真没了；真要“今天就删干净”，手动删掉那天以前的备份（历史版本也就没了）。
- 每月做一次恢复演练，接着在恢复出来的那一份上做删除演练。

## 10. 什么会发到外面

- 抽取（L3）、模型复核，开了的话还有关系的判断、软变化判定，会把窗口里的原文发给你配的模型。包头写 `privacy: private` 的包不发；没配本机的模型，它只进原文层、不抽取。
- 事实的文字会发给你配的向量服务（可以是本机的）。
- `javmem quiz`（可选，你自己跑的时候）：题、标准答案、照题查到的条目（打过码；private 的包里的不发）发给你配的模型。
- 发出去的内容归模型服务商的条款管（留多久、拿不拿去训练）：javmem 删不到那边。
- javmem 自己不往外报使用数据；compose 里 Neo4j 的上报关着；Graphiti 支线（可选）的统计关着，只连本机。
