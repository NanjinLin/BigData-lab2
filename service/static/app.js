const $ = (id) => document.getElementById(id);
const dimensions = ["Accurate", "Complete", "Unique", "Up-to-date", "Consistent"];
const labels = {Accurate:"准确性",Complete:"完整性",Unique:"唯一性","Up-to-date":"时效性",Consistent:"一致性"};
const tables = {ratings:"评分",users:"用户",movies:"电影"};
const reasons = {policy_rating_below_minimum:"低于用户指定的最低保留评分",policy_missing_title_year:"按用户策略隔离缺少年份的电影记录",policy_filtered_movie_reference:"引用被策略过滤电影的评分"};
const stages = {waiting_for_worker:"等待启动",starting_pipeline:"准备运行环境",check_hdfs:"检查 HDFS",check_yarn:"检查 YARN",create_raw_dir:"创建原始数据目录",score_before:"清洗前评分",clean:"执行清洗",export_clean:"导出净表",score_after:"清洗后评分",download_score_before:"下载清洗前评分",download_clean_process:"下载清洗与审计结果",download_cleaned:"下载净表",download_score_after:"下载清洗后评分",completed:"完成",historical_report:"历史报告",startup:"启动失败"};
let currentTask = null;
let pollTimer = null;
let selectionToken = 0;
let questionToken = 0;
let modelConfigured = false;
const explanationCache = new Map();
const explanationQuestion = "请根据本次正式报告解释五维评分的主要变化、清洗解决的问题、修复与去重及隔离造成的数据量变化，并说明仍未解决的问题和评分局限。若有用户策略过滤，请与无效数据区分。只引用报告证据。";

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error(`接口返回非 JSON（HTTP ${response.status}）`); }
  if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
  return data;
}
function fmt(value) { return value == null ? "无法评价" : Number(value).toFixed(2); }
function int(value) { return Number(value).toLocaleString("zh-CN"); }
function clear(node) { node.replaceChildren(); }
function cell(row, content) { const td = document.createElement("td"); td.textContent = content; row.append(td); }
function ruleSummary(options) {
  const config = options || {min_retained_rating:1,missing_title_year_policy:"flag",table_weights:{ratings:1,users:1,movies:1}};
  const weights = config.table_weights;
  return `最低保留评分 ${config.min_retained_rating}；标题缺少年份：${config.missing_title_year_policy === "quarantine" ? "隔离" : "保留并标记"}；汇总权重 评分:用户:电影 = ${weights.ratings}:${weights.users}:${weights.movies}`;
}
function setStatus(status) {
  $("task-id").textContent = `任务 ID：${status.task_id}`;
  $("state").textContent = ({queued:"排队中",running:"执行中",created:"准备中",completed:"已完成",failed:"失败",unknown:"状态不明"})[status.state] || status.state;
  $("state").className = `state ${status.state}`;
  $("stage").textContent = `当前阶段：${stages[status.stage] || status.stage || "未知"}；更新时间：${status.updated_at || "未记录"}`;
  $("selected-rules").textContent = status.agent ? `本次执行参数：${ruleSummary(status.agent.options)}；规则版本 ${status.agent.rule_version}` : "";
  $("error").textContent = status.error || "";
  $("results").hidden = true;
  $("start-button").disabled = !modelConfigured || ["queued","running","created"].includes(status.state);
}
function renderReport(report, taskId, agentMetadata) {
  const parameters = report.rule_parameters || {min_retained_rating:1,missing_title_year_policy:"flag",table_weights:{ratings:1,users:1,movies:1}};
  clear($("scores"));
  for (const dimension of dimensions) {
    const row = document.createElement("tr");
    cell(row, `${labels[dimension]} · ${dimension}`);
    cell(row, fmt(report.quality_before.overall[dimension]));
    cell(row, fmt(report.quality_after.overall[dimension]));
    const delta = report.quality_delta[dimension];
    cell(row, delta == null ? "无法评价" : `${delta >= 0 ? "+" : ""}${fmt(delta)}`);
    $("scores").append(row);
  }
  $("scoring-note").textContent = `本次 ${ruleSummary(parameters)}。准确性是可检查值的合法性代理分；时效性只评价评分表，以数据发布时期的固定历史窗口为参照。无综合分。`;
  $("aggregation-note").textContent = `除时效性外，四个维度先逐表评分，再按本次 评分:用户:电影 = ${parameters.table_weights.ratings}:${parameters.table_weights.users}:${parameters.table_weights.movies} 汇总非空且权重大于零的表。`;
  clear($("counts")); clear($("reasons"));
  for (const [name, data] of Object.entries(report.cleaning.tables)) {
    const row = document.createElement("tr");
    [tables[name] || name, int(data.input), int(data.contributing), int(data.duplicate), int(data.quarantine), int(data.output)].forEach(value => cell(row, value));
    $("counts").append(row);
    for (const [reason, count] of Object.entries(data.reasons || {})) {
      const chip = document.createElement("span"); chip.className = "chip";
      chip.textContent = `${tables[name] || name} · ${reasons[reason] || reason}: ${int(count)}`;
      $("reasons").append(chip);
    }
  }
  clear($("version"));
  for (const [label, value] of [["原始数据版本",report.raw_data_version],["清洗规则版本",report.rule_version],["规则快照 SHA-256",report.rule_sha256 || "历史报告未记录"],["净表版本",report.clean_data_version],["净表 SHA-256",report.clean_data_sha256]]) {
    const item = document.createElement("div"); item.textContent = `${label}：${value}`; $("version").append(item);
  }
  const trace = document.createElement("div");
  trace.textContent = agentMetadata ? `模型决策：${agentMetadata.model} · 响应 ${agentMetadata.model_response_id}` : "历史任务：无大模型决策记录";
  $("version").append(trace);
  const p = report.period_counts;
  $("period").textContent = `T1 = ${report.T1}；T2 = ${report.T2}。训练 ${int(p.train)}，验证 ${int(p.validation)}，测试 ${int(p.test)} 条。`;
  const u = report.cleaning.unresolved;
  const strictYear = report.cleaning.tables.movies.reasons?.policy_missing_title_year || 0;
  const lowRating = report.cleaning.tables.ratings.reasons?.policy_rating_below_minimum || 0;
  $("limits").textContent = `仍待核验：疑似乱码标题 ${int(u.possible_mojibake || 0)}，保留的年份异常 ${int(u.missing_title_year || 0)}，非典型邮编 ${int(u.non_us_zip_pattern || 0)}。按策略隔离缺少年份的电影原始行 ${int(strictYear)}，过滤合法低分评分 ${int(lowRating)}。用户自填属性和电影内容的真实性无法仅靠数据内部规则确认。`;
  clear($("audit"));
  for (const [reason, samples] of Object.entries(report.cleaning.audit_samples || {})) {
    if (!samples.length) continue;
    const example = samples[0]; const card = document.createElement("div"); card.className = "sample";
    const title = document.createElement("strong"); title.textContent = `${tables[example.table] || example.table} · ${reason} · ${example.action}`;
    const body = document.createElement("pre"); body.textContent = example.raw || JSON.stringify(example.details || {});
    card.append(title, body); $("audit").append(card);
  }
  $("download").href = `/api/tasks/${encodeURIComponent(taskId)}/report?download=1`;
}
async function renderSamples(taskId, token) {
  const data = await api(`/api/tasks/${encodeURIComponent(taskId)}/samples`);
  if (token !== selectionToken) return;
  clear($("samples"));
  $("sample-message").textContent = data.state === "available" ? `来自净表版本 ${data.clean_data_version} 的前 ${data.rows.length} 条记录。` : data.reason;
  for (const row of data.rows) {
    const card = document.createElement("div"); card.className = "sample";
    const title = document.createElement("strong"); title.textContent = tables[row.table] || row.table;
    const body = document.createElement("pre"); body.textContent = JSON.stringify(row.fields, null, 2);
    card.append(title, body); $("samples").append(card);
  }
}
async function renderExplanation(taskId, token) {
  if (!modelConfigured) {
    $("explanation").textContent = "请按页面上方的模型状态提示配置或重启服务后生成报告解读。";
    return;
  }
  $("explanation").textContent = "模型正在读取并解读本次报告…";
  let request = explanationCache.get(taskId);
  if (!request) {
    request = api(`/api/tasks/${encodeURIComponent(taskId)}/ask`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({question:explanationQuestion})
    });
    explanationCache.set(taskId, request);
  }
  try {
    const result = await request;
    if (token === selectionToken) $("explanation").textContent = `${result.answer}（模型：${result.model}；响应：${result.model_response_id || "未记录"}；依据：本次 report.json）`;
  } catch (error) {
    explanationCache.delete(taskId);
    if (token === selectionToken) $("explanation").textContent = `报告解读失败：${error.message}`;
  }
}
async function showTask(taskId) {
  const token = ++selectionToken;
  ++questionToken;
  currentTask = taskId;
  clearTimeout(pollTimer);
  $("results").hidden = true;
  $("answer").textContent = "";
  $("explanation").textContent = "";
  $("sample-message").textContent = "";
  $("task-id").textContent = `任务 ID：${taskId}`;
  $("stage").textContent = "正在读取任务状态…";
  $("error").textContent = "";
  try {
    const status = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (token !== selectionToken) return;
    setStatus(status);
    if (status.state === "completed") {
      const report = await api(`/api/tasks/${encodeURIComponent(taskId)}/report`);
      if (token !== selectionToken) return;
      renderReport(report, taskId, status.agent);
      $("results").hidden = false;
      await renderSamples(taskId, token);
      void renderExplanation(taskId, token);
    } else if (["queued","running","created"].includes(status.state)) {
      pollTimer = setTimeout(() => { if (token === selectionToken) showTask(taskId); }, 2500);
    }
  } catch (error) { if (token === selectionToken) $("error").textContent = error.message; }
}
async function loadHistory() {
  try {
    const tasks = await api("/api/tasks");
    const selector = $("history"); clear(selector);
    const blank = document.createElement("option"); blank.value = ""; blank.textContent = "选择任务"; selector.append(blank);
    for (const task of tasks) {
      const option = document.createElement("option"); option.value = task.task_id;
      option.textContent = `${task.task_id} · ${task.state}`; selector.append(option);
    }
    if (currentTask) selector.value = currentTask;
  } catch (error) { $("form-message").textContent = error.message; }
}
async function loadAgentConfig() {
  try {
    const config = await api("/api/agent");
    const currentVersion = Array.isArray(config.capabilities) && config.capabilities.includes("run_hadoop_with_adjusted_rules");
    modelConfigured = Boolean(config.configured && currentVersion);
    $("model-status").textContent = !currentVersion
      ? "检测到旧版服务进程：请停止旧服务，并在已配置 API Key 的终端重新运行 python -m service.app。历史报告仍可浏览。"
      : config.configured
        ? `模型已配置：${config.model}。任务选择和报告追问会调用 ${config.provider}。`
        : "模型尚未配置：请在服务端设置 OPENAI_API_KEY 后重启服务。当前仍可查看历史报告。";
    $("start-button").disabled = !modelConfigured || ["queued","running","created"].includes($("state").classList[1]);
    $("ask-button").disabled = !modelConfigured;
  } catch (error) { $("model-status").textContent = `无法读取模型配置：${error.message}`; }
}
$("start-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("form-message").textContent = "正在提交…";
  try {
    const task = await api("/api/tasks", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:$("prompt").value})});
    $("form-message").textContent = "任务已创建。";
    currentTask = task.task_id;
    await loadHistory(); await showTask(task.task_id);
  } catch (error) { $("form-message").textContent = error.message; }
});
$("history").addEventListener("change", (event) => { if (event.target.value) showTask(event.target.value); });
$("open-task").addEventListener("click", () => {
  const taskId = $("task-lookup").value.trim();
  if (taskId) showTask(taskId);
});
$("refresh").addEventListener("click", () => { loadHistory(); if (currentTask) showTask(currentTask); });
$("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!currentTask) return;
  const taskId = currentTask;
  const token = selectionToken;
  const request = ++questionToken;
  $("answer").textContent = "正在查询…";
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/ask`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:$("question").value})});
    if (token === selectionToken && request === questionToken) $("answer").textContent = `${result.answer}（模型：${result.model}；响应：${result.model_response_id || "未记录"}；依据：本次 report.json）`;
  } catch (error) { if (token === selectionToken && request === questionToken) $("answer").textContent = error.message; }
});
loadHistory();
loadAgentConfig();
