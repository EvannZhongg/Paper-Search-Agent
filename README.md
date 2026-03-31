# Agentic-Scholar

一个面向论文检索场景的独立后端服务原型。当前仓库已经完成多检索源接入、基础 API、配置加载、连通性探测与本地调试脚本，但整体仍处于 MVP 阶段，离“可长期演进的检索核心服务”还有一轮架构收敛。

当前项目优先作为：

- 独立后端接口服务
- 后续可被 Django 调用
- 后续可封装为 skill 给其他 agent 调用

## 当前状态

更新日期：2026-03-31

当前可以确认的进度：

- FastAPI 服务已可启动
- `quick` / `deep` 两条基础检索链路已可返回真实结果
- `quick` 已拆成独立检索通道，并接入 `hybrid rerank`
- `deep` 已拆成独立检索通道，并支持 `criteria + logic + query bundle + criterion-level judge` 的复杂组合查询链路
- `quick` / `deep` 的内部召回接口已开始统一，并补上 `retrieval_traces`，为后续 `fusion` 铺路
- `deep` 已拆出 provider-specific recall/query rendering，不同 source 不再共享同一种 raw query
- `deep` 已落地动态送审窗口、最终 hard prune 和默认结果收敛策略
- provider 配置、凭证注入、连通性探测脚本已具备
- 多个 connector 已完成首版接入
- 已落地首版 `provider runtime/policy` 层
- 已接入 Redis 配置化缓存与 provider 级请求控制
- 各检索源的批处理、缓存、限流策略已开始按 provider 解耦
- 调试输出已补 `raw_recall_count / deduped_count / finalized_count`

本文后续统一使用 `deep` 指代当前这条 criterion-aware 深搜链路，不再单独使用其他别名。

当前阶段判断：

- 这是一个“后端原型已跑通”的项目，不再是纯设计稿
- 但还不是“架构计划 fully landed”的版本
- 当前更适合视为：`可运行 MVP + 待收敛的核心引擎`
- 当前文档说明已按 2026-03-31 的代码状态重新对齐

## 已完成内容

### 1. 配置体系

已完成：

- `config/config.yaml`
- `.env` / `.env.example`
- `config/settings.py`

当前配置方式：

- 非敏感配置放在 `config/config.yaml`
- API Key / Email 等敏感信息通过 `.env` 注入
- 支持通过 `*_env` 字段从环境变量映射到运行配置
- Redis 连接信息与 provider runtime 策略也统一在配置层声明

### 2. 后端 API MVP

当前已实现接口：

- `GET /v1/health`
- `GET /v1/providers`
- `GET /v1/providers/status`
- `POST /v1/search/quick`
- `POST /v1/search/deep`

当前尚未实现：

- `POST /v1/search/fusion`
- `POST /v1/search/plan`
- `POST /v1/search/retrieve`
- `POST /v1/search/judge`
- `POST /v1/resolve/fulltext`

### 3. 已接入的 connector

当前代码中已实现：

- OpenAlex
- Semantic Scholar
- CORE
- IEEE Xplore
- Unpaywall
- arXiv

当前配置中已预留但未实现运行接入：

- 万方
- CNKI

### 4. 调试与验证脚本

已提供脚本：

- `scripts/run_provider_probes.py`
- `scripts/run_quick_search.py`
- `scripts/run_search.py`

### 5. Prompt 与 LLM 适配

已完成：

- prompt 集中管理到 `app/prompts.py`
- LLM 客户端兼容 `responses` / `chat_completions`
- Embedding 客户端已接入
- 当前主路径优先依赖 LLM planner；不可用时回退到启发式 planner
- Deep Search 已支持在有可用 LLM 配置时做 criterion-level 结构化判定
- intent planner prompt 已明确要求：非英文输入尽量重写为面向英文文献检索的简洁学术英文 `rewritten_query`
- intent planner prompt 已明确要求保留 acronym、模型名、数据集名、作者名、会议名和领域术语
- intent planner prompt 已明确约束 `query_hints` 为 1-4 个词的 provider-friendly 检索短语，避免 `also try`、`related term`、`search for` 这类指令语进入 query bundle

