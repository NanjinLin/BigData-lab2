/* Classifier results are always fetched from the registered task, never mocked. */
const classifyDialog = document.getElementById("classification-dialog");
const classifyTask = document.getElementById("classification-task");
const classifyStatus = document.getElementById("classification-status");
const classifyTrain = document.getElementById("classification-train");
const classifyPredictForm = document.getElementById("classification-predict-form");
const classifierLabels = {logistic_regression:"逻辑回归", decision_tree:"决策树"};
const classifierStages = {waiting_for_worker:"等待启动",verify_clean_data:"核验清洗数据",build_temporal_features:"构建历史特征",train_models:"训练与评估模型",save_report:"保存模型和报告",completed:"已完成"};
let classifyPoll = null;
let classifyRequest = 0;
let classifyBusy = false;
let classifyReady = false;
let classificationReport = null;
const clf = (tag, cls, text) => {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
const classifierPercent = value => value == null ? "—" : `${(value*100).toFixed(1)}%`;
const classifierNumber = value => value == null ? "—" : Number(value).toFixed(3);
function classifierStatus(text, error=false) {
 classifyStatus.textContent=text;
 classifyStatus.hidden=!text;
 classifyStatus.classList.toggle("error",error);
}
function classifierCell(value){return clf("td","",value==null?"—":value);}
function classifierTable(headers, rows){
 const wrap=clf("div","classification-table-wrap"),table=clf("table","classification-table"),head=clf("thead"),h=clf("tr"),body=clf("tbody");
 for(const name of headers)h.append(clf("th","",name));head.append(h);table.append(head);
 for(const items of rows){const tr=clf("tr");for(const value of items)tr.append(classifierCell(value));body.append(tr);}
 table.append(body);wrap.append(table);return wrap;
}
function classifierDetails(title, children){const details=clf("details","classification-details");details.append(clf("summary","",title));for(const child of children)details.append(child);return details;}
function setClassificationReady(ready){
 classifyReady=ready;
 classifyPredictForm.hidden=!ready;
 document.getElementById("classification-predictions").replaceChildren();
 document.getElementById("classification-download").hidden=!ready;
}
function renderClassifierReport(report){
 classificationReport=report;
 const parent=document.getElementById("classification-report");parent.replaceChildren();
 const note=clf("p","classification-overview",`训练 ${report.split_counts.train.toLocaleString("zh-CN")} · 验证 ${report.split_counts.validation.toLocaleString("zh-CN")} · 测试 ${report.split_counts.test.toLocaleString("zh-CN")}`);
 const header=clf("div","classification-section-heading");header.append(clf("h3","","模型表现"),note);parent.append(header);
 parent.append(classifierTable(["算法","Accuracy","Precision","Recall","F1","AUC","Brier ↓"],
  Object.entries(report.models).map(([id,item])=>[classifierLabels[id]||id, ...["accuracy","precision","recall","f1","roc_auc"].map(x=>classifierPercent(item.test[x])),classifierNumber(item.test.brier)])));
 const details=[];
 details.push(clf("p","classification-meta",`数据版本：${report.source.clean_data_version}`));
 details.push(clf("p","classification-meta",`训练截止：${report.source.T1} · 验证截止：${report.source.T2}`));
 details.push(classifierTable(["算法","参数","验证 F1","版本"],Object.entries(report.models).map(([id,item])=>[
   classifierLabels[id]||id,Object.entries(item.parameters).map(([k,v])=>`${k}=${v}`).join("，"),classifierPercent(item.validation.f1),item.model_version])));
 details.push(clf("p","classification-meta",report.feature_policy));
 for(const limit of report.limitations||[])details.push(clf("p","classification-meta",limit));
 parent.append(classifierDetails("实验配置与说明", details));
 const link=document.getElementById("classification-download");
 link.href=`/api/tasks/${encodeURIComponent(report.task_id)}/classification/report?download=1`;
 setClassificationReady(true);
}
function renderClassificationPrediction(result){
 const card=clf("article","classification-card");
 const top=clf("div","classification-card-top");
 top.append(clf("h4","",classifierLabels[result.algorithm]||result.algorithm));
 top.append(clf("span",`classification-class ${result.predicted_class===1?"positive":"negative"}`, result.predicted_class===1?"预计 ≥4 分":"预计 <4 分"));
 card.append(top,clf("div","classification-prob",classifierPercent(result.positive_probability)));
 card.append(clf("p","classification-prob-label","评分达到 4 分的概率"));
 if(result.user_train_rating_count===0)card.append(clf("p","classification-low-history","该用户在训练期没有评分记录"));
 else card.append(clf("p","classification-history",`训练期已有 ${result.user_train_rating_count} 条评分`));
 return card;
}
async function loadClassificationTasks(preferred){
 const tasks=await api("/api/tasks");classifyTask.replaceChildren();
 classifyTask.append(new Option("选择清洗任务",""));
 for(const t of tasks){if(t.state!=="completed")continue;classifyTask.append(new Option(t.task_id,t.task_id));}
 if(preferred&&[...classifyTask.options].some(x=>x.value===preferred))classifyTask.value=preferred;
 else if(classifyTask.options.length>1)classifyTask.selectedIndex=classifyTask.options.length-1;
}
function stopClassificationPoll(){if(classifyPoll!==null)clearTimeout(classifyPoll);classifyPoll=null;}
function queueClassificationPoll(taskId, seq){
 stopClassificationPoll();
 classifyPoll=setTimeout(()=>{
  if(classifyDialog.open&&classifyTask.value===taskId&&seq===classifyRequest)void loadClassification();
 },2200);
}
async function loadClassification(){
 stopClassificationPoll();const seq=++classifyRequest,taskId=classifyTask.value;
 document.getElementById("classification-report").replaceChildren();
 setClassificationReady(false);
 classificationReport=null;
 classifyTrain.disabled=classifyBusy;
 if(!taskId){classifyTrain.disabled=true;classifierStatus("选择一个清洗任务即可开始训练。");return;}
 try {
  const state=await api(`/api/tasks/${encodeURIComponent(taskId)}/classification/status`);
  if(seq!==classifyRequest||classifyTask.value!==taskId)return;
  if(state.state==="completed"){
    const report=await api(`/api/tasks/${encodeURIComponent(taskId)}/classification/report`);
    if(seq!==classifyRequest||classifyTask.value!==taskId)return;
    renderClassifierReport(report);
    classifyTrain.textContent="重新训练";
    classifierStatus("模型已就绪，可以直接预测。");
  }else if(state.state==="running"||state.state==="queued"){
    classifyTrain.disabled=true;
    classifierStatus(`训练中 · ${classifierStages[state.stage]||state.stage||"处理中"}`);
    queueClassificationPoll(taskId,seq);
  }else{
    classifyTrain.textContent="开始训练";
    classifierStatus(state.state==="failed"||state.state==="interrupted" ? (state.error||"训练未完成，请重试。") : (state.message||"尚无分类模型，点击开始训练。"),state.state==="failed"||state.state==="interrupted");
  }
 }catch(error){if(seq===classifyRequest)classifierStatus(error.message,true);}
 finally{if(seq===classifyRequest&&classifyPoll===null)classifyTrain.disabled=classifyBusy||!taskId;}
}
async function openClassification(taskId){
 stopClassificationPoll();if(!classifyDialog.open)classifyDialog.showModal();
 classifierStatus("正在读取任务…");
 try{await loadClassificationTasks(taskId||selectedTaskId);await loadClassification();}
 catch(error){classifierStatus(error.message,true);}
}
document.getElementById("classification-open").addEventListener("click",()=>{closeMobileMenu();void openClassification();});
document.getElementById("classification-close").addEventListener("click",()=>classifyDialog.close());
classifyDialog.addEventListener("close",()=>{stopClassificationPoll();++classifyRequest;});
classifyTask.addEventListener("change",()=>{void loadClassification();});
document.getElementById("classification-task-form").addEventListener("submit",e=>{e.preventDefault();void loadClassification();});
classifyTrain.addEventListener("click",async()=>{
 const id=classifyTask.value;if(!id){classifierStatus("先选择清洗任务。",true);return;}
 classifyBusy=true;classifyTrain.disabled=true;
 classifierStatus("正在提交训练…");
 try{
  const result=await api(`/api/tasks/${encodeURIComponent(id)}/classification/train`,jsonOptions("POST",{}));
  if(classifyTask.value!==id)return;
  if(result.state!=="queued"&&result.state!=="running")throw new Error("训练没有成功启动。");
  await loadClassification();
 }catch(error){classifierStatus(error.message,true);}
 finally{classifyBusy=false;if(classifyPoll===null)classifyTrain.disabled=!classifyTask.value;}
});
classifyPredictForm.addEventListener("submit",async e=>{
 e.preventDefault();
 const id=classifyTask.value,uid=Number(document.getElementById("classification-user").value),mid=Number(document.getElementById("classification-movie").value);
 if(!classifyReady||!id||!Number.isSafeInteger(uid)||!Number.isSafeInteger(mid)||uid<1||mid<1){classifierStatus("请填写有效的 UserID 和 MovieID。",true);return;}
 const out=document.getElementById("classification-predictions");out.replaceChildren();
 const submit=classifyPredictForm.querySelector("button[type=submit]");submit.disabled=true;
 classifierStatus("预测中…");
 try{
  const results=await Promise.all(["logistic_regression","decision_tree"].map(algorithm=>api(`/api/tasks/${encodeURIComponent(id)}/classification/predict`,jsonOptions("POST",{user_id:uid,movie_id:mid,algorithm}))));
  if(classifyTask.value!==id)return;
  out.append(clf("p","classification-target",`用户 ${uid} · ${results[0].movie_title}`));
  const cards=clf("div","classification-result-grid");for(const item of results)cards.append(renderClassificationPrediction(item));out.append(cards);
  classifierStatus("");
 }catch(error){classifierStatus(error.message,true);}
 finally{submit.disabled=false;}
});
