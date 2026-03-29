# 论文检索 Agent 架构计划

更新日期：2026-03-30

这份文档基于当前仓库的真实代码状态重写，目标不是重新描述一个“理想系统”，而是回答两个更实际的问题：

- 当前已经做到哪一步
- 接下来怎样把现有 MVP 收敛成可长期演进的检索核心

## 1. 当前实现快照

截至 2026-03-30，当前仓库已经不再只是设计稿，而是一个可运行的后端原型。

已经可以确认的能力：

- FastAPI 服务已可启动
- 已提供 `GET /v1/health`
- 已提供 `GET /v1/providers`
- 已提供 `GET /v1/providers/status`
- 已提供 `POST /v1/search/quick`
- 已提供 `POST /v1/search/deep`
- 已有 provider 配置、凭证注入和 live probe 脚本
- 已有 OpenAlex、Semantic Scholar、CORE、IEEE Xplore、Unpaywall、arXiv 六个 connector 首版实现
- 已有最小统一 schema
- 已有 LLM 客户端适配层，可兼容 `responses` 和 `chat/completions`
- 已有 Embedding 客户端
- 已拆分出共享检索层、Quick 通道和 Deep 通道
- 当前主路径在 LLM 可用时优先依赖 LLM planner
- Deep 已支持按检索源逐篇 `LLM judge`

当前还没有落地的关键能力：

- `POST /v1/search/fusion`
- `POST /v1/search/plan`
- `POST /v1/search/retrieve`
- `POST /v1/search/judge`
- `POST /v1/resolve/fulltext`
- 独立 `resolver` 模块
- 独立 `orchestrator` / `ranker` / `normalizer` 模块
- embedding 排序链路
- provider 级缓存与限流
- 系统化自动测试
- 前端页面
- Django 集成层
- skill 封装

当前阶段判断：

- 这是一个已经跑通主链路的后端 MVP
- 但还不是一个结构稳定的论文检索引擎
- 现阶段最重要的任务，不是继续横向加入口，而是继续强化已经拆出的 Quick / Deep 核心链路

## 2. 当前代码和目标架构之间的差距

### 2.1 已经具备的骨架

当前代码已经有几项非常关键的基础：

- 有统一 API 入口
- 有统一的 provider 注册和模式筛选
- 有统一的最小返回结构
- 有多源并发召回能力
- 有共享 intent planning
- 有共享 query variant 构造、DOI 标准化和多源去重
- 有独立 Quick 通道
- 有独立 Deep 通道
- 有按检索源逐篇执行的 deep judge

也就是说，系统已经具备“从 query 到多源结果”的完整闭环。

### 2.2 仍然偏 MVP 的部分

当前最主要的结构性问题已经从“所有逻辑都堆在一个文件里”，变成了“共享逻辑仍然偏集中，模块边界还可以更清晰”。

当前职责大致是：

- `search_service.py`
  - 只做薄封装
- `search_common.py`
  - 共享 planner、query variant、去重、召回、基础评分
- `quick_channel.py`
  - Quick 通道排序
- `deep_channel.py`
  - Deep 通道判定

这已经比最初的 MVP 清晰很多，但距离最终模块化目标还有空间。

当前实现和目标架构的核心差距主要有这些：

1. Quick 和 Deep 还没有真正成为两条独立检索通道

这条差距已经明显缩小，当前状态更准确地说是：

- 二者已经是两条独立通道
- 共享的是 planner、基础召回和去重层
- Quick 已经接入 `hybrid rerank`
- Deep 已经接入硬过滤 + per-source `LLM judge`
- 当前仍待增强的是更丰富的过滤条件和更成熟的排序特征

2. 去重还停留在最小版本

- 当前已支持 DOI 标准化
- DOI 缺失时退化为 `title + year + first_author`
- 但多源 metadata 合并仍然是 MVP 级别

3. 统一模型还不够丰富

- 当前已有 `SearchIntent`、`PaperResult`、`SearchResponse`
- 但还没有完整的 `CanonicalPaper`
- 也没有独立的 `JudgmentRecord`

4. 配置层和实现层已开始对齐

- `config.yaml` 中的 `retrieval.quick` 和 `retrieval.deep` 已被代码消费
- embedding 配置也已被 Quick 通道使用
- 但 `fusion`、缓存、限流、resolver 相关配置仍未完全落地

5. 运维和质量层仍然缺位

- 缺统一错误码
- 缺统一日志
- 缺缓存
- 缺 provider 级限流器
- 缺自动测试

## 3. 当前推荐的目标架构

基于当前进度，最合理的目标不是推翻重来，而是在已有 MVP 上继续收敛到下面这条主线：

`统一查询规划 + 多数据源 connector + 标准化去重 + Quick/Deep 双通道 + 可选 Fusion + Fulltext Resolver`