### 6. Provider Runtime / Policy

已完成：

- 新增统一的 `provider runtime/policy` 层
- connector 的共享缓存、请求控制和批量调度已从具体 provider 逻辑中抽离
- Redis 已作为共享缓存和分布式请求控制后端接入
- `BaseSourceClient` 已统一承接标准化 query、批处理策略和 HTTP request 包装

当前策略现状：

- arXiv：`sequential batch + Redis cache + Redis 限流/锁 + 429 backoff`
- Semantic Scholar：`Redis cache + 保守请求控制`
- OpenAlex / CORE / IEEE / Unpaywall：已接入 Redis 热缓存

## 当前实际实现方式

### Quick Search

当前流程：

1. 对用户 query 做 intent planning
2. 当前优先使用 LLM planner 生成 `rewritten_query`、`must_terms`、`should_terms`、`filters`、`logic` 与 `criteria`；对非英文输入会尽量生成面向英文论文源的学术英文 `rewritten_query`
3. 生成 Quick 通道专属 query variants，当前优先使用 `intent.rewritten_query`
4. 把 query variants 下发给可用 source，并由各 provider 自己决定批处理策略
5. 对结果做统一去重和 DOI 标准化
6. 结合 lexical / semantic / source prior / recency / open access 做 `hybrid rerank`
7. 按 `quick score` 排序返回

注意：

- 当前 Quick Search 已不再只是启发式打分
- 在 embedding 可用时，会计算 query 与论文文档文本的语义相似度
- 若 embedding 不可用，会自动退化为 lexical + source prior + recency + OA 的混合排序
- provider 侧的缓存、限流和多 query variant 调度不再硬编码在共享召回层

### Deep Search

当前流程：

1. 对用户 query 做 intent planning
2. 当前优先使用 LLM planner 生成 `rewritten_query`、`must_terms`、`should_terms`、`filters`、`logic` 与 `criteria`；对非英文输入会尽量生成面向英文论文源的学术英文 `rewritten_query`
3. 生成面向复杂组合查询的 `query bundle`，其中包括主查询、criteria 合取查询、紧凑放宽查询、原始 query fallback 与 criterion-focused query；query phrase 会优先压缩成 provider-friendly 检索短语，而不是自然语言提示句
4. 做多源召回；`deep` 已拆出 provider-specific recall/query rendering，同时尽量保持 `quick` / `deep` 内部 batch 接口统一，便于后续 `fusion`
5. 对每个 source 的候选结果先做 criterion-level heuristic 预评分，计算 required criteria coverage 与 composite heuristic score
6. 再做基础硬过滤，例如 `year_from/year_to/is_oa`
7. 若 LLM 已配置且启用，则按“full-coverage 保底 + coverage band round-robin + lane early-stop”的动态窗口，对候选逐篇做 criterion-level `LLM judge`
8. 将 required criteria coverage、criterion score、heuristic 分与 LLM relevance 融合成 `deep score`
9. 所有 source 结果再统一去重，并按 `coverage -> decision -> deep score` 排序
10. 做最终 hard prune，默认优先返回 `keep + 高分 maybe`，并在响应中附带 `raw_recall_count / deduped_count / finalized_count`

注意：

- 当前 LLM judge 是“每个检索源内逐篇判断 + criterion-level judgment”，不是仅对全局结果做一次统一判定
- 当前送审窗口已不再固定为“每源 Top-N 截断”，而是按 coverage band 和 `(query variant, source)` 车道动态轮转送审
- 当前硬过滤仍是第一版，主要支持 `year_from`、`year_to` 和 `is_oa`
- 组合条件 `c4` 现在会保留独立 query 位，并参与 `criteria-and` 合取查询；相关支持阈值也已调高
- heuristic/LLM 的 criterion judgment 融合已不再使用 `or + max` 直接把 coverage 抬成 `1.0`
- `deep` 默认已不再返回所有 dedup 后候选；`retrieval.default_top_k_return` 和最终 maybe 阈值配置已开始生效
- `query_hints` 已被进一步收紧为 1-4 个词的 provider-friendly 短语，避免指令语污染 deep query bundle
- 复杂组合查询已能走 `criteria + logic + query bundle + criterion-level judge` 链路，但 query policy、更多 hard filters 和更稳定的 criterion-level evidence 仍可继续增强

