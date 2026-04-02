# 论文检索 Agent 架构计划

更新日期：2026-04-03

这份文档基于当前仓库的真实代码状态重写，目标不是重新描述一个“理想系统”，而是回答两个更实际的问题：

- 当前已经做到哪一步
- 接下来怎样把现有 MVP 收敛成可长期演进的检索核心

本文后续统一使用 `deep` 指代当前这条 criterion-aware 深搜链路，不再单独使用其他别名。

## 1. 当前实现快照

截至 2026-04-03，当前仓库已经不再只是设计稿，而是一个可运行的后端原型。

已经可以确认的能力：

- FastAPI 服务已可启动
- 已提供 `GET /v1/health`
- 已提供 `GET /v1/providers`
- 已提供 `GET /v1/providers/status`
- 已提供 `POST /v1/search/quick`
- 已提供 `POST /v1/search/deep`
- `SearchRequest` 已支持 `sources`、`limit_per_source`、`public_only`、`enable_llm`、`enable_intent_planner`、`llm_top_n`
- 已有 provider 配置、凭证注入和 live probe 脚本
- 已有 OpenAlex、Semantic Scholar、CORE、Crossref、IEEE Xplore、Unpaywall、arXiv 七个 connector 首版实现
- 已有最小统一 schema
- 已有 LLM 客户端适配层，可兼容 `responses` 和 `chat/completions`
- 已有 Embedding 客户端
- 已拆分出共享检索层、Quick 通道和 Deep 通道
- 已有独立静态前端测试页 `frontend/index.html` 与本地代理脚本 `frontend/dev_server.py`
- 当前主路径在 LLM 可用时优先依赖 LLM planner
- intent planner prompt 已明确支持将非英文 query 重写为适合英文论文源检索的学术英文 `rewritten_query`
- intent planner prompt 已明确要求尽量保留 acronym、模型名、数据集名、作者名、会议名和领域术语
- intent planner prompt 已明确约束 `query_hints` 为 1-4 个词的 provider-friendly 检索短语，避免指令语进入 query bundle
- `deep` 已默认支持 `criteria + logic + query bundle + criterion-level judge` 的复杂组合查询链路
- Deep 召回 query bundle 已调整为优先使用 `intent.rewritten_query`
- `query_hints` 已收敛为 provider-friendly 检索短语，不再把自然语言提示句或 `also try` 之类指令语直接送进 query bundle
- `quick` / `deep` 的内部召回接口已开始统一，并补上 `retrieval_traces`
- `deep` 已拆出 provider-specific recall/query rendering，不同 source 不再共享同一种 raw query
- 组合条件 criterion（例如 planner 产出的 `combination` 条件）已保留独立 query 位，并参与 `criteria-and` 合取查询
- `search_common.normalize_text()` 已改成 Unicode-aware，并为 CJK 增加了 fallback tokenization
- 已落地首版 `provider runtime/policy` 层
- 已接入 Redis 共享缓存与 provider 级请求控制
- connector 的共享缓存、批量调度和请求限流已开始从具体 provider 逻辑中抽离
- 已接入 Crossref connector 首版，并按 `query.bibliographic + polite mailto + provider runtime` 方式纳入统一链路
- 组合条件支持阈值已调高，heuristic/LLM 的 criterion judgment 融合也不再通过 `or + max` 把 coverage 人为抬到 `1.0`
- 已落地动态送审窗口：full coverage 保底送审、coverage band 轮转送审、低产出车道 early-stop
- 已落地 Deep 最终 hard prune，默认优先返回 `keep + 高分 maybe`
- `SearchResponse` 已补 `raw_recall_count / deduped_count / finalized_count`
- `deep` 默认召回预算已可通过 `retrieval.deep.limit_per_source_default` 配置

当前还没有落地的关键能力：