推荐分层如下。

### 3.1 Core Engine

核心检索逻辑只保留一份，建议包含这些模块：

- `planner`
- `orchestrator`
- `connectors`
- `normalization`
- `ranking`
- `judge`
- `resolver`

### 3.2 Service Layer

在核心之上暴露面向业务的服务层：

- `SearchService`
- `ResolveService`
- `SourceAvailabilityService`

### 3.3 Delivery Layer

所有交付形态都只做薄封装：

- HTTP API
- Frontend Web UI
- Skill / Tool Wrapper
- 可选 Python SDK

这条分层原则要尽量坚持：

`核心检索逻辑只存在一份，API、前端、Django 和 skill 都只调用它，不重复实现它`

## 4. 推荐的核心抽象

### 4.1 SearchIntent

当前代码里已经有最小版 `SearchIntent`，下一步建议扩充为更适合 planner 使用的结构。

建议至少覆盖：

- `original_query`
- `rewritten_query`
- `must_terms`
- `should_terms`
- `exclude_terms`
- `filters`
- `source_preferences`
- `planner`
- `reasoning`

其中 `filters` 建议逐步支持：

- `year_from`
- `year_to`
- `is_oa`
- `publication_types`
- `language`

### 4.2 CanonicalPaper

当前的 `PaperResult` 已经可以作为最小统一结构，但后续更适合拆成两层：

- 召回和标准化阶段使用 `CanonicalPaper`
- 返回给 API 时再映射为 `PaperResult`

`CanonicalPaper` 建议逐步补齐这些字段：

- `source`
- `source_id`
- `title`
- `abstract`
- `authors`
- `year`
- `venue`
- `doi`
- `url`
- `pdf_url`
- `is_oa`
- `citations`
- `keywords`
- `fields_of_study`
- `language`
- `publication_type`
- `raw`

### 4.3 JudgmentRecord

当前 `score / scores / decision / confidence / reason / matched_fields` 已经具备雏形。

后续建议把判定信息独立建模为 `JudgmentRecord`，至少保留：

- `paper_id`
- `quick_score`
- `vector_score`
- `keyword_score`
- `deep_score`
- `llm_relevance`
- `decision`
- `confidence`
- `reason`
- `evidence`

这样 Quick、Deep 和 Fusion 才能共享同一套解释结构。

## 5. 数据源角色分层建议

当前 connector 已经接入得比最初 Phase 1 更广，但在产品策略上仍然建议分层使用，而不是默认把所有源平铺并发。

### 5.1 主召回层

优先作为默认召回层：

- OpenAlex
- Semantic Scholar
- CORE
- arXiv

其中当前最稳定、最适合作为默认主力的仍然是：

- OpenAlex
- Semantic Scholar

### 5.2 精准增强层

只在指定来源或特定场景启用：

- IEEE Xplore
- 万方

### 5.3 Resolver / Enrichment 层

不作为主召回源，而是作为补全与解析层：

- Unpaywall
- CORE discover

### 5.4 受限接入层

继续保留接口预留，但暂不纳入自动主链路：

- CNKI

## 6. 三条检索通道的目标定义

### 6.1 Quick Search

当前状态：

- 已实现接口
- 已可返回真实结果
- 当前优先依赖 LLM planner 生成 `rewritten_query`、`must_terms` 和 `should_terms`
- 已接入 `hybrid rerank`
- 在 embedding 可用时，会引入 semantic score

目标状态：

- 共享查询规划
- 多源召回
- 标准化去重
- keyword + embedding 的 hybrid rerank
- 返回 Top-K

它的定位应当是：

- 低延迟
- 成本低
- 覆盖广
- 适合探索式检索

### 6.2 Deep Search

当前状态：

- 已实现接口
- 当前优先依赖 LLM planner 生成 `rewritten_query`、`must_terms`、`should_terms` 与 `filters`
- 每个检索源内会先做启发式预评分
- 已有基础硬过滤
- 在有可用 LLM 配置时，可对每个检索源内的 Top-N 候选逐篇做结构化 judge

目标状态：

- 独立于 Quick 的高精度后处理通道
- 先做硬规则过滤
- 再做结构化 LLM judge
- 输出更稳定的保留理由和排除理由

它的定位应当是：

- 精度优先
- 解释性更强
- 可处理更复杂约束

### 6.3 Fusion Search

当前状态：

- 仅在 schema 和配置层有预留
- 尚无 API 和服务实现

目标状态：

- Quick 和 Deep 并行运行
- 双路结果统一去重
- 产出 `fusion_score`
- 返回命中来源标记

它更适合：

- 高价值查询
- 专家模式
- 后台批处理

## 7. 当前最值得优先做的收敛工作

