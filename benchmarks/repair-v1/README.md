# repair-v1 冻结评估集

20 份自制 HTML，两个虚构品牌，44 个场景、39 种不同问题文本。其中 24 个预标注为可答，20 个预标注为应拒答或要求补充范围。覆盖同义词、故障码、部件号、数值、步骤、警告、表格、未知适用范围、待复核、撤下、替换和多版本参数冲突。

这些新资料与之前 `examples/retrieval`、`examples/qa` 分开。生产检索／问答代码固定基线为 `381040c`，问题及证据标签在第一次运行评估前写入 `dataset.json` 和 `freeze.json`。本轮没有为了这些结果修改生产排名和问答规则。

**标签由助手根据原文件编写，不是外部人工标注。** 数据集与已有规则都来自同一项目环境，可能存在设计偏差；不能宣称为人工盲测、独立行业专家评估或真实维修效果。当前外部人工复核为 0。

## 文件

- `dataset.json`：资料元数据、导入顺序、预期发布状态、问题、预期回答状态、支持文件、文字锚点和警告。
- `corpus/`：公开自制来源，所有参数仅为软件测试示例。
- `freeze.json`：数据／标签摘要与生产代码基线摘要。冻结用于检查意外改动，不是数字签名或可信时间戳。
- `review-template.csv`：逐题复核表，署名和判定保持空白，不伪造人工验收。

## 运行

```bash
.venv/bin/python scripts/evaluate_benchmark.py --modes bm25 \
  --output work/bm25-evaluation.json --report work/bm25-evaluation.md

# 已准备固定版本本地 BGE 模型时运行，不下载模型
HF_HOME="$PWD/work/huggingface" HF_HUB_OFFLINE=1 \
  .venv/bin/python scripts/evaluate_benchmark.py --modes bm25 semantic hybrid
```

默认输出 `docs/benchmark-results.json` 与 `docs/冻结题集评估结果.md`。新测量建议用 `--output/--report` 写入 `work/`，避免不必要地覆盖已公布结果。

CI 只运行 BM25，输出到 `work/`，不下载模型。数据摘要不匹配、发布状态错误、引用或检索范围完整性错误会失败；召回／拒答质量失配作为数据报告，不把所有预标注答案硬写成测试预期来制造通过。

评估分母分别记录可答题、应拒答题、实际已答题及返回候选；零分母使用 `null`，不算 100%。某题被多个模式执行不增加独立样本量。前三区分“匹配标题”和“匹配正文文件及证据锚点”，只计后者。

## 人工复核流程

1. 复制 `review-template.csv` 到 `work/reviews.csv`。逐题核对原始 HTML、`dataset.json` 的范围、支持证据、预期状态及生成结果。
2. 填 `reviewer`、ISO 日期／时间 `reviewed_at`、`verdict=approve|reject` 和必要说明。`approve` 表示认可该题预标注标签；`reject` 表示不同意。空行保持待复核。字段为自行署名，系统不验证身份。
3. 以 `--reviews work/reviews.csv` 导入，完整结果继续保留所有场景，认可标签子集的指标另列。没有认可记录时该子集比率为 `null`。
4. 不能通过只复核容易的题宣称全量评估通过；应公布覆盖比例、拒绝标签的原因及评审来源。需要修改标签或来源时新建数据集版本并记录原因，不直接改本版冻结摘要。

示例命令：

```bash
.venv/bin/python scripts/evaluate_benchmark.py --modes bm25 \
  --reviews work/reviews.csv --output work/reviewed-results.json \
  --report work/reviewed-results.md
```

这些记录只是标签复核。若要测独立语义正确性，还需评审人逐条判断答复是否真正回答问题、引用是否支持每个结论、警告是否适用；当前的自动文件／锚点核对不代替这一工作。