- `POST /v1/search/fusion`
- `POST /v1/search/plan`
- `POST /v1/search/retrieve`
- `POST /v1/search/judge`
- `POST /v1/resolve/fulltext`
- 独立 `resolver` 模块
- 独立 `orchestrator` / `ranker` / `normalizer` 模块
- 更成熟的 embedding 排序链路
- 更细粒度的 provider query policy / 观测能力
- 系统化自动测试
- 正式前端页面（当前仅有独立测试页）
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
- 有按检索源逐篇执行、且支持动态窗口的 deep judge
- 有按 provider 解耦的 runtime/policy 入口
- 有 Redis 驱动的共享缓存与请求控制首版实现
- 有面向后续 `fusion` 的统一内部召回接口与 `retrieval_traces`

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
- 当前 `PaperResult` 已补 `retrieval_traces`，`SearchResponse` 已补 `raw_recall_count / deduped_count / finalized_count`
- 但还没有完整的 `CanonicalPaper`
- 也没有独立的 `JudgmentRecord`

4. 配置层和实现层已开始对齐

- `config.yaml` 中的 `retrieval.quick` 和 `retrieval.deep` 已被代码消费
- embedding 配置也已被 Quick 通道使用
- Redis 配置和 `sources.<provider>.runtime` 策略也已开始被代码消费
- 但 `fusion`、更细粒度的 query policy、resolver 相关配置仍未完全落地

5. 运维和质量层仍然缺位

- 缺统一错误码
- 缺统一日志
- provider runtime 缺更完整的指标与观测
- 缺自动测试

6. 多语言 query planning 和 lexical scoring 仍偏英文中心

- 当前 prompt 层已经补上“非英文 query -> 学术英文 `rewritten_query`”与实体保留规则，但这只解决了 query planning 的一部分问题
- `search_common.normalize_text()` 已改成 Unicode-aware，并为 CJK 增加了 fallback tokenization，但在完全没有 LLM rewrite 时，多语言 heuristic planner 仍无法替代英文学术重写
- 这仍会影响 heuristic fallback planner、Quick lexical rerank、Deep heuristic prefilter，以及 `must_terms` / `should_terms` 的质量上限
- Deep 召回 query bundle、Quick 的 lexical rerank 与 Deep 的 heuristic scoring 已开始优先使用 `rewritten_query`，但 Quick / Deep 的 bilingual query policy 仍未完全 source-aware
- 因此下一阶段不能只停留在“改 prompt”，还需要把 query planning、lexical normalization 和 provider query policy 一起收敛

## 3. 当前推荐的目标架构

基于当前进度，最合理的目标不是推翻重来，而是在已有 MVP 上继续收敛到下面这条主线：

`统一查询规划 + 多数据源 connector + 标准化去重 + Quick/Deep 双通道 + 可选 Fusion + Fulltext Resolver`

推荐分层如下。

### 3.1 Core Engine

核心检索逻辑只保留一份，建议包含这些模块：

- `planner`
- `orchestrator`
- `connectors`
- `provider_runtime`
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

### 4.4 ProviderRuntimePolicy

这部分已经有首版真实实现，当前更适合把它视为核心抽象，而不是临时补丁。

它负责表达每个 provider 自己的运行策略，例如：

- `batch_mode`
- `cache_backend`
- `cache_ttl_seconds`
- `rate_limit_backend`
- `min_interval_seconds`
- `serialize_requests`
- `retry_on_statuses`
- `retry_backoff_seconds`

工程含义是：

- 查询规划仍然共享
- 但多 query variant 怎么批量下发、是否缓存、是否串行、是否做共享限流，都由 provider 自己决定
- 这样可以避免把 arXiv 的严格约束硬编码到所有数据源上

## 5. 数据源角色分层建议

当前 connector 已经接入得比最初 Phase 1 更广，但在产品策略上仍然建议分层使用，而不是默认把所有源平铺并发。

当前代码状态下，还需要把“角色分层”和“运行时策略分层”一起看：

- 不是所有源都应该用同样的批处理方式
- 不是所有源都需要同样强度的共享限流
- 是否接 Redis 缓存、缓存 TTL 多久，也应按 provider 单独定义

### 5.1 主召回层

优先作为默认召回层：

- OpenAlex
- Semantic Scholar
- CORE
- Crossref
- arXiv

其中当前最稳定、最适合作为默认主力的仍然是：

- OpenAlex
- Semantic Scholar

Crossref 在这里更适合作为：

- 补广度的高覆盖元数据召回源
- DOI / funding / license / relation / full-text-link 信号增强源
- 不单独承担相关性排序的 metadata 基础设施