现在不建议优先做更多接口包装或更多交付入口，建议先把下面这些核心问题解决。

### 7.1 先补强 Normalize + Deduplicate

这是当前最先该做的一步。

优先补的内容：

- DOI 标准化
- 标题清洗
- `title + year + first_author` 近似去重
- 多源合并策略

原因很直接：

- 当前已经是多源系统
- 如果去重不稳，多源价值会被重复结果抵消
- Fusion、resolver、评估指标也都会受影响

### 7.2 把 `search_service.py` 拆成独立模块

这一部分已经部分完成，当前已经有：

- `search_common.py`
- `quick_channel.py`
- `deep_channel.py`

下一步更适合继续拆出：

- `planners/intent_planner.py`
- `orchestrators/retrieval_orchestrator.py`
- `normalization/deduper.py`
- `ranking/quick_ranker.py`
- `ranking/deep_ranker.py`
- `judge/llm_judge.py`

当前不是没有分层，而是共享层还偏厚。

### 7.3 让 Quick 和 Deep 真正分叉

建议先把二者的产品语义固定下来：

- Quick 负责广召回后的快速排序
- Deep 负责复杂约束下的精细判断

这样后续做 Fusion 才不会只是把同一批结果重复算两遍。

### 7.4 再补 Fusion 和 Resolve

在 normalize、ranker 和 judge 稳定之前，`fusion` 很容易把当前问题叠加放大。

因此建议顺序是：

1. 先收敛核心链路
2. 再补 `POST /v1/search/fusion`
3. 再补 `POST /v1/resolve/fulltext`

## 8. 推荐的阶段路线图

### Phase 1：后端 MVP 跑通

当前状态：已基本完成

已经完成：

- 基础 FastAPI 服务
- provider 配置体系
- 多 connector 首版接入
- `quick` / `deep` API
- 本地调试脚本
- live probe
- Quick 独立通道
- Deep 独立通道
- Quick `hybrid rerank`
- Deep per-source `LLM judge`

仍待补齐：

- 更强去重
- 更稳定 judge
- 更清晰的模块边界

### Phase 2：核心引擎收敛

当前状态：下一阶段主线

目标：

- 把共享逻辑进一步从 `search_common.py` 中拆细
- 固化 `SearchIntent` / `CanonicalPaper` / `JudgmentRecord`
- 完成 normalize + dedup
- 继续增强 Quick 的 hybrid ranking
- 继续增强 Deep 的规则过滤和 source-aware judge

### Phase 3：能力补齐

目标：

- `POST /v1/search/fusion`
- `POST /v1/resolve/fulltext`
- 缓存
- provider 级限流
- 统一错误码与日志
- 自动测试

### Phase 4：多交付形态扩展

目标：

- 独立前端页面
- Django 调用层
- 可选 Python SDK
- skill 封装

建议顺序仍然是：

1. 后端 API
2. 前端页面
3. Django / SDK
4. skill

## 9. 关于前端、Django 和 skill 的定位

当前路线不建议改变。

### 9.1 独立后端接口服务

这仍然应该是主线和唯一事实来源。

它负责：

- query planning
- source orchestration
- normalize / dedup
- quick / deep / fusion
- fulltext resolve
- 日志、缓存、限流、错误码

### 9.2 独立前端页面

前端应当只是这个后端服务的官方客户端。

前端负责：

- 输入 query
- 选择 mode
- 选择 source
- 展示结果和解释

前端不应负责：

- connector 调用
- API key 管理
- query planning

### 9.3 Django 集成

推荐继续保持“Django 是调用方，而不是核心实现载体”。

更适合的职责边界是：

- Django 负责用户、权限、历史、收藏、业务页面
- 检索服务负责检索逻辑本身

### 9.4 Skill 封装

skill 应该排在后面，并优先调用稳定后的统一 API，而不是直接重新耦合全部 connector。

## 10. 近期开发优先级

如果只看接下来一轮最值得投入的工作，建议按这个顺序推进：

1. 补强 DOI 标准化和多源去重
2. 继续拆分共享逻辑，降低 `search_common.py` 的集中度
3. 继续增强 Quick 的 hybrid ranking
4. 继续增强 Deep 的硬规则过滤和更稳定的 per-source LLM judge
5. 完成 `POST /v1/search/fusion`
6. 完成 `POST /v1/resolve/fulltext`
7. 增加统一日志、错误码、缓存、限流和自动测试
8. 最后再推进前端、Django 集成和 skill

## 11. 一句话结论

当前项目最准确的定位是：

`一个已经跑通主链路、并开始依赖 LLM planner 驱动 Quick/Deep 双通道的论文检索后端 MVP，下一阶段重点是继续增强判定质量、去重质量与模块边界。`