### 当前数据模型

已经具备最小统一结构：

- `SearchIntent`
- `PaperResult`
- `RetrievalTrace`
- `SearchResponse`
- `ProbeResult`

但还未完全达到目标架构中的：

- 丰富版 `SearchIntent`
- 完整版 `CanonicalPaper`
- 独立版 `JudgmentRecord`

其中当前已新增的实用字段包括：

- `PaperResult.retrieval_traces`
- `SearchResponse.raw_recall_count`
- `SearchResponse.deduped_count`
- `SearchResponse.finalized_count`

## 当前目录

```text
app/
  api/
  connectors/
  domain/
frontend/
  llm/
  services/
config/
docs/
scripts/
```

关键文件：

- 后端入口：[app/main.py](app/main.py)
- 路由定义：[app/api/routes.py](app/api/routes.py)
- provider 注册：[app/services/provider_registry.py](app/services/provider_registry.py)
- 搜索主流程：[app/services/search_service.py](app/services/search_service.py)
- provider runtime：[app/services/provider_runtime.py](app/services/provider_runtime.py)
- Redis runtime：[app/services/redis_runtime.py](app/services/redis_runtime.py)
- prompt 集中管理：[app/prompts.py](app/prompts.py)
- LLM 客户端：[app/llm/client.py](app/llm/client.py)
- 配置文件：[config/config.yaml](config/config.yaml)
- 配置加载：[config/settings.py](config/settings.py)

## 当前架构判断

当前代码结构的优点：

- 已经形成统一入口和统一 schema
- connector 接口风格基本一致
- provider 开关、public 状态和 mode 能力已被纳入配置层
- 本地调试体验已经具备最小闭环
- provider 共享运行时策略已开始收口到统一层
- Redis 缓存和 provider 级请求控制已完成首版接入

当前仍存在的结构性缺口：

- `search_service.py` 已退化为薄封装，但共享逻辑仍集中在 `search_common.py`
- 还没有独立的 orchestrator、resolver、日志与指标模块
- 去重已补上 DOI 标准化，但仍属于 MVP 级多源合并
- 当前 Quick 的 semantic 分数仍是轻量 embedding rerank，不是成熟学习排序器
- provider runtime/policy 仍是第一版，日志、错误码、自动测试与更细粒度策略仍待补齐

## 当前稳定性与可用性说明

当前主链路实测可跑通的组合：

- OpenAlex
- Semantic Scholar
- arXiv（在 Redis 缓存与 provider 限流控制下可跑通，但需严格尊重公开配额）

其余源的现状：

- CORE：connector 已实现，可作为补充召回源继续验证稳定性
- IEEE Xplore：connector 已实现，但更适合在有明确需求或指定来源时启用
- Unpaywall：更适合作为 `OA/fulltext resolver`，不建议当主搜索源
- arXiv：已接入 Redis 队列、缓存和单连接控制，但热门 query 在公开配额下仍可能触发 `429`

## 已知问题

### 1. Quick / Deep 仍偏 MVP

当前 `quick` 和 `deep` 已可用，并且已经拆成两条独立通道，但整体仍然偏 MVP。

这意味着：

- Quick 已有 hybrid rerank，但排序策略仍较轻量
- Deep 已有按 source 逐篇 judge，但硬过滤条件仍较少
- 两条通道共享 planner 和基础召回层，后续还可继续向更强的 source-aware orchestration 演进

### 2. 去重仍不够强

当前去重逻辑主要依赖：

- DOI 标准化
- `title + year + first_author`

这会带来一个典型问题：

- 当前已经能处理 `doi.org/...` 与裸 DOI 的差异
- 但多源元数据融合仍不够丰富，例如 citation、venue、publication type 还没有完整合并策略

### 3. arXiv 限流严格

当前代码已落地首版 provider runtime 控制，并为 arXiv 接入：

- Redis 缓存
- Redis 分布式锁
- provider 级串行队列
- 最小请求间隔控制
- `429` 退避重试