当前实现层面的典型策略是：

- OpenAlex：并发 query variant + Redis 热缓存
- Semantic Scholar：Redis 热缓存 + 保守请求控制
- Crossref：`query.bibliographic + polite mailto + Redis 缓存 + 保守串行请求控制`
- arXiv：串行 query variant + Redis 缓存 + Redis 限流/锁

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
- 当前优先依赖 LLM planner 生成 `rewritten_query`、`must_terms`、`should_terms`、`logic` 与 `criteria`
- 当前 Quick query bundle 已优先使用 `intent.rewritten_query`，且在默认配置 `retrieval.quick.max_query_variants=1` 下通常只会下发 `rewritten-main`
- 已接入 `hybrid rerank`
- 在 embedding 可用时，会引入 semantic score
- provider 批处理策略已从共享召回层下沉到 runtime/policy 层

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
- 当前优先依赖 LLM planner 生成 `rewritten_query`、`must_terms`、`should_terms`、`filters`、`logic` 与 `criteria`
- 当前 Deep 召回已切到面向复杂组合查询的 query bundle，当前默认会组合 `rewritten-main`、`criteria-and`、`original-query`、`must-terms`、criterion-focused query 与 `criteria-compact`
- `deep` 已拆出 provider-specific recall/query rendering，且尽量保持 `quick` / `deep` 内部召回接口统一
- 组合条件 criterion（例如 planner 产出的 `combination` 条件）已进入合取查询与独立 criterion query，且 query phrase 会先清洗成 provider-friendly 检索短语
- 每个检索源内会先做 criterion-level heuristic 预评分与 required coverage 计算
- 已有基础硬过滤
- 在有可用 LLM 配置时，可按动态送审窗口对每个检索源内的候选逐篇做 criterion-level 结构化 judge
- provider runtime 已可控制不同 source 的 query variant 调度方式
- 当前 `deep` 已直接承接复杂组合查询的 criterion-aware 处理语义；复杂组合查询已可按 `criteria + logic + query bundle + criterion-level judge` 路径处理
- 已增加最终 hard prune，默认优先返回 `keep + 高分 maybe`
- 响应已补 `raw_recall_count / deduped_count / finalized_count`

目标状态：

- 独立于 Quick 的高精度后处理通道
- 先做硬规则过滤
- 再做结构化 LLM judge
- 输出更稳定的保留理由和排除理由

它的定位应当是：

- 精度优先
- 解释性更强
- 可处理更复杂约束

### 6.2.1 复杂组合查询专用 `deep` Pipeline

对于“文本 RAG + 图 RAG”“模型 A + 数据集 B + 方法 C”“综述 + 近五年 + 医学影像”这类复杂组合查询，当前实现已经开始按这条专用模式运行。

当前已落地的设计方向：

- 在 intent planning 中显式产出 `criteria + logic`，而不只产出 `rewritten_query/must_terms/should_terms`
- `criteria` 应支持多个 required 子条件，例如 `text_rag`、`graph_rag`、`combination`
- `logic` 应至少支持 `AND`，后续可扩展 `OR` / `NOT`
- Deep 召回不再只依赖少量 query variants，而是生成 query bundle，包括 `rewritten-main`、`criteria-and`、`original-query`、`must-terms`、criterion-focused query 与 `criteria-compact`
- query bundle 中的 criterion phrase 已优先压缩成 provider-friendly 检索短语，避免把整句 prompt 或指令语直接送入 provider
- `query_hints` 已被进一步收紧为 1-4 个词的 noun phrase，避免 `also try` / `related term` / `search for` 这类提示语污染 deep recall
- 组合条件 criterion 已保留独立 query 位，并进入合取查询，而不是只在 judge 阶段存在
- heuristic 预评分不再只计算一个总分，而是按 criterion 分别计算 support，再合成为 composite score
- LLM judge 不再只输出总体 `decision/relevance/confidence/reason`，而是补充 criterion-level judgment
- 组合条件的支持阈值已调高，heuristic/LLM 的 criterion merge 也不再通过 `or + max` 把 coverage 虚高抬满
- 对复杂组合查询动态提高 `llm_top_n_per_source` 和 query bundle 预算，避免送审预算被泛相关候选耗尽
- 动态送审窗口已实现为 full coverage 保底、coverage band round-robin 和 lane early-stop 的组合
- 最终排序优先依据 required criteria coverage、criterion-level evidence 和 deep score，而不是只看整体相关性
- 最终返回阶段已增加 hard prune，并附带 `raw_recall_count / deduped_count / finalized_count`

