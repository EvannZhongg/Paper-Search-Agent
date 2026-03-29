# Paper Search Agent

一个面向论文检索场景的独立后端服务原型。当前仓库已经完成多检索源接入、基础 API、配置加载、连通性探测与本地调试脚本，但整体仍处于 MVP 阶段，离“可长期演进的检索核心服务”还有一轮架构收敛。

当前项目优先作为：

- 独立后端接口服务
- 后续可被 Django 调用
- 后续可封装为 skill 给其他 agent 调用

## 当前状态

更新日期：2026-03-29

当前可以确认的进度：

- FastAPI 服务已可启动
- `quick` / `deep` 两条基础检索链路已可返回真实结果
- provider 配置、凭证注入、连通性探测脚本已具备
- 多个 connector 已完成首版接入

当前阶段判断：

- 这是一个“后端原型已跑通”的项目，不再是纯设计稿
- 但还不是“架构计划 fully landed”的版本
- 当前更适合视为：`可运行 MVP + 待收敛的核心引擎`

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
- Deep Search 支持在有可用 LLM 配置时做结构化判定

## 当前实际实现方式

### Quick Search

当前流程：

1. 对用户 query 做 intent planning
2. 生成 `rewritten_query`
3. 把 `rewritten_query` 下发给可用 source 做多源召回
4. 对结果做基础去重
5. 使用原始 query 做启发式词法打分并返回

注意：

- 当前 Quick Search 还不是架构计划中的“embedding + keyword hybrid ranker”
- 目前仍是启发式相关性打分

### Deep Search

当前流程：

1. 对用户 query 做 intent planning
2. 走与 quick 相同的多源召回链路
3. 对候选结果做启发式相关性判断
4. 若 LLM 已配置且启用，则对 Top-N 做结构化 LLM judge
5. 若 LLM 未启用，则回退为启发式 deep 评分

注意：

- 当前 Deep Search 的召回层仍直接复用 `quick_search` 的 source 调用方式
- 还没有独立的硬规则过滤器
- 还没有完整的 `LLM Precision Judge` 服务化拆分

### 当前数据模型

已经具备最小统一结构：

- `SearchIntent`
- `PaperResult`
- `SearchResponse`
- `ProbeResult`

但还未完全达到目标架构中的：

- 丰富版 `SearchIntent`
- 完整版 `CanonicalPaper`
- 独立版 `JudgmentRecord`

## 当前目录

```text
app/
  api/
  connectors/
  domain/
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

当前仍存在的结构性缺口：

- `search_service.py` 同时承担 planner、召回、去重、排序、judge，职责过重
- 还没有独立的 orchestrator、normalizer、ranker、resolver 模块
- Quick / Deep 的行为还没有完全分叉成两条真正独立的检索通道
- 去重仍是 MVP 水平，跨源 DOI 标准化还不完整
- embedding 配置已预留，但当前尚未接入实际向量排序
- 缺少统一日志、错误码、缓存、限流与测试体系

## 当前稳定性与可用性说明

当前主链路实测可跑通的组合：

- OpenAlex
- Semantic Scholar

其余源的现状：

- CORE：connector 已实现，可作为补充召回源继续验证稳定性
- IEEE Xplore：connector 已实现，但更适合在有明确需求或指定来源时启用
- Unpaywall：更适合作为 `OA/fulltext resolver`，不建议当主搜索源
- arXiv：connector 已实现，但公开接口限流严格，当前仍需更强的 provider 级限流和队列

## 已知问题

### 1. Quick / Deep 仍偏 MVP

当前 `quick` 和 `deep` 已可用，但两者差异主要在后处理，而不是完整独立的检索策略。

这意味着：

- Quick 还没有真正的向量化快速筛选
- Deep 还没有真正的规则过滤 + 精细 judge 分层

### 2. 去重仍不够强

当前去重逻辑主要依赖：

- DOI 原值
- `source + title + year`

这会带来一个典型问题：

- 不同 source 返回的 DOI 格式不一致时，可能无法正确跨源合并同一篇论文

### 3. arXiv 限流严格

当前代码已加入基础节流，但仍不足以保证稳定使用。

后续需要：

- provider 级队列
- 更严格的单连接控制
- 结果缓存

### 4. Unpaywall 更适合作为 resolver

当前更推荐：

- 保留 Unpaywall probe
- 后续为 `resolve/fulltext` 接口服务

而不是把它当作常规 quick/deep 主召回源。

### 5. 暂无测试目录

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

默认启动后可访问：

- `http://127.0.0.1:8000/v1/health`
- `http://127.0.0.1:8000/v1/providers`
- `http://127.0.0.1:8000/v1/providers/status`

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
- `--limit-per-source 3`
- `--sources openalex,semanticscholar,core`
- `--public-only`
- `--disable-intent-planner`
- `--enable-llm`
- `--llm-top-n 8`
- `--raw`

如果不传 query，脚本会进入交互式输入：

```bash
python scripts/run_search.py --mode deep
```

## 下一步建议

建议按这个顺序继续推进：

1. 先补强统一标准化与去重，形成更完整的 `CanonicalPaper`
2. 把 planner / orchestrator / ranking / judge 从 `search_service.py` 中拆开
3. 为 Quick Search 接入真正的 hybrid ranking 或 embedding rerank
4. 为 Deep Search 增加硬规则过滤与更稳定的 LLM judge 链路
5. 完成 `POST /v1/search/fusion`
6. 完成 `POST /v1/resolve/fulltext`
7. 增加日志、错误码、缓存、限流和测试
8. 最后再做前端页面、Django 集成层和 skill 封装

## 相关文档

- 架构计划书：[docs/paper_search_agent_architecture_plan_zh.md](docs/paper_search_agent_architecture_plan_zh.md)
- OpenAlex 调研：[docs/openalex_api_research_zh.md](docs/openalex_api_research_zh.md)
- Semantic Scholar 调研：[docs/semanticscholar_api_research_zh.md](docs/semanticscholar_api_research_zh.md)
- CORE 调研：[docs/core_api_research_zh.md](docs/core_api_research_zh.md)
- IEEE 调研：[docs/ieee_xplore_api_research_zh.md](docs/ieee_xplore_api_research_zh.md)
- Unpaywall 调研：[docs/unpaywall_api_research_zh.md](docs/unpaywall_api_research_zh.md)
- arXiv 调研：[docs/arxiv_api_research_zh.md](docs/arxiv_api_research_zh.md)