但仍要注意：

- arXiv 公共接口额度非常紧
- 热门 query 在高频 smoke test 下仍可能返回 `429`
- 多实例部署时必须共用同一个 Redis，不能绕过官方限制

### 4. 多语言 query 规划与词法打分仍偏英文中心

当前主要问题：

- intent planner prompt 已经补上“非英文 query -> 学术英文 `rewritten_query`”与实体保留规则，但这只解决了 query planning 的一部分问题
- `normalize_text()` 已改成 Unicode-aware，并为 CJK 增加了 fallback tokenization，但没有真正的翻译能力时，多语言 heuristic planner 仍不可能替代 LLM rewrite
- 启发式 fallback planner 已不再完全丢掉 CJK，但在没有 LLM planner 时，复杂中文 query 的英文学术重写能力仍然有限
- Deep 召回 query bundle、Quick lexical rerank 与 Deep heuristic scoring 已开始优先使用 `intent.rewritten_query`，但 bilingual query policy 仍可继续按 source 做细化

这意味着：

- 仅仅把 prompt 和 Deep 召回层补到英文 rewrite 还不够
- 对 OpenAlex、Semantic Scholar、arXiv、CORE 这类英文为主的数据源，英文 rewrite 会改善召回
- 但如果不同时补强 lexical normalization 和 bilingual query strategy，中文或其他非英文 query 在 rerank / judge 阶段仍会吃亏

建议方向：

- 保留原始 query，同时生成面向英文论文源的 `rewritten_query`
- 在 prompt 中明确保留术语实体，不要把 acronym、数据集、模型名和作者名翻坏
- 让 provider query policy 能按 source 选择 original-first、English-first 或 bilingual query variants
- 继续把 `intent.rewritten_query` 贯穿到 Quick / Deep 的 lexical scoring 和 heuristic scoring
- 把 `normalize_text()` 和相关 lexical scoring 改成 Unicode-aware，或至少为 CJK 增加 fallback tokenization

### 5. 复杂组合查询的 `deep` 已完成第一轮收敛，但仍需继续提纯

当前已完成：

- `SearchIntent` 已新增 `criteria` 和 `logic`
- `deep` 已默认生成面向复杂组合查询的 `query bundle`
- `query_hints` 已收紧为 1-4 个词的 provider-friendly 检索短语，避免把整句 prompt 或指令语直接送进 bundle
- 组合条件 `c4` 已保留独立 query 位，并进入 `criteria-and` 合取查询
- `quick` / `deep` 内部召回接口已开始统一，并补上 `retrieval_traces`
- `deep` 已拆出 provider-specific recall/query rendering，不同 source 不再共享同一种 raw query
- heuristic 预评分已支持 criterion-level support 与 required coverage
- `LLM judge` 已支持 criterion-level judgment
- 已落地动态送审窗口：full coverage 保底送审、coverage band 轮转送审、低产出车道 early-stop
- 组合条件的支持阈值已调高，heuristic/LLM 融合后也不再用 `or + max` 把 coverage 人为抬满
- 复杂查询已支持动态放大 query bundle 和 `llm_top_n_per_source` 预算
- `deep` 已增加最终 hard prune，默认只保留 `keep + 高分 maybe`
- 响应和调试脚本已补 `raw_recall_count / deduped_count / finalized_count`
- `deep` 默认召回预算已可通过 `retrieval.deep.limit_per_source_default` 配置

当前剩余问题：

- provider-specific deep query policy 仍然偏启发式，source-aware recall 还可以继续细化
- `2-of-4 / 3-of-4` 这类放宽组合还没有补上，仍可能漏掉“满足大部分条件但未全部显式写全”的候选
- 复杂组合查询里 `c4` / combination criterion 的证据要求仍可继续收紧
- `criteria-and` 和部分 criterion query phrase 仍可能有重复或串味
- criterion-level evidence 目前主要依赖标题和摘要，还没有扩展到更丰富的 metadata / fulltext 信号
- 当前虽已补基础调试计数，但还缺按 `(source, query_variant)` 车道展开的更细粒度观测

建议方向：