工程上当前可以直接把这条模式视为 `deep` 本身：

- `deep` 默认处理多条件组合、强 conjunction 和复杂验证型查询
- `deep` 共享 connector、runtime 和去重层，但在 planner、query generation、prefilter 和 judge 上已经体现出 criterion-aware 分叉
- 对外不再额外引入新的模式名，避免和 API 模式枚举脱节

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

### 7.2 补强多语言 Query Planning 和 Lexical Normalization

这部分已经从“体验优化”变成“核心召回与排序质量问题”。

当前最需要尽快收敛的点：

- prompt 层的非英文 query -> 学术英文 rewrite 与实体保留规则已经落地
- Deep 召回 query bundle 已优先使用 `intent.rewritten_query`
- `normalize_text()` 已完成 Unicode-aware 改造，但在没有 LLM rewrite 时，多语言 heuristic planner 仍有明显上限
- Quick / Deep 的 lexical 与 heuristic scoring 已开始优先使用 `intent.rewritten_query`，但 bilingual query policy 仍不能只靠单一策略兜底

建议方向：

- 保留原始 query，同时生成英文 rewrite
- 让 provider 按 source 选择 original-first、English-first 或 bilingual query variants
- 继续把 `intent.rewritten_query` 贯穿到 Quick / Deep 的 lexical scoring 和 heuristic scoring
- 在现有 Unicode-aware / CJK fallback tokenization 的基础上，继续收敛 bilingual lexical scoring 和 provider query policy

### 7.3 继续收敛已落地的 `deep` 复杂组合查询链路

这部分的核心已经从“要不要做”转成“如何继续把已落地实现收敛到更稳定的 source-aware 版本”。

当前这一轮已经完成的收敛：

- `query_hints` 已压缩并收紧为 provider-friendly 检索短语，prompt 化 bundle 已明显减少
- 组合条件 criterion 已保留独立 query 位，并真正参与合取召回
- `quick` / `deep` 的内部召回接口已开始统一，并补上 `retrieval_traces`
- `deep` 已拆出 provider-specific recall/query rendering，不同 source 不再共享同一种 raw query
- 组合条件支持阈值已调高，coverage 不再被 heuristic/LLM 的 `or + max` 融合虚高抬满
- 动态送审窗口已落地：full coverage 保底、coverage band round-robin、低产出车道 early-stop
- Deep 最终 hard prune 已落地，默认不再返回所有 dedup 后候选
- 响应已补 `raw_recall_count / deduped_count / finalized_count`，方便区分召回损失、去重损失和最终裁剪损失

基于最近几轮对“文本 RAG + 图 RAG”与“超材料 FSS”查询的 smoke test，可以确认：

- GraphRAG-only 候选已不再普遍被打成满 coverage，明显错误项也能被压到 `drop`
- 当前 Top 结果已经更接近“真正结合 text RAG + graph RAG 的论文候选”
- FSS 这类目标明确的查询，在提高召回预算和送审预算后，已能捕获更多 `keep`
- `deep` 默认输出已经比上一轮更收敛，但结果质量仍明显依赖 planner 和 provider-specific recall 质量

下一步更值得优先补齐这些能力：

- 继续收紧 combination criterion 的证据要求，尽量要求“明确存在独立 text retriever + graph retriever 的联合方案”
- 在 query bundle 中增加 `2-of-4 / 3-of-4` 放宽组合，覆盖“满足大部分 required criteria、但没有在标题/摘要里把全部条件一次性写全”的候选论文
- 继续清洗 planner 产出的 criterion hints 和 `criteria-and` phrase，减少重复和串味 query 进入 query bundle
- 为不同 provider 增加更细的 source-aware query bundle policy，并继续打磨 deep query renderer
- 在预筛和 judge 中补更稳定的 criterion-level evidence 抽取
- 在现有动态送审窗口之上补 lane 级 debug 统计、预算观测和更稳定的 early-stop 调参能力
- 继续增强 required criteria coverage 之外的 hard filters 与排序特征

