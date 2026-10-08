# 知序 · RAG 知识工作台

FastAPI + Vue3 的文档知识库与多轮问答系统，使用 PostgreSQL 和 Qdrant。支持文件/文件夹上传、服务器目录增量同步、文字及扫描 PDF、DOC、DOCX、TXT；扫描页使用本地 OCR，回答使用外部 DeepSeek，Embedding 使用 `qwen3.7-text-embedding-flash`，重排序使用 `qwen3.7-text-rerank`。

## Docker 启动

1. 复制 `.env.example` 为 `.env`，填写三个模型服务配置。DeepSeek 必须填写实际可调用模型 ID；百炼地址中的 `{WorkspaceId}` 要替换为业务空间 ID，地域与密钥必须匹配。Embedding/Rerank 使用各自完整专用 endpoint，不把聊天地址当作其他接口。
2. 在 `data/sources/` 放置需同步的服务器资料，或把 `SOURCES_HOST_PATH` 改为服务器绝对目录。
3. 执行：

```sh
docker compose build
docker compose up -d postgres qdrant
docker compose run --rm migrate
docker compose run --rm --no-deps worker python -m app.parsing --init-ocr
docker compose up -d
```

打开 http://localhost:8080 。数据库默认仅绑定本机 55432，Qdrant 为 56333，API 为 8000。首次 OCR 初始化会下载本地模型，后续模型保存在持久卷中；在受限网络环境可先在联网环境初始化卷，再迁移模型资产。

模型未配置时仍可管理知识库，但相关任务或问答会明确提示未配置；不会返回模拟回答。系统没有登录与权限管理。

## 使用

- 创建知识库，添加“文件 / 文件夹上传”数据源，选择文件或文件夹。相对路径作为文件身份，同路径再次上传会增量更新；未上传的旧文件不会被批量删除。
- 添加“服务器目录”数据源，填写**容器内**路径，例如 `/data/sources/manuals`。配置同步间隔或点“立即同步”。只有完整扫描成功后才移除消失文件，目录或子目录不可访问时不执行删除。
- 查看“处理任务”的进度及文件级错误，失败任务可以重试。文字 PDF 直接提取，扫描页走本地 OCR，旧 DOC 转 DOCX 后提取。强制 OCR 操作仅用于已有 PDF 的重新识别。
- 查看文档片段，然后进入“知识问答”选择知识库。回答流式显示，支持连续追问、取消和重试；点击来源查看回答当时的片段快照。已删除原文的历史引用会明确标记不可再打开原文件。

## 检索与切块

- 切块优先保留章节、段落、列表和表格；过长内容按段落、句子和长度递归拆分。目标/最大/重叠默认 600/900/80，标题前缀计入预算。
- 未配置匹配的本地 tokenizer 时使用 `utf8-byte-upper-budget-v1` 保守预算；界面显示“预算计数”，不将字节数误称为模型实际 token 数。可配置 `RAG_TOKENIZER_PATH` 和 `RAG_TOKENIZER_MODEL`，二者必须与 Embedding 模型匹配。
- 中文关键词使用 jieba 分词和 PostgreSQL 全文排序；向量使用 Qdrant cosine。两路各召回 40，RRF 融合去重，再外部重排序，最多取 8 块作为回答依据。均可调。
- 重排序不可用时退回融合排名并显示降级，向量分支不可用时可使用关键词分支。两路同时不可用则报错。
- 新文档版本索引完成后才切换生效版本；修改失败时旧版仍可检索。模型/维度配置变化后，在知识库任务页面执行“重建向量索引”，新集合完全覆盖当前版本后再切换。
- `RAG_RELEVANCE_THRESHOLD` 默认不设。重排序分数是请求内的相关程度，不是跨请求的绝对可靠性指标；用自己的标注问答集校准后再配置。没有检索依据时明确说明资料不足。

## 运行配置

所有配置以 `RAG_` 为前缀，见 `.env.example` 和 `backend/app/config.py`。密钥仅在服务端使用，不返回浏览器或日志。OCR 在本地识别，但文档检索文本会发送到 Embedding 服务，候选片段会发送到 Rerank 服务，问答问题、有限会话历史与入选片段会发送到 DeepSeek。

