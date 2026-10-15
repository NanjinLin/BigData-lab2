# MovieLens 1M 迭代一提交说明

本文件夹是迭代一的独立提交包，依据《引言_项目总体要求与汇报安排》和《迭代一_Hadoop数据清洗与Agent基础》整理。源码基线为本地 Git `main` 提交 `68a0b26`，另有提交包内的运行脚本和校验说明调整；数据与报告来自本提交包重新实跑的默认规则任务 `20260929151428Z-submit`。两份课程要求文件不在提交包内。

## 包内材料

- `service/`：自然语言 Agent、任务服务、HTTP 接口及前端；`hadoop_jobs/`：Hadoop Streaming 清洗和五维评分；`scripts/`：运行与环境准备脚本。
- `config/quality_rules_v1.json`：默认规则；`hadoop-conf/`：Hadoop 配置；`raw_data_manifest.json`：原始数据版本及哈希；`.gitattributes`：固定输入文件的换行字节。
- `ml-1m/ml-1m/{ratings.dat,users.dat,movies.dat}`：本次运行所登记的三份原始输入，供复现；`tests/`：自动测试。
- `docs/迭代一_清洗与评分规则_v1.md`：评分口径与清洗规则；`docs/迭代一_手动测试.md`：功能验证步骤；`docs/WSL_Hadoop_运行环境.md`：原机器的环境记录；`docs/迭代一_最新实跑结果.md`：本提交包所选任务的结果说明。
- `runs/20260929151428Z-submit/`：同一次 Hadoop 任务的完整净表 `cleaned.tsv`、处理审计 `clean_process.tsv`、正式 `report.json`、规则快照、清洗前后评分、状态与作业日志。旧任务和失败运行不纳入提交包。
- `verify_submission.py`：净表、报告、规则、源码及原始输入的文件一致性校验脚本；提交前运行。

## 系统与接口

用户在前端提出清洗和质量评估请求，Agent 选择 `run_hadoop_clean_and_score` 或受限参数工具 `run_hadoop_with_adjusted_rules`；任务服务调用 Hadoop 完成清洗前评分、清洗、净表导出和清洗后评分。Agent 回答结果追问前调用 `read_task_report`，以正式报告为依据。

在项目根目录设置自己的 `OPENAI_API_KEY`（不要写入文件），可选设置 `OPENAI_MODEL`，然后运行 `python -m service.app`，打开 `http://127.0.0.1:8765`。通过自然语言 Agent 发起新任务需要可用的模型 API、WSL、HDFS 和 YARN；直接运行 `scripts/run_from_wsl.sh` 的 Hadoop 清洗及评分不需要模型 API。本提交包的实跑采用后者，不能当作真实模型自动选择工具的验收证据。新 WSL 环境的准备参见 `scripts/setup_wsl_runtime.sh` 和 `docs/迭代一_手动测试.md`。不要对已有 NameNode 数据重新格式化。

主要 HTTP 接口：`POST /api/tasks` 发起任务；`GET /api/tasks` 列表；`GET /api/tasks/<任务 ID>` 状态；`GET /api/tasks/<任务 ID>/report` 报告；`GET /api/tasks/<任务 ID>/samples` 净表样例；`POST /api/tasks/<任务 ID>/ask` 结果追问；`GET /api/agent` 模型配置状态。接口实现在 `service/app.py`。

自动测试：在项目根目录运行 `python -m unittest discover -s tests -v`，并运行 `python verify_submission.py` 检查本提交包。真实模型与 Hadoop 的 Agent 端到端运行仍须在有 API 密钥的环境中验证；模拟模型测试和直接 Hadoop 实跑不能替代此项验收。

## 本次结果与版本

- 任务状态：`completed`；任务 ID：`20260929151428Z-submit`；四个 Hadoop 作业均完成。
- 原始数据版本：`raw-ml1m-r1b3c4690-uC0397B2E-mE256099E`；规则版本：`quality-clean-v1.0.0`；清洗数据版本：`clean-c91a5e196ca1af48c3da`。
- 净表共有 1,006,166 行（评分 996,113、用户 6,112、电影 3,941）；SHA-256 为 `c91a5e196ca1af48c3dae9d2f5a6d66164fd222931ba7d954426eb02df811fa7`，与本次报告一致。
- 默认规则文件与任务规则快照的 SHA-256 均为 `59915ec6317ca500e2a44f845fe6245b6659d10d5f530460ea1ce14c5d73ddb3`；Hadoop 四份源码拼接后的 SHA-256 与报告登记的 `8e3a6d0942e4f69cdf8cd06ef8dd2c593bbfc978a460e7f279f008cff1648b60` 一致。
- 时间边界：`T1 = 2000-12-01T23:59:59Z`；`T2 = 2000-12-28T23:59:59Z`。评分表训练、验证、测试记录数分别为 797,116、100,886、98,111。
- 评分、行数变化、处置方式与局限见 `docs/迭代一_最新实跑结果.md`，机器可读原值见该任务的 `report.json`。后续迭代应使用相同的数据、规则和时间边界，不混用其他任务的产物。
