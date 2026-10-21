const $ = (id) => document.getElementById(id);
const dimensionNames = {Accurate:"准确性",Complete:"完整性",Unique:"唯一性","Up-to-date":"时效性",Consistent:"一致性"};
const tableNames = {ratings:"评分",users:"用户",movies:"电影"};
const stateNames = {created:"准备中",queued:"排队中",running:"运行中",completed:"已完成",failed:"失败",unknown:"状态不明"};
const stageNames = {waiting_for_worker:"等待启动",starting_pipeline:"准备环境并等待 Hadoop 就绪",check_hdfs:"检查 HDFS",wait_hdfs_ready:"等待 HDFS 退出安全模式",check_yarn:"检查 YARN",create_raw_dir:"创建原始数据目录",score_before:"清洗前评分",clean:"清洗数据",export_clean:"导出净表",score_after:"清洗后评分",download_score_before:"下载清洗前结果",download_clean_process:"下载审计结果",download_cleaned:"下载净表",download_score_after:"下载清洗后结果",completed:"完成",historical_report:"历史报告",startup:"启动失败"};
const reasonNames = {policy_rating_below_minimum:"低于最低保留评分",policy_missing_title_year:"标题缺少年份",policy_filtered_movie_reference:"引用被过滤电影"};
let activeChatId = null;
let selectedTaskId = null;
let configured = false;
let sending = false;
let inspectorRequest = 0;
let statusTimer = null;
let navigationToken = 0;
let taskSelectionToken = 0;
let inspectorState = null;
const reportCache = new Map();

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error(`接口返回非 JSON（HTTP ${response.status}）`); }
  if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
  return data;
}
function jsonOptions(method, body) { return {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}; }
function node(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = String(value);
  return element;
}
function number(value) { return Number(value ?? 0).toLocaleString("zh-CN"); }
function score(value) { return value == null ? "无法评价" : Number(value).toFixed(2); }
function statusText(status) { return stateNames[status.state] || status.state || "未知"; }
function scrollBottom() { $("chat-scroll").scrollTo({top:$("chat-scroll").scrollHeight,behavior:"smooth"}); }
function setWelcome() { $("welcome").hidden = $("messages").children.length > 0; }
function closeMobileMenu() { $("sidebar").classList.remove("is-open"); $("mobile-backdrop").hidden = true; }
function updateContext() {
  $("context-label").textContent = selectedTaskId ? `当前任务 · ${selectedTaskId}` : "与你的数据分析助手交流";
}

function renderMessage(message, {pending=false,error=false} = {}) {
  const row = node("div", `message ${message.role}${pending ? " pending" : ""}${error ? " error" : ""}`);
  if (message.role === "user") {
    row.append(node("div", "message-content", message.content));
  } else {
    row.append(node("div", "assistant-avatar", "M"));
    const content = node("div", "assistant-content");
    if (pending || error) content.textContent = message.content;
    else renderModelAnswer(content, {answer:message.content,model:message.model,model_response_id:message.model_response_id,evidence:message.evidence});
    if(message.workflow){const plan=node("div","workflow-host");renderWorkflow(plan,message.workflow);content.prepend(plan);}
    for(const result of message.analyses||[])content.append(renderAnalysis(result));
    for(const result of message.preferences||[])content.append(renderPreferenceResult(result));
    // Classification results already appear in the Agent answer. Avoid repeating
    // the same predictions as another stack of cards below the response.
    if (message.task_id) content.append(taskInline(message.task_id));
    row.append(content);
  }
  $("messages").append(row);
  setWelcome();
  return row;
}
function taskInline(taskId) {
  const button = node("button", "task-inline");
  button.type = "button";
  button.append(node("span", "status-dot running"));
  const words = node("span");
  words.append(node("strong", "", "数据处理任务"), node("small", "", taskId));
  button.append(words, node("span", "", "查看 →"));
  button.addEventListener("click", () => selectTask(taskId, true));
  return button;
}