- 继续收紧组合条件 `c4` 的证据要求，尽量要求“明确存在独立 text retriever + graph retriever 的联合方案”
- 在 query bundle 中增加 `2-of-4 / 3-of-4` 放宽组合，覆盖“满足大部分条件但未在标题/摘要中把所有条件都显式写全”的相关论文
- 继续清洗 `criteria-and` 与 criterion query phrase，减少重复、串味和过长短语
- 为不同 provider 增加更细的 source-aware query bundle 策略，并继续打磨 deep query renderer
- 在现有动态送审窗口之上补 lane 级 debug 统计和更细的观测指标
- 继续增强 criterion-level evidence、hard filters 与 fulltext resolver 协同

### 6. Unpaywall 更适合作为 resolver

当前更推荐：

- 保留 Unpaywall probe
- 后续为 `resolve/fulltext` 接口服务

而不是把它当作常规 quick/deep 主召回源。

### 7. 暂无测试目录

当前仓库主要依赖：

- 本地脚本验证
- provider live probe
- 人工 smoke test

还没有系统化自动测试。

## 环境要求

建议：

- Python `3.10+`
- 已准备 `.env`
- 已安装 `requirements.txt`

安装依赖：

```bash
pip install -r requirements.txt
```

## 配置说明

1. 复制环境变量模板：

```bash
copy .env.example .env
```

2. 在 `.env` 中填入已有凭证：

- `OPENALEX_API_KEY`
- `SEMANTIC_SCHOLAR_API_KEY`
- `CORE_API_KEY`
- `UNPAYWALL_EMAIL`
- `IEEE_XPLORE_API_KEY`
- `REDIS_USERNAME`
- `REDIS_PASSWORD`
- `LLM_API_KEY`
- `EMBED_API_KEY`

3. 检查 `config/config.yaml` 中各 source 的开关：

- `enabled`
- `public_enabled`
- `supports_quick`
- `supports_deep`
- `supports_fusion`

4. 检查模型配置：

- `llm.provider`
- `llm.model`
- `llm.api_base`
- `llm.api_interface`
- `llm.api_interface_preference`
- `llm.temperature`
- `embedding.provider`
- `embedding.model`
- `embedding.api_base`
- `embedding.dim`
- `embedding.batch_size`

5. 检查 Redis 与 provider runtime 配置：

- `redis.host`
- `redis.port`
- `redis.db`
- `redis.key_prefix`
- `sources.<provider>.runtime.batch_mode`
- `sources.<provider>.runtime.cache_backend`
- `sources.<provider>.runtime.cache_ttl_seconds`
- `sources.<provider>.runtime.rate_limit_backend`
- `sources.<provider>.runtime.min_interval_seconds`
- `sources.<provider>.runtime.serialize_requests`

6. 检查 retrieval 相关配置：

- `retrieval.default_top_k_return`
- `retrieval.deep.limit_per_source_default`
- `retrieval.deep.llm_top_n_per_source`
- `retrieval.deep.max_dynamic_llm_top_n_per_source`
- `retrieval.deep.final_high_score_maybe_threshold`
- `retrieval.deep.final_high_score_maybe_min_coverage`

当前模型接口策略：

- `llm.api_interface=auto`
  - 运行时自动兼容 `responses` 和 `chat_completions`
- `llm.api_interface_preference=responses`
  - 在 `auto` 模式下优先尝试 `responses`

## 启动服务

开发模式启动：

```bash
uvicorn app.main:app --reload
```

说明：

- 默认配置按 `127.0.0.1:6379` 连接 Redis
- 若 Redis 不可用，部分 provider 会退化到本地请求控制，但无法提供跨进程共享缓存/限流

默认启动后可访问：

- `http://127.0.0.1:8000/v1/health`
- `http://127.0.0.1:8000/v1/providers`
- `http://127.0.0.1:8000/v1/providers/status`

## 独立前端测试页

当前仓库已补一个完全独立的静态测试页：

- 页面目录：`frontend/`
- 页面文件：`frontend/index.html`
- 独立代理脚本：`frontend/dev_server.py`

这个前端不会挂进当前 FastAPI 应用内部，只是一个单独的测试界面，用来：

