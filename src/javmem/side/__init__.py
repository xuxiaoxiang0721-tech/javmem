"""Graphiti 支线（第 6 步）：开源的 Graphiti 当第二双眼睛，只吃原话，结果只进它自己的 Neo4j（体检库）。

- 和主线同一套 L1、L2、打码切窗口（不带预处理 A、B 的标注，不带上文），每个窗口一个 episode；每个包（每一版）
  在体检库里自己一组，撤回、换版本就整组删掉重跑（sweep）。
- 只用本机的模型（D156）：抽取是本机 Qwen，向量是本机的向量服务。进程里有一道只许连本机那几个端口的闸（netguard），
  Graphiti 自己的上报统计在 import 之前关掉。
- 主线的事实、关系、实体、证据、隔离、待确认、L3 结果库一张都不读；打分、比对的时候才把两边放在一起看。
- graphiti-core 是可选依赖 `[side]`：只有 `graph` 这个模块 import 它，别的模块（切 episode、支线的库、闸、导出、
  比对）不用装 Graphiti 也能用、能测。

各模块：config（环境变量）、episodes（包 → 窗口 → episode）、store（支线自己的库 side.db）、netguard（闸）、
graph（接 Graphiti：三个客户端、自定义类型、按组删）、run（跑一批包）、sweep（撤回了的整组删）、export（导出成对齐格式）、
compare（第二证人、体检的比对）、checkup（每周体检抽包）、cli（`javmem side …`）。
"""
