# MovieLens 评分分类模块

本模块在迭代一 Hadoop 清洗任务的基础上预测评分是否 **≥4**。使用两个独立的分类算法：Logistic Regression（逻辑回归）与 Decision Tree（决策树）。训练产物、评估结果和模型版本均保存在对应 Hadoop 任务目录。

## WSL 运行

在原项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-classification.txt
python -m service.app --host 127.0.0.1 --port 8765
```

页面打开 `http://127.0.0.1:8765`，左侧点击 **评分分类 · 两算法对照**，选择已有的 Hadoop 清洗任务，点击 **开始训练**。训练任务异步执行，界面显示阶段和失败原因。完成后页面展示两种算法的验证/测试指标、模型版本；输入 `UserID` 和 `MovieID`，点击 **预测** 查看两种独立模型的预测类别、评分≥4的概率、训练期历史与结果。模型参数、版本和技术说明可展开查看或下载报告。无需大模型 Key 即可通过分类面板直接操作；自然语言 Agent 需要配置 Responses 兼容模型。

也可以从 WSL 命令行训练：

```bash
python scripts/run_classification.py --task-id 20260930T112146Z-b0d51382
# 或者自动选择最近一个已完成、带完整净表的任务：
python scripts/run_classification.py
```

训练始终复用该任务的 `runs/<task_id>/cleaned.tsv` 与 `report.json`，不重新清洗；必须具备完整净表，且 SHA-256、三表行数与正式报告一致。当前工作目录中净表若未放入 Git，可从原 Hadoop 机器复制到同名目录，或者按原流程重新执行一次 Hadoop 清洗。执行时应使用新产生任务的 ID。

## API

以 `TASK_ID` 代替具体任务 ID：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/tasks/TASK_ID/classification/train` | 异步训练两种分类器，JSON 请求体 `{}` |
| GET | `/api/tasks/TASK_ID/classification/status` | 训练阶段、状态、失败原因 |
| GET | `/api/tasks/TASK_ID/classification/report` | 两种算法参数、验证和测试评价、版本 |
| GET | `/api/tasks/TASK_ID/classification/report?download=1` | 下载评价报告 |
| POST | `/api/tasks/TASK_ID/classification/predict` | 独立调用指定算法预测 |

预测调用示例：

```bash
curl -s http://127.0.0.1:8765/api/tasks/TASK_ID/classification/predict \
 -H 'Content-Type: application/json' \
 -d '{"user_id":1,"movie_id":1193,"algorithm":"logistic_regression"}'
```

将 `algorithm` 改为 `decision_tree` 即可独立调用决策树。无模型、未完成训练、用户或电影不存在、版本不匹配和模型文件校验失败均返回明确错误，不会生成替代结果。

Agent 注册的独立工具为 `predict_high_rating_logistic_regression`、`predict_high_rating_decision_tree`；支持 `train_rating_classifiers`、`get_classification_status`、`read_classification_report`。未指定算法的自然语言预测请求调用两种模型对照。所需 UserID/MovieID 必须由用户提供。

## 实验规则

- 标签：`Rating>=4` 为 1，否则为 0。特征包括训练期用户/电影的历史评分量、高分占比、均分、用户对电影类型的历史偏好、电影类型、发行年份、用户性别/年龄编码/职业编码。**UserID、MovieID 本身不作为学习特征**。
- 时间边界取自本任务报告：`Timestamp<=T1` 为训练，`T1<Timestamp<=T2` 为验证，`Timestamp>T2` 为测试；训练数据不使用验证期和测试期评分信息。
- 每条训练评分的历史特征只来自**严格早于该事件时间戳**的训练期记录；同时刻事件分批构造特征再更新历史，防止同时间戳互相泄漏。验证/测试/在线预测统一使用冻结在 T1 的训练期历史。
- 逻辑回归在验证集上选择 `C ∈ {0.1,1,10}`，先标准化，再使用 L2 正则化；决策树在验证集上选择 `max_depth ∈ {6,10,14}`，`min_samples_leaf=30`，随机种子为 42。均按验证 F1 优先、ROC-AUC 次优确定参数，随后才计算一次最终测试集指标。
- 决策阈值固定 0.5。记录 Accuracy、Precision、Recall、F1、ROC-AUC、Brier、混淆矩阵以及训练/验证/测试样本量；AUC 不适用时标记 `null`。测试集不得参与模型选择。
- 模型存储在 `runs/<task_id>/classification/models/<experiment_id>/`；当前评价报告在 `runs/<task_id>/classification/report.json`，历次评价报告保存在 `classification/reports/`。绑定清洗数据 SHA-256、净表版本、T1/T2、算法配置与分类实现哈希。每个算法保存独立 joblib 模型和 SHA-256。
- 输出是模型对**未来发生评分事件**是否≥4的估计，并不表示用户真实已评分。对训练期观影历史不足的用户会明确提示不确定性；未知 ID 直接报错。

## 测试

```bash
python -m unittest tests.test_classification -v
python -m unittest discover -s tests
node --test tests/*.js
```

`tests.test_classification` 使用独立构造并完成哈希登记的小规模测试数据验证真实模型训练、版本隔离、同时间戳无泄漏、模型推断以及 HTTP API。正式实验指标以实际 Hadoop 清洗净表的训练结果为准。