问题改写固定关闭 DeepSeek 思考模式，避免推理耗尽短输出预算。回答默认 `RAG_DEEPSEEK_THINKING=disabled`、`RAG_OUTPUT_TOKENS=4096`；需要思考模式可显式启用，并相应增加输出预算、超时及 `RAG_GENERATION_CONTEXT`。[DeepSeek 思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。

可调资源包括上传大小、PDF 页数、渲染像素与分辨率、转换/解析超时、模型并发及有限重试。默认 Worker 单进程低并发。CPU OCR 占用较多资源；可增加 Worker，但任务队列会串行处理同一文档或数据源。

```sh
docker compose ps
docker compose logs --tail 100 api worker
curl http://localhost:8000/api/health/live
curl http://localhost:8000/api/health/ready
docker compose stop
```

`live` 只说明进程存活，`ready` 检查 PostgreSQL/Qdrant。模型就绪与本地模型资产在运行配置和任务错误中单独展示。

## 本地开发

Python 3.11/3.12、Node 22+、Docker：

```sh
python -m venv .venv
# Windows 使用 .venv/Scripts/python.exe，Linux 使用 .venv/bin/python。
.venv/Scripts/python.exe -m pip install -c backend/constraints.txt -e "./backend[dev]"
docker compose up -d postgres qdrant
cd backend
../.venv/Scripts/python.exe -m alembic upgrade head
../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另一个终端：
cd frontend
npm ci
npm run dev
```

本地开发默认 PostgreSQL/Qdrant 使用 `127.0.0.1`，避免 Windows IPv6 localhost 等待。开发模式读取项目根 `.env`，进程环境变量优先；推荐 Docker 正式部署。OCR 和 LibreOffice 可直接使用 Worker 容器验证。

后台索引需要另一个终端运行 `python -m app.worker`（使用同一虚拟环境与配置）。本地使用扫描件或 DOC 时也需要 OCR 依赖、模型资产和 LibreOffice；首选容器 Worker。

## 验证

不要在生产数据库运行测试；测试会创建并删除自己的知识库。单元/接口契约测试采用受控模型响应，不能代表真实模型效果。

```sh
# 单元测试，不要求 PostgreSQL：
cd backend
../.venv/Scripts/python.exe -m pytest tests/test_core.py -q
# 独立集成数据库：
docker compose exec -T postgres psql -U rag -d postgres -c "CREATE DATABASE rag_test;"
# PowerShell：
$env:RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55432/rag_test'
../.venv/Scripts/python.exe -m alembic upgrade head
../.venv/Scripts/python.exe -m pytest tests -q
../.venv/Scripts/python.exe -m mypy app
../.venv/Scripts/python.exe -m ruff check app tests
# 前端：
npm run build
```

真实模型验证须填入可调用密钥，运行 `python scripts/live_smoke.py`；该脚本会调用服务并产生相应费用，不打印密钥。验收状态见 `docs/validation.md`。

浏览器契约流程可在项目根运行 `.venv/Scripts/python scripts/browser_contract.py`，会启动并关闭独立测试 API、Worker 与 HTTP 模型夹具。真实服务效果另行运行 `scripts/evaluate_retrieval.py <知识库ID>`，评估方法见 `docs/retrieval-evaluation.md`。

## 备份、恢复与回滚

以 PostgreSQL 生效版本为事实来源，业务数据库、Qdrant 和原文快照必须协调备份。`python scripts/backup.py --output <目录>` 暂停 API/Worker/前端写入，导出 PostgreSQL、Qdrant 集合快照和文件/模型卷，完成后恢复原先运行服务。模型环境配置单独私密保存。

恢复到空环境时先启动数据库和 Qdrant，按 `docs/operations.md` 恢复协调备份与原文卷，确认索引覆盖当前版本，再启动写入服务。不执行 `docker compose down -v`，该命令会删除持久化卷。

回滚应用采用上一个镜像和兼容配置。不兼容数据库迁移应停止写入并恢复整套协调备份，不能只回退向量库。旧版本清理失败可重试，生效版本校验不会向新请求暴露旧块。