- 输入 query
- 切换 `quick` / `deep`
- 直接调用现有搜索接口
- 导入 `scripts/outputs/*.json` 做纯展示测试

推荐启动方式：

1. 先启动当前后端：

```bash
uvicorn app.main:app --reload
```

2. 再启动独立前端代理：

```bash
python frontend/dev_server.py
```

3. 打开浏览器访问：

```text
http://127.0.0.1:8080
```

说明：

- 页面默认请求 `/api/search/quick` 和 `/api/search/deep`
- `frontend/dev_server.py` 会把 `/api/*` 代理到 `http://127.0.0.1:8000/v1/*`
- 如果你已经有放开 CORS 的后端地址，也可以在页面里直接改 `API Base URL`
- 如果只想看展示效果，不跑接口也可以直接导入 `scripts/outputs/search_*.json`

## 调试脚本

### 1. 检查 provider 配置与 live probe

```bash
python scripts/run_provider_probes.py
```

### 2. 本地执行 quick search

```bash
python scripts/run_quick_search.py transformer
```

### 3. 用统一脚本测试 quick 或 deep

```bash
python scripts/run_search.py "transformer" --mode quick
python scripts/run_search.py "transformer" --mode deep
```

常用参数：

- `--mode quick|deep`
- `--limit-per-source 8`
- `--sources openalex,semanticscholar,core`
- `--public-only`
- `--disable-llm`
- `--disable-intent-planner`
- `--llm-top-n 8`
- `--raw`

脚本输出当前会附带：

- `raw_recall_count`
- `deduped_count`
- `finalized_count`

如果不传 `--limit-per-source`：

- `deep` 会读取 `config/config.yaml` 中的 `retrieval.deep.limit_per_source_default`
- 当前默认值为 `10`

如果不传 query，脚本会进入交互式输入：

```bash
python scripts/run_search.py --mode deep
```

## 下一步建议

建议按这个顺序继续推进：

1. 继续补强统一标准化与去重，形成更完整的 `CanonicalPaper`
2. 补强多语言 query planning 与 lexical normalization，形成“原始 query + 英文 rewrite + source-aware query policy”的统一策略
3. 继续收敛已经落地的 `deep` 复杂组合查询链路，优先补齐 `2-of-4 / 3-of-4` 放宽组合、更严格的 `c4` 证据要求、更细的 source-aware recall/query policy，以及更稳定的 criterion-level evidence
4. 把共享 planner / recall / dedup 继续从 `search_common.py` 中拆成更清晰的模块
5. 继续把 `provider runtime/policy` 扩展到更细粒度的 query policy、日志和观测指标
6. 继续增强 Quick Search 的 hybrid ranking
7. 继续增强 Deep Search 的硬规则过滤与更稳定的 per-source LLM judge 链路
8. 完成 `POST /v1/search/fusion`
9. 完成 `POST /v1/resolve/fulltext`
10. 增加统一日志、错误码和自动测试
11. 在现有独立测试页基础上，再做正式前端、Django 集成层和 skill 封装

## 相关文档

- 架构计划书：[paper_search_agent_architecture_plan_zh.md](paper_search_agent_architecture_plan_zh.md)
- Quick / Deep 流程架构图：[docs/quick_deep_search_architecture_zh.md](docs/quick_deep_search_architecture_zh.md)
- OpenAlex 调研：[docs/openalex_api_research_zh.md](docs/openalex_api_research_zh.md)
- Semantic Scholar 调研：[docs/semanticscholar_api_research_zh.md](docs/semanticscholar_api_research_zh.md)
- CORE 调研：[docs/core_api_research_zh.md](docs/core_api_research_zh.md)
- IEEE 调研：[docs/ieee_xplore_api_research_zh.md](docs/ieee_xplore_api_research_zh.md)
- Unpaywall 调研：[docs/unpaywall_api_research_zh.md](docs/unpaywall_api_research_zh.md)
- arXiv 调研：[docs/arxiv_api_research_zh.md](docs/arxiv_api_research_zh.md)
- Crossref 调研：[docs/crossref_api_research_zh.md](docs/crossref_api_research_zh.md)
