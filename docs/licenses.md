# 许可证

## javmem 自己

MIT，见仓库根目录的 [LICENSE](../LICENSE)。

## Neo4j（主图，可选）

- 主图、Graphiti 支线的图用的是 **Neo4j 5.26 社区版**，社区版的许可证是 **GPLv3**。没用企业版：没有企业版的镜像、不同意企业版的许可、没装插件（APOC、GDS 都没有）；中文全文索引用的是它自带的 `cjk` 分析器。在线备份是企业版才有的，所以 javmem 的备份走账本（主图坏了从账本整张重建）。
- javmem 只通过 Bolt 协议连它，用的是官方驱动 `neo4j`（Python 包，Apache-2.0、Python-2.0）。仓库里只有 compose 文件指着官方镜像（钉着指纹），不带 Neo4j 的代码、不打包它：镜像由用户自己拉，Neo4j 的许可证管 Neo4j，javmem 照样是 MIT。
- **别发布把 Neo4j 打包进去的镜像或者安装包**：那样就是在分发 Neo4j，得照 GPLv3 给出它的源码、带上它的许可证。
- Neo4j 是 Neo4j 公司的商标，这里的文档只是提它的名字，和 Neo4j 公司没有关系。

## Python 依赖

都照 `requirements/` 下的锁文件装（版本、每个文件的 sha256 都钉死）；下面只写许可证，版本以锁文件为准。`tests/test_license.py` 钉着：装着的运行时、开发用的包，许可证都要是下面这一类（GPL 一类的、认不出的都红），锁文件里的每个包这一页都要列着。

### 运行时（`requirements/main.txt`）

| 包 | 许可证 |
| --- | --- |
| `cffi` | MIT-0 |
| `charset-normalizer` | MIT |
| `cryptography` | Apache-2.0 或 BSD-3-Clause |
| `et-xmlfile` | MIT |
| `jieba` | MIT |
| `lxml` | BSD-3-Clause |
| `neo4j` | Apache-2.0 和 Python-2.0 |
| `openpyxl` | MIT |
| `pdfminer-six` | MIT |
| `pycparser` | BSD-3-Clause |
| `python-docx` | MIT |
| `pytz` | MIT |
| `pyyaml` | MIT |
| `setuptools`（装 javmem 自己用） | MIT |
| `typing-extensions` | PSF-2.0 |
| `xlrd` | BSD |

### 开发、跑测试多装的（`requirements/dev.txt`）

| 包 | 许可证 |
| --- | --- |
| `colorama`（只在 Windows 上装） | BSD |
| `iniconfig` | MIT |
| `packaging` | Apache-2.0 或 BSD-2-Clause |
| `pluggy` | MIT |
| `pygments` | BSD-2-Clause |
| `pytest` | MIT |

### 用 Claude 判软变化另装的（`requirements/claude.txt`，可选）

| 包 | 许可证 |
| --- | --- |
| `anthropic` | Apache-2.0 |
| `annotated-types`、`anyio`、`docstring-parser`、`h11`、`jiter`、`pydantic`、`pydantic-core`、`truststore`、`typing-inspection` | MIT |
| `sniffio` | MIT 或 Apache-2.0 |
| `httpx2`、`httpcore2`、`idna` | BSD-3-Clause |
| `opentelemetry-api` | Apache-2.0 |
| `httpx2-jsfetch` | 只在 emscripten 上才装（在浏览器里跑 Python 那一种），Windows、Linux 都装不上它；许可证这里没核过 |

### Graphiti 支线另装的（`requirements/side.txt`，可选，单独一个环境）

| 包 | 许可证 |
| --- | --- |
| `graphiti-core` | Apache-2.0 |
| `openai`、`tenacity`、`requests`、`distro` | Apache-2.0 |
| `numpy` | BSD-3-Clause（里面带着的几样另有 0BSD、MIT、Zlib、CC0-1.0） |
| `httpx`、`httpcore`、`python-dotenv` | BSD-3-Clause |
| `posthog`、`backoff`、`urllib3` | MIT |
| `certifi` | MPL-2.0（只用、不改） |

支线锁文件里别的包（`pydantic` 这些）和上面几张表里的一样。Graphiti 的使用统计（`posthog`）在 import Graphiti 以前就关掉了，支线只连本机。

## 不在仓库里、要自己准备的

- **模型和向量服务：** javmem 不带模型。抽取用的大模型、事实的向量用的模型由你自己配，它们的许可证、服务条款归它们自己。
- **几个文档里提到的工具：** 备份推异地的例子用的 restic（BSD-2-Clause）、重新生成锁文件用的 uv（MIT 或 Apache-2.0），都不是 javmem 的依赖，要用自己装。
