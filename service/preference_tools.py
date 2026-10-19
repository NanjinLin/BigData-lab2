"""Independent Agent contracts for registered clustering/reduction results."""

CONFIG_PROPERTIES = {
    "min_history": {"type": "integer", "minimum": 1, "maximum": 10000},
    "n_clusters": {"type": "integer", "minimum": 2, "maximum": 20},
    "eps": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
    "min_samples": {"type": "integer", "minimum": 2, "maximum": 1000},
    "dimensions": {"type": "integer", "minimum": 1, "maximum": 56},
    "retention": {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 1},
}
CONFIG_SCHEMA = {"type": "object", "properties": CONFIG_PROPERTIES,
                 "required": list(CONFIG_PROPERTIES), "additionalProperties": False}


def _tool(name, description, properties):
    return {"type": "function", "name": name, "strict": True, "description": description,
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, **properties},
                           "required": ["task_id", *properties], "additionalProperties": False}}


TRAIN_PREFERENCES = _tool("train_user_preferences",
    "异步构建T1训练期用户画像并训练KMeans、DBSCAN、PCA、截断SVD及压缩前后聚类对照。返回提交状态，不代表已完成。",
    {"parameters": CONFIG_SCHEMA})
PREFERENCE_STATUS = _tool("get_preferences_status", "读取聚类与降维实时阶段及失败原因。", {})
PREFERENCE_REPORT = _tool("read_preferences_report", "读取已登记的画像分析报告、参数、版本及真实评价指标。", {})
QUERY_NAMES = {"cluster_users_kmeans": ("cluster", "kmeans"),
               "cluster_users_dbscan": ("cluster", "dbscan"),
               "reduce_users_pca": ("reduce", "pca"),
               "reduce_users_truncated_svd": ("reduce", "truncated_svd")}
QUERY_TOOLS = [_tool(name,
    f"独立读取{algorithm}的实际结果与参数、群体摘要及评价；降维含坐标、信息保留和两种聚类对照。"
    "user_ids为空时只返回前30人样例，最多查询200个ID；未知或历史不足逐人标注。"
    "parameters应与报告中的实际配置一致；不一致需先训练。",
    {"user_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 200},
     "parameters": CONFIG_SCHEMA,
     **({"comparison_algorithm": {"type": "string", "enum": ["both", "kmeans", "dbscan"]}}
        if category == "reduce" else {})}) for name, (category, algorithm) in QUERY_NAMES.items()]
PREFERENCE_TOOLS = [TRAIN_PREFERENCES, PREFERENCE_STATUS, PREFERENCE_REPORT, *QUERY_TOOLS]
PREFERENCE_OPERATIONS = tuple(t["name"] for t in PREFERENCE_TOOLS)