async function loadChats() {
  const chats = await api("/api/conversations");
  const list = $("chat-list"); list.replaceChildren();
  if (!chats.length) list.append(node("div", "nav-empty", "还没有对话"));
  for (const chat of chats) {
    const item = node("button", `nav-item${chat.id === activeChatId ? " active" : ""}`);
    item.type = "button"; item.title = chat.title;
    item.append(node("span", "item-icon", "◫"), node("span", "item-label", chat.title));
    item.addEventListener("click", () => showChat(chat.id));
    list.append(item);
  }
}
async function newChat() {
  const chat = await api("/api/conversations", jsonOptions("POST", {}));
  await showChat(chat.id);
  closeMobileMenu();
  $("message-input").focus();
}
async function showChat(id) {
  const token = ++navigationToken;
  const chat = await api(`/api/conversations/${encodeURIComponent(id)}`);
  if (token !== navigationToken) return;
  activeChatId = chat.id;
  selectedTaskId = chat.selected_task_id || null;
  sessionStorage.setItem("movielens-active-chat", chat.id);
  $("conversation-title").textContent = chat.title;
  updateContext();
  $("messages").replaceChildren();
  for (const item of chat.messages) renderMessage(item);
  setWelcome(); scrollBottom();
  await loadChats();
  await loadTasks();
  closeMobileMenu();
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  const list = $("task-list"); list.replaceChildren();
  if (!tasks.length) list.append(node("div", "nav-empty", "还没有处理任务"));
  for (const task of tasks.slice(0, 30)) {
    const row = node("div", "task-nav-row");
    const item = node("button", `nav-item${task.task_id === selectedTaskId ? " active" : ""}`);
    item.type = "button"; item.title = `${task.task_id} · ${statusText(task)}`;
    item.append(node("span", "item-icon", "▦"),node("span", "item-label", task.task_id),node("span", `status-dot ${task.state}`));
    item.addEventListener("click", () => selectTask(task.task_id, true));
    const remove = node("button", "task-delete", "×");
    remove.type = "button";
    remove.title = "删除本机任务记录";
    remove.setAttribute("aria-label", `删除任务 ${task.task_id}`);
    remove.disabled = !["completed", "failed"].includes(task.state);
    remove.addEventListener("click", () => void deleteTask(task.task_id));
    row.append(item, remove); list.append(row);
  }
}
async function deleteTask(taskId) {
  if (sending) { window.alert("请等当前对话处理完成后再删除任务。"); return; }
  if (!window.confirm(`删除任务 ${taskId}？\n\n将删除本机任务记录、报告、净表、统计和模型文件，无法撤销。\n聊天消息及 HDFS 中的数据保留。`)) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}`, {method:"DELETE"});
    reportCache.delete(taskId);
    taskSelectionToken++; inspectorRequest++;
    if (selectedTaskId === taskId) { selectedTaskId = null; updateContext(); }
    if (inspectorState?.taskId === taskId || $("inspector-title").textContent === taskId) {
      inspectorState = null; closeInspector();
      $("inspector-body").replaceChildren(node("div", "empty-inspector", "任务记录已删除"));
    }
    await loadTasks();
  } catch (error) { window.alert(`删除未完成：${error.message}`); }
}
async function selectTask(taskId, open) {
  const token = ++taskSelectionToken;
  const chatToken = navigationToken;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  } catch (error) {
    if (open && token === taskSelectionToken && chatToken === navigationToken) {
      openInspector();
      $("inspector-title").textContent = taskId;
      $("inspector-body").replaceChildren(node("div", "empty-inspector", error.message));
    }
    return;
  }
  if (token !== taskSelectionToken || chatToken !== navigationToken) return;
  selectedTaskId = taskId;
  updateContext();
  await loadTasks();
  if (open) openInspector();
  await renderInspector(taskId);
  closeMobileMenu();
}
function openInspector() { $("inspector").classList.add("is-open"); $("inspector").setAttribute("aria-hidden","false"); }
function closeInspector() { $("inspector").classList.remove("is-open"); $("inspector").setAttribute("aria-hidden","true"); }
function section(title) { const container=node("section","detail-section");container.append(node("h3","",title));return container; }
function stat(label,value,full=false) { const box=node("div",`detail-stat${full?" full":""}`);box.append(node("small","",label),node("strong","",value));return box; }
function detailRow(label,value) { const row=node("div","detail-row");row.append(node("b","",label),node("span","",value));return row; }
function table(headers,rows) {
  const wrap=node("div","detail-table-wrap"), t=node("table","detail-table"), head=node("thead"), hr=node("tr");
  headers.forEach(x=>hr.append(node("th","",x)));head.append(hr);t.append(head);
  const body=node("tbody"); for(const values of rows){const tr=node("tr");values.forEach(x=>tr.append(node("td","",x)));body.append(tr);}t.append(body);wrap.append(t);return wrap;
}
function addAction(parent,label,handler) { const button=node("button","",label);button.type="button";button.addEventListener("click",handler);parent.append(button); }

async function renderInspector(taskId) {
  const token=++inspectorRequest;
  $("inspector-title").textContent=taskId;
  $("inspector-body").replaceChildren(node("div","empty-inspector","正在读取任务状态…"));
  try {
    const status=await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    if(token!==inspectorRequest)return;
    inspectorState={taskId,state:status.state,stage:status.stage};
    const body=$("inspector-body");body.replaceChildren();
    const banner=node("div",`status-banner${status.state==="failed"?" failed":""}`,
      `${statusText(status)} · ${stageNames[status.stage]||status.stage||"阶段未记录"}${status.error?`\n${status.error}`:""}`);
    body.append(banner);
    const actions=node("div","detail-section detail-actions");
    addAction(actions,"在对话中询问此任务",()=>{closeInspector();$("message-input").focus();});
    if(status.state==="completed"){
      addAction(actions,"评分分类分析",()=>{closeInspector();openClassification(taskId);});
      addAction(actions,"查看数据图表",()=>{closeInspector();openTaskCharts("quality-bars",taskId);});
      const link=node("a","","下载完整 JSON 报告");link.href=`/api/tasks/${encodeURIComponent(taskId)}/report?download=1`;actions.append(link);
      addAction(actions,"让 Agent 解读报告",()=>{closeInspector();sendMessage("请根据当前任务的正式报告解释五维评分变化、数据处置及仍未解决的问题，并说明结论的局限。")});
      addAction(actions,"分析电影类型评分",()=>{closeInspector();sendMessage("请复用当前任务净表，比较不同电影类型的平均评分、评分次数和参与人数，说明样本量与统计局限。")});
    }
    body.append(actions);
    if (["completed", "failed"].includes(status.state)) {
      const remove = node("button", "danger-button", "删除本机任务记录");
      remove.type = "button";
      remove.addEventListener("click", () => void deleteTask(taskId));
      actions.append(remove);
    }
    if(status.agent){const s=section("本次执行配置");s.append(detailRow("使用工具",status.agent.tool||"未记录"));s.append(detailRow("规则版本",status.agent.rule_version||"未记录"));if(status.agent.options)s.append(detailRow("规则参数",JSON.stringify(status.agent.options)));body.append(s);}
    if(status.state!=="completed")return;
    let report=reportCache.get(taskId);
    if(!report){report=await api(`/api/tasks/${encodeURIComponent(taskId)}/report`);reportCache.set(taskId,report);}
    if(token!==inspectorRequest)return;
    const scores=section("五维质量评分");
    scores.append(table(["维度","清洗前","清洗后","变化"],Object.entries(dimensionNames).map(([key,name])=>[name,score(report.quality_before?.overall?.[key]),score(report.quality_after?.overall?.[key]),report.quality_delta?.[key]==null?"—":`${report.quality_delta[key]>=0?"+":""}${score(report.quality_delta[key])}`])));
    scores.append(node("p","detail-note","百分制；同一规则下对保留数据重新评分。详细分子分母见完整报告。"));body.append(scores);
    const counts=section("数据量与处置");
    counts.append(table(["表","输入","贡献","去重","隔离","净表"],Object.entries(report.cleaning?.tables||{}).map(([name,data])=>[tableNames[name]||name,number(data.input),number(data.contributing),number(data.duplicate),number(data.quarantine),number(data.output)])));
    counts.append(node("p","detail-note","贡献来源行与净表行可能不同；去重和隔离不代表修复。"));body.append(counts);
    const reasons=section("异常及策略原因"), reasonsList=node("div","reason-list");
    for(const [name,data] of Object.entries(report.cleaning?.tables||{}))for(const [key,value] of Object.entries(data.reasons||{})){
      const row=node("div","reason-item");row.append(node("span","",`${tableNames[name]||name} · ${reasonNames[key]||key}`),node("strong","",number(value)));reasonsList.append(row);
    }
    reasons.append(reasonsList);body.append(reasons);
    const versions=section("来源、版本和时间"), grid=node("div","detail-grid");
    for(const [label,value] of [["原始数据版本",report.raw_data_version],["规则版本",report.rule_version],["净表版本",report.clean_data_version],["净表 SHA-256",report.clean_data_sha256],["T1",report.T1],["T2",report.T2]])grid.append(stat(label,value||"未记录",true));
    versions.append(grid);const periods=report.period_counts||{};versions.append(node("p","detail-note",`训练 ${number(periods.train)} · 验证 ${number(periods.validation)} · 测试 ${number(periods.test)}`));body.append(versions);
    const limits=section("仍需核验"), unresolved=report.cleaning?.unresolved||{};
    limits.append(node("p","detail-note",`疑似乱码标题 ${number(unresolved.possible_mojibake)}；年份异常 ${number(unresolved.missing_title_year)}；非典型邮编 ${number(unresolved.non_us_zip_pattern)}。数据内部规则不能验证现实真实性。`));body.append(limits);
    const audits=section("审计样例"), list=node("div","detail-list");
    for(const [reason,items] of Object.entries(report.cleaning?.audit_samples||{})){
      if(!items?.length)continue;const sample=items[0],row=node("div","detail-row");row.append(node("b","",`${tableNames[sample.table]||sample.table} · ${reason} · ${sample.action}`),node("pre","",sample.raw||JSON.stringify(sample.details||{})));list.append(row);
    }audits.append(list);body.append(audits);
    const sampleSection=section("净表样例"), sampleList=node("div","detail-list");
    try{const samples=await api(`/api/tasks/${encodeURIComponent(taskId)}/samples`);if(token!==inspectorRequest)return;
      if(samples.state==="available")for(const item of samples.rows){const row=node("div","detail-row");row.append(node("b","",tableNames[item.table]||item.table),node("pre","",JSON.stringify(item.fields)));sampleList.append(row);}
      else sampleList.append(node("p","detail-note",samples.reason||"本地净表不可用。"));
    }catch(error){sampleList.append(node("p","detail-note",error.message));}
    sampleSection.append(sampleList);body.append(sampleSection);
  }catch(error){if(token===inspectorRequest)$("inspector-body").replaceChildren(node("div","empty-inspector",error.message));}
}

async function loadConfig() {
  const config=await api("/api/model-config");configured=Boolean(config.configured);
  $("model-pill").textContent=configured?config.model:"模型未配置";
  $("model-pill").classList.toggle("unconfigured",!configured);
  $("side-config-dot").classList.toggle("ready",configured);
  $("setting-model").value=config.model||"";
  $("setting-url").value=config.base_url||"https://api.openai.com/v1";
  $("setting-key").value="";
  $("key-state").textContent=config.key_present?"已设置 Key；留空可沿用当前 Key":"尚未设置 Key";
  return config;
}
async function openSettings() { await loadConfig();$("settings-error").textContent="";if(!$("settings-dialog").open)$("settings-dialog").showModal(); }
function closeSettings() { $("settings-dialog").close();$("setting-key").value=""; }
async function saveSettings(event) {
  event.preventDefault();$("settings-error").textContent="";$("save-settings").disabled=true;
  try{
    await api("/api/model-config",jsonOptions("PUT",{model:$("setting-model").value.trim(),base_url:$("setting-url").value.trim(),api_key:$("setting-key").value}));
    await loadConfig();closeSettings();
  }catch(error){$("settings-error").textContent=error.message;}
  finally{$("save-settings").disabled=false;}
}

async function sendMessage(value) {
  const text=(value??$("message-input").value).trim();
  if(!text||sending)return;
  if(!configured){await openSettings();return;}
  sending=true;$("send-button").disabled=true;$("message-input").value="";resizeComposer();
  try{
    if(!activeChatId){const chat=await api("/api/conversations",jsonOptions("POST",{}));activeChatId=chat.id;sessionStorage.setItem("movielens-active-chat",chat.id);}
    const sentChatId=activeChatId;
    const sentTaskId=selectedTaskId;
    const optimistic=renderMessage({role:"user",content:text});
    const pending=renderMessage({role:"assistant",content:"正在思考并检查可用工具…"},{pending:true});scrollBottom();
    const progressHost=node("div","workflow-host");pending.querySelector(".assistant-content").append(progressHost);
    let progressBusy=false;
    const progressTimer=setInterval(async()=>{
      if(progressBusy||activeChatId!==sentChatId)return;progressBusy=true;
      try{const activity=await api(`/api/conversations/${encodeURIComponent(sentChatId)}/activity`);if(pending.isConnected&&activity.workflow)renderWorkflow(progressHost,activity.workflow);}catch{}finally{progressBusy=false;}
    },1000);
    try{
      const result=await api(`/api/conversations/${encodeURIComponent(sentChatId)}/messages`,jsonOptions("POST",{message:text,task_id:sentTaskId}));
      if(activeChatId===sentChatId){
        clearInterval(progressTimer);pending.remove();renderMessage(result.message);selectedTaskId=result.selected_task_id||null;
        $("conversation-title").textContent=result.title;updateContext();
      }
      await loadChats();await loadTasks();
      if(result.message.task_id&&activeChatId===sentChatId)void renderInspector(result.message.task_id);
    }catch(error){
      if(activeChatId===sentChatId){pending.remove();optimistic.remove();renderMessage({role:"assistant",content:`请求未完成：${error.message}`},{error:true});if(!$("message-input").value)$("message-input").value=text;}
    }finally{clearInterval(progressTimer);}
  }catch(error){renderMessage({role:"assistant",content:`无法创建对话：${error.message}`},{error:true});$("message-input").value=text;}
  finally{sending=false;$("send-button").disabled=false;scrollBottom();$("message-input").focus();}
}
function resizeComposer(){const input=$("message-input");input.style.height="auto";input.style.height=`${Math.min(input.scrollHeight,160)}px`;}

$("new-chat").addEventListener("click",()=>void newChat());
for(const button of document.querySelectorAll("[data-chart]"))button.addEventListener("click",()=>{closeMobileMenu();openTaskCharts(button.dataset.chart,selectedTaskId);});
$("refresh-chats").addEventListener("click",()=>void loadChats());
$("refresh-tasks").addEventListener("click",()=>void loadTasks());
$("menu-toggle").addEventListener("click",()=>{$("sidebar").classList.add("is-open");$("mobile-backdrop").hidden=false;});
$("mobile-backdrop").addEventListener("click",closeMobileMenu);
$("open-inspector").addEventListener("click",()=>{openInspector();if(selectedTaskId)void renderInspector(selectedTaskId);});
$("close-inspector").addEventListener("click",closeInspector);
$("task-lookup-form").addEventListener("submit",event=>{event.preventDefault();const id=$("task-lookup").value.trim();if(id)void selectTask(id,true);});
$("composer").addEventListener("submit",event=>{event.preventDefault();void sendMessage();});
$("message-input").addEventListener("input",resizeComposer);
$("message-input").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();void sendMessage();}});
for(const suggestion of document.querySelectorAll("[data-prompt]"))suggestion.addEventListener("click",()=>void sendMessage(suggestion.dataset.prompt));
for(const button of [$("open-settings"),$("sidebar-settings")])button.addEventListener("click",()=>void openSettings());
$("close-settings").addEventListener("click",closeSettings);
$("cancel-settings").addEventListener("click",closeSettings);
$("settings-form").addEventListener("submit",saveSettings);
$("toggle-key").addEventListener("click",()=>{const input=$("setting-key");input.type=input.type==="password"?"text":"password";$("toggle-key").textContent=input.type==="password"?"显示":"隐藏";});
$("clear-key").addEventListener("click",async()=>{try{await api("/api/model-config",{method:"DELETE"});await loadConfig();$("settings-error").textContent="Key 已从服务进程中移除。";}catch(error){$("settings-error").textContent=error.message;}});

async function initialize(){
  try{await loadConfig();await loadTasks();const existing=sessionStorage.getItem("movielens-active-chat");if(existing){try{await showChat(existing);}catch{sessionStorage.removeItem("movielens-active-chat");await loadChats();}}else await loadChats();}
  catch(error){$("context-label").textContent=`服务连接失败：${error.message}`;}
  statusTimer=setInterval(async()=>{try{await loadTasks();if(selectedTaskId&&$("inspector").classList.contains("is-open")&&(!inspectorState||inspectorState.taskId!==selectedTaskId||["created","queued","running"].includes(inspectorState.state)))await renderInspector(selectedTaskId);}catch{}},6000);
}
void initialize();
