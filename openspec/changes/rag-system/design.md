# Design

## Context

见 proposal.md 的动机及五份能力规格。仓库只有 OpenSpec 初始化配置，没有既有代码、数据或接口。模型通过外部 API 调用，本地执行解析、OCR 与切块；部署目标为 Docker。下述库选择及运行参数是可实施的首版设计默认值，不代表已运行验证；实施时锁定兼容版本并完成容器验收。

## Goals / Non-Goals

**Goals:**
- 重任务与 HTTP 请求分离，任务中断后可恢复。
- 以数据库生效版本为准，实现跨数据库的可恢复发布，而不依赖分布式事务。
- 每个片段可追溯原文；中文关键词与语义召回都有独立评估样例。
- 默认 CPU 部署可运行，资源参数和检索参数均可配置。

**Non-Goals:**
- 登录、角色、权限、多租户隔离，以及外部网站采集。
- 实时文件监听、分布式集群编排和在线协同编辑。
- 完美重建所有扫描件版面；不对低质量 OCR 内容承诺准确识别。
- 首版不采用模型驱动的语义边界切分、代理自主工具调用或单独的全文搜索服务。

## Decisions

### 1. 服务组织与前端

采用 Vue3 + TypeScript + Vite，FastAPI + SQLAlchemy + Alembic。建议目录为 frontend/、backend/、deploy/、tests/。前端页面覆盖知识库列表/详情、数据源配置、文档列表与片段预览、任务进度和会话问答；使用真实接口状态，任务进度轮询，回答采用 fetch 流式读取 SSE。

```text
[Browser/Vue3] --> [Frontend proxy] --> [FastAPI]
                                          |
                           +--------------+--------------+
                           v                             v
                     [PostgreSQL]                     [Qdrant]
                           ^                             ^
                           |                             |
                      [Worker] --------------------------+
                           |
               [Parsing / Local OCR / Chunking]

[FastAPI] --> [DeepSeek / Embedding / Rerank APIs]
[Worker]  --> [Embedding API]
```

FastAPI 只执行短操作、入队和检索/问答编排，不使用进程内 BackgroundTasks 承载长时间索引。为避免首版增加 Redis，Worker 从 PostgreSQL 持久任务表领取任务，使用行锁、租约、心跳及过期回收；转换和 OCR 放入有超时的子进程。允许多个 Worker，但同一数据源同步串行，同一文档更新串行。Worker 调度到期目录同步任务，唯一任务键防止重复入队。

### 2. 数据模型及接口边界

- knowledge_bases：名称、状态、当前 index_config_id、切块配置。
- data_sources：类型、允许根内路径、同步间隔、上次完整扫描、状态。
- documents：数据源、相对路径唯一身份、当前生效版本、删除标记。
- document_versions：文件哈希、原文快照路径、解析/切块配置指纹、状态。
- chunks：版本、序号、原文、检索文本、来源 JSON、哈希、关键词索引；Qdrant point ID 由版本和块身份确定。
- index_configs / index_builds：模型、服务标识、维度、tokenizer、集合名称、重建状态；不保存明文密钥到业务响应。
- jobs / job_items：输入指纹、类型、阶段、计数、错误、租约、尝试次数、时间。
- embedding_cache：检索文本哈希 + Embedding 配置指纹作为键。
- conversations / messages / citations：消息顺序、选择知识库、请求幂等键、状态与引用快照。

候选 API：/api/knowledge-bases、/api/data-sources、/api/documents、/api/jobs、/api/conversations；支持上传、同步、重试、删除、片段读取、检索调试和 POST /api/conversations/{id}/messages 流式问答。统一错误结构包含 code、message、request_id，不包含密钥。服务配置首版由环境变量与部署配置提供，不额外实现密钥管理页面。

上传批次保留相对路径，不把批次内缺失文件视为删除。目录扫描路径以容器内路径为准，前端清楚说明挂载关系。对路径规范化和符号链接解析后的路径做根目录约束。原文件复制到受管理的不可变版本快照后再解析，避免读取过程中源文件变化；复制前后校验元信息，变化则重试，不发布不稳定输入。

### 3. 本地解析与 OCR

采用 PyMuPDF 提取 PDF 文本与坐标、渲染扫描页；本地 PaddleOCR/PP-Structure 管线识别中文、英文和可恢复版面。默认使用 CPU，不要求 GPU；模型资产在构建或显式初始化阶段下载到持久化位置，正式 OCR 不依赖外部识别 API。PDF 按文字数量、可读性与图像覆盖等可配置规则决定是否 OCR，可人工重试强制 OCR。

DOC 使用 LibreOffice headless 转换为 DOCX，独立临时目录和用户配置目录避免并发冲突；随后与 DOCX 一样提取标题样式、段落、列表、表格。TXT 优先 UTF-8/BOM，再受控尝试常见中文编码，失败明确报错。不要把 Word 转换产物页码当作原始文档页码。

