# MovieLens 分析 Agent · 迭代二

在已有 Hadoop 数据治理与逻辑回归/决策树分类模块上，增加 **K-Means、DBSCAN、PCA、截断 SVD**，以及训练期用户画像、群体解释、压缩前后聚类对照、二维散点图、结果下载和独立 Agent 工具。

已核对课程《26Fall-008-分类1010+1014》第24页：逻辑回归、决策树均在课件列出的分类算法中。新增四种无监督算法采用小组确认的选型。完整说明见 [迭代二说明](docs/迭代二_系统说明与运行.md)，实际指标见 [实跑结果](docs/迭代二_实跑结果.md)，汇报步骤见 [演示与验收](docs/迭代二_演示与验收.md)。原有分类细节见 [CLASSIFICATION_GUIDE.md](CLASSIFICATION_GUIDE.md)。

## Windows 启动

在本目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-iteration2.txt
.\.venv\Scripts\python.exe -m service.app --port 8767
```

打开 <http://127.0.0.1:8767>。从左侧进入“评分分类”或“用户聚类 · 降维对照”，选择任务 `20260923T144724Z-3509cc23`。已携带训练产物时可直接预测、查看和下载，无须重复训练。已有`.venv`时跳过创建步骤。

Linux/WSL 使用 `python3 -m venv .venv`、`source .venv/bin/activate`，再执行 `python -m pip install -r requirements-iteration2.txt` 和 `python -m service.app --port 8767`。

分类、聚类及降维面板不需要大模型密钥。自然语言 Agent 需要在页面模型设置中配置可用的 **Responses API** 服务；当前环境未配置密钥，真实云端模型的端到端对话尚未验收。自动化测试中的模拟模型不代表真实云端验收。

## 训练、校验和测试

```powershell
.\.venv\Scripts\python.exe scripts/run_classification.py --task-id 20260923T144724Z-3509cc23
.\.venv\Scripts\python.exe scripts/run_preferences.py --task-id 20260923T144724Z-3509cc23
.\.venv\Scripts\python.exe scripts/verify_iteration2.py --task-id 20260923T144724Z-3509cc23
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/*.js
```

Git 仓库不保存大型净表与模型；仅克隆源码时会明确显示“数据不可用/未完成”，不会产生占位结果。需将上述任务的完整`runs/<任务ID>/`目录一并转交，或使用打包脚本生成包含净表、模型和结果的提交包。禁止将别的任务报告与本任务净表混用。

```powershell
.\.venv\Scripts\python.exe scripts/package_iteration2.py --task-id 20260923T144724Z-3509cc23
```

原来的`README_提交说明.md`记录迭代一提交，迭代二以本页和`docs/迭代二_*.md`为准。