这条模式最适合处理：

- `A + B + combine` 型查询
- 多个必须同时成立的专业概念
- 需要验证“论文是否真的把两个方向结合起来”的问题

### 7.4 把 `search_service.py` 拆成独立模块

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

同时也建议继续把当前已存在的 `provider runtime/policy` 首版实现进一步收敛成独立子层，避免它继续散落在 base client 和各 connector 之间。

### 7.5 让 Quick 和 Deep 真正分叉

建议先把二者的产品语义固定下来：

- Quick 负责广召回后的快速排序
- Deep 负责复杂约束下的精细判断

这样后续做 Fusion 才不会只是把同一批结果重复算两遍。

### 7.6 继续补强 Provider Runtime / Policy

这部分已经不是“要不要做”的问题，而是“如何从首版收敛到稳定版”的问题。

下一步更值得补的点：

- provider 级观测指标
- 更明确的 query policy
- probe 和正式搜索链路的策略对齐
- 更细粒度的 backoff / degrade 策略

### 7.7 再补 Fusion 和 Resolve

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
- Crossref connector 首版接入
- `quick` / `deep` API
- 本地调试脚本
- live probe
- 独立前端测试页与本地代理脚本
- Quick 独立通道
- Deep 独立通道
- Quick `hybrid rerank`
- Deep per-source `LLM judge`
- Deep 动态送审窗口
- Deep 最终 hard prune
- Redis 配置化缓存
- provider 级请求控制首版
- `provider runtime/policy` 首版

仍待补齐：

- 更强去重
- 更稳定 judge
- 更清晰的模块边界

### Phase 2：核心引擎收敛

当前状态：下一阶段主线

目标：

- 把共享逻辑进一步从 `search_common.py` 中拆细
- 固化 `SearchIntent` / `CanonicalPaper` / `JudgmentRecord`
- 固化 `ProviderRuntimePolicy`
- 完成 normalize + dedup
- 补强多语言 query planning 与 multilingual lexical scoring
- 继续收敛已落地的 `deep` 复杂组合查询链路
- 继续增强 Quick 的 hybrid ranking
- 继续增强 Deep 的规则过滤和 source-aware judge

### Phase 3：能力补齐

目标：

- `POST /v1/search/fusion`
- `POST /v1/resolve/fulltext`
- 统一错误码与日志
- 更完整的 provider 观测与治理能力
- 自动测试

### Phase 4：多交付形态扩展

目标：

- 正式前端页面（在当前独立测试页基础上产品化）
- Django 调用层
- 可选 Python SDK
- skill 封装

建议顺序仍然是：

1. 后端 API
2. 前端页面产品化
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

当前代码状态：

- 已有 `frontend/index.html` 独立静态测试页
- 已有 `frontend/dev_server.py`，可把 `/api/*` 代理到现有后端
- 当前这套前端更适合作为联调和结果展示沙盒，还不是正式产品前端

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
2. 补强多语言 query planning、Unicode-aware normalization 和 bilingual lexical scoring
3. 继续收敛已经落地的 `deep`，优先补齐 `2-of-4 / 3-of-4` 放宽组合、更严格的 combination criterion 证据要求、更细的 source-aware recall/query policy，以及更稳定的 criterion-level evidence
4. 继续拆分共享逻辑，降低 `search_common.py` 的集中度
5. 继续收敛 `provider runtime/policy`，补齐 query policy、日志和观测
6. 继续增强 Quick 的 hybrid ranking
7. 继续增强 Deep 的硬规则过滤和更稳定的 per-source LLM judge
8. 完成 `POST /v1/search/fusion`
9. 完成 `POST /v1/resolve/fulltext`
10. 增加统一错误码和自动测试
11. 最后再推进前端、Django 集成和 skill

## 11. 一句话结论

当前项目最准确的定位是：

`一个已经跑通主链路、并开始通过 provider runtime/policy 收敛多源缓存与请求控制的论文检索后端 MVP，下一阶段重点是继续增强多语言检索质量、判定质量、去重质量、模块边界与运行时治理能力。`