对 OCR 断行、重复页眉页脚进行可追溯清洗，保存原始解析元素和规范化文本；不可靠的结构退回段落切分。表格转为带表头的文本表示，超长表格分行，超长单行继续拆分。替代方案为仅文字 OCR：部署更轻但丢失表格/版面信息，首版优先结构识别并保留降级路径。

### 4. 切块与缓存

按章节聚合段落，跨页章节可合并；不足目标大小的段落只在同章节合并，列表尽量完整。超过上限时按段落、句子、最后 token 边界递归切分。默认目标 600、最大 900、拆分重叠约 80 tokens；章节边界不重叠。标题前缀计入长度，过长前缀截短但保留完整标题元数据。

优先使用服务商公开且匹配所选模型的 tokenizer，固定本地版本；若无法获取精确 tokenizer，则采用明确标记的保守 UTF-8 字节计数预算，不宣称实际模型 token 数精确相同。两者均记录计数器标识；任何 API 输入限制错误触发进一步拆分并记录原因。默认不允许用英文字数或字符数冒充精确中文 token 数。

向量与关键词索引使用“文档名称 + 标题路径 + 原文”；展示使用原文。相同检索文本哈希且模型服务/模型/维度/指令配置一致时复用向量。页码、坐标、章节路径和原文范围重新绑定到当前版本。解析、清洗、切块、分词及计数器都记录配置版本；版本变化显式触发重建。

### 5. 增量同步与跨库发布

身份为 data_source_id + normalized_relative_path。相同哈希且配置不变跳过处理；重命名按删除与新增处理，但可复用哈希缓存。同步先完整枚举并记录扫描批次，发生遍历错误则禁止整轮删除；单个文件解析失败不影响其他文件更新，也不将该文件当作删除。

发布顺序：
1. 保存不可变输入快照，创建 building 版本及任务。
2. 解析、切块，保存 PostgreSQL 块，创建向量并 upsert 到 Qdrant；等待写入完成并校验块数。
3. 获取文档锁，校验输入代次未被更新或删除，在 PostgreSQL 单事务切换 active_version_id。
4. 入队清理旧向量与块；清理失败可重试，不能改变生效版本。

两个数据库没有原子提交。PostgreSQL 是生效版本唯一事实来源，关键词查询直接关联 active_version_id；向量查询首先按知识库和索引配置过滤，再核验候选版本是否当前生效。对暂存/旧向量造成的候选占用采用分批扩大召回到可配置上限；设置后台清理，达到上限时记录诊断。发布前验证 Qdrant 可检索性；绝不以 Qdrant 的单独 active 布尔标志决定版本。

检索在同一数据库快照中校验版本，并在组装上下文前再次剔除删除标记。已经生成中的回答保留当次引用快照，文档后续变更不改写旧回答来源。清理保留最小引用快照与定位，原文已删除时界面说明不可再打开原文件。

删除先事务写删除标记、阻止新检索和在途发布，再异步清理。知识库删除还需取消其任务或使其发布校验失败。故障恢复程序检查 building 版本、超期任务及待清理记录，使用确定性 point ID 和幂等任务实现可恢复处理。

Embedding 配置变化时建立新集合/索引配置，按文档重建并补齐重建期间变更；持知识库发布锁验证新索引覆盖全部当前版本后切换配置。检索在切换前沿用旧配置，重建失败保留旧索引。避免在原集合直接改变维度或混用模型向量。

### 6. 中文混合检索与重排序

关键词分支采用应用层中文分词（首版 jieba，可配置专有词词典），英文归一化；将分词后文本写入 PostgreSQL simple 配置 tsvector 并建 GIN，查询使用同一分词规则构造安全 OR tsquery，按 ts_rank_cd 排序。这里是关键词排序，不宣称 PostgreSQL 原生排名等于 BM25。相对引入 Elasticsearch 或数据库额外扩展，此方案保持当前技术栈和容器数量。

向量分支使用 Qdrant cosine 索引、知识库/版本/配置 payload 索引；默认模型 qwen3.7-text-embedding-flash、1024 维。查询和文档 Embedding 指令按服务接口区分并纳入配置指纹。

初始参数：两路各召回 40，RRF 常数 60、等权，去重后取最多 40 候选交给 qwen3.7-text-rerank，最终最多 8 块且受生成上下文预算限制。全部可配置；RRF 使用排名而非直接混加不同服务分数。重排服务使用百炼模型专用接口，不假设与聊天或 Embedding 接口通用；批次限制以官方文档和实际服务返回校验。

重排有限重试后失败时保留融合顺序并在 SSE 元数据与 UI 标记降级。任一召回分支失败可使用另一分支但标记降级；两路均失败返回检索错误。无候选直接说明资料不足。重排相关性阈值通过小型中文测试集校准，模型/配置变化后重新校准，不用未经校准的固定分数承诺相关性。

