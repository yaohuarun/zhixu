# 中文检索评估

导入 `fixtures/retrieval/ingestion.txt`、`synchronization.txt`、`retrieval.txt` 到独立测试知识库。`questions.json` 包含六个有依据问题和两个无依据问题，并标注目标文档；`questions-holdout.json` 为四个独立复核问题。运行 `scripts/evaluate_retrieval.py`，报告同时保存关键词、向量、RRF 和重排序的排名以及原文和来源名称。`guide.txt` 保留为导入及浏览器契约样例。

当前参数：每路 40 个候选，最多扩大向量召回到 640，RRF k=60，最多 8 个上下文块，保守上下文预算 12000 UTF-8 字节。重排总输入保守预算 120000 字节。`RAG_RELEVANCE_THRESHOLD` 未启用。

真实 `qwen3.7-text-embedding-flash` / `qwen3.7-text-rerank` 验收已完成，所有查询均无降级。首次命中结果：关键词 5/6，向量 6/6，RRF 6/6，重排 6/6；四路 Top-3 均为 6/6。原始结果见 [真实检索报告](reports/real-retrieval-report.json)。

校准集有依据文档的最低重排分数为 0.770572，无依据问题的最高分为 0.325541，二者中点取四位小数得到 **0.5481**。启用该截止值的独立复核中，两个有依据问题均选中目标来源，两个无依据问题均没有入选块，结果 **4/4**。见 [校准报告](reports/retrieval-calibration.json) 与 [复核报告](reports/real-retrieval-holdout.json)。

该值仅适用于这套小型说明语料，系统默认仍不设置全局阈值；上线业务资料需重新校准。本地确定性向量或固定分数只能验证契约，不能选择真实相关性阈值。

```powershell
.venv/Scripts/python scripts/evaluate_retrieval.py <测试知识库ID> --output data/real-retrieval-report.json
.venv/Scripts/python scripts/calibrate_retrieval.py data/real-retrieval-report.json
# 在独立评估进程中使用上述报告的阈值，不修改业务部署配置：
$env:RAG_RELEVANCE_THRESHOLD='0.5481'
.venv/Scripts/python scripts/evaluate_retrieval.py <测试知识库ID> --dataset fixtures/retrieval/questions-holdout.json --output data/real-retrieval-holdout.json
Remove-Item Env:RAG_RELEVANCE_THRESHOLD
```

校准时分别检查有依据问题的首位命中和 Top-k 覆盖，再检查两个无依据问题是否被错误接受。用真实业务标注扩展负样本，单独保留评估集，不用少量示例上的最高分直接当作通用阈值。改动模型、切块或领域词典后重新运行，并保留参数与模型配置版本。