### 7. 多轮回答与模型适配

三个独立适配器分别处理 DeepSeek chat、百炼 Embedding 和 Rerank。DeepSeek 的具体模型 ID 必填部署配置；不把供应商名称作为模型 ID。地址、工作空间、区域、密钥、超时、并发和重试独立配置，遵循各接口格式和限流规则，日志只记录脱敏状态。

保存本轮原始问题，再让 DeepSeek 结合预算内历史生成独立检索问题；改写失败回退原问题。重排后块携带稳定引用编号。生成 prompt 将知识内容置于明确数据边界，要求依据来源回答、不执行文档中的指令、资料不足时明确说明。对返回引用编号进行白名单校验，未知编号不生成可点击引用。

历史、当前问题、来源和输出分别分配预算；按 DeepSeek 模型已配置上下文限制裁剪，超预算优先减少低相关块，再裁剪较旧历史，保留当前问题。首版不额外实现历史向量记忆或模型摘要。

POST SSE 事件包含 meta（改写/降级信息）、sources、delta、done、error，终止状态写入数据库。同会话只允许一个活动生成；幂等键确保重试不会重复用户消息。连接断开/用户取消时终止上游请求并保存 interrupted 内容，失败回答不作为下一轮完整助手历史。来源查看返回引用时保存的片段快照及定位，而不是随文档更新改变内容。

### 8. Docker 与运行配置

Compose 包含 frontend（静态资源与 /api 代理）、api、worker、postgres、qdrant，api 和 worker 共享后端代码但 Worker 镜像包含 OCR/LibreOffice。数据库任务队列不引入额外 broker。上传、版本快照、数据库、Qdrant 与 OCR 模型各自持久化；服务器数据源只读挂载到 /data/sources，用户填写容器路径。

提供 .env.example、配置说明、依赖版本锁定、迁移命令和 readiness。限定单文件大小、PDF 页数、渲染分辨率、并发 OCR 数、转换超时、API 超时和重试次数；默认 CPU Worker 从低并发开始。临时文件无论成功失败均清理。代理关闭 SSE 缓冲并配置超时。密钥不进入 Vite 构建或浏览器返回。

## Risks / Trade-offs

- OCR 的表格、双栏或模糊扫描结果不可靠 → 保留结构质量信息、片段预览与强制 OCR 重试，验收采用代表性样例。
- CPU OCR 慢、模型资产大 → 独立 Worker、资源预算、持久化模型及可调并发；GPU 加速可后续扩展。
- 中文分词和领域缩写召回不足 → 同分词查询、可配词典、混合召回和中文评估集；首版不承诺 BM25。
- 两库版本一致性及暂存候选占位 → 数据库生效版本校验、适度扩大候选、恢复扫描与幂等清理；覆盖崩溃窗口测试。
- 外部 API 超时/限流/费用 → 批量与并发预算、向量缓存、有限重试、明确降级和请求计量。
- 精确 tokenizer 不可获取 → 显式保守计数器、配置版本与真实 API 长度验收，不伪造精确 token 指标。
- 本地 OCR 不能阻止资料文本出站 → 部署文档明确说明 Embedding、Rerank、生成的文本发送范围。

## Migration Plan

1. 选择并锁定经过容器验证的依赖版本，配置模型 ID/区域/工作空间和密钥，初始化本地 OCR 模型。
2. 初始化持久化卷与只读数据源挂载，执行 Alembic 迁移，启动数据库、Qdrant、API、Worker 和前端。
3. 使用 PDF/扫描件/DOC/DOCX/TXT 样例完成导入、增量更新、中文检索和多轮引用验收。
4. 建立数据库、Qdrant、文件及配置的协调备份；恢复后核对生效版本和索引，缺失索引从快照重建。
5. 应用回滚使用上一镜像及兼容配置；存在不兼容数据库迁移时先停止写入，从协调备份恢复，不单独回退 Qdrant 导致版本错配。

## Open Questions

- DeepSeek 具体模型 ID、百炼区域/工作空间和服务配额：部署时配置与连通性检查确定，不改变三类适配器边界。
- 实际资料语言分布、领域词典及规模：用于调整 OCR、并发与召回参数，首版先用中文/英文样例验证。

## References

- [结构切块](https://docs.unstructured.io/api-reference/partition/chunking)
- [PaddleOCR 结构识别](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.md)
- [PostgreSQL 全文排名](https://www.postgresql.org/docs/17/textsearch-controls.html)
- [Qdrant 过滤](https://qdrant.tech/documentation/search/filtering/)
- [Embedding API](https://help.aliyun.com/en/model-studio/text-embedding-synchronous-api)
- [Rerank API](https://help.aliyun.com/zh/model-studio/text-rerank-api)
- [DeepSeek Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)
