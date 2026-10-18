/* Task-bound chart explorer. It never calls a model or starts a Hadoop job. */
(() => {
  const get=id=>document.getElementById(id);
  const catalog={
    "quality-bars":{name:"质量对比柱状图",tab:"质量柱状",description:"对比清洗前后五个质量维度，统一使用 0—100 分刻度。"},
    "quality-radar":{name:"五维质量雷达图",tab:"质量雷达",description:"概览清洗前后的质量轮廓，精确数值可在下方数据表查看。"},
    genre:{name:"电影类型评分对比",tab:"电影类型",description:"按平均评分比较电影类型，悬停条形可同时查看评分次数和人数。"},
    distribution:{name:"1—5 分评分分布",tab:"评分分布",description:"查看所选任务净表中，已关联电影的各档评分次数与占比。"},
    scatter:{name:"评分次数与平均分",tab:"电影散点",description:"每个点代表一部有评分的电影，展示全量统计，支持次数门槛与横轴刻度切换。"}
  };
  let kind="quality-bars",taskId=null,sequence=0,current=null,chart=null;
  let minimum=1,logarithmic=true,donut=false;
  const cache=new Map();
  function element(tag,className,text){const value=document.createElement(tag);if(className)value.className=className;if(text!==undefined)value.textContent=String(text);return value;}
  async function request(path,body){const response=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});let value;try{value=await response.json();}catch{throw new Error(`请求失败（HTTP ${response.status}）`);}if(!response.ok)throw new Error(value.error||`请求失败（HTTP ${response.status}）`);return value;}
  function status(message,state=""){get("chart-status").textContent=message;get("chart-status").className=`chart-status ${state}`;get("chart-status").hidden=false;get("chart-result").hidden=true;get("chart-plot").setAttribute("aria-busy",String(state==="loading"));}
  function selected(){return get("chart-task-id").value.trim()||get("chart-task").value;}
  function selectKind(value){kind=value;get("charts-title").textContent=catalog[kind].name;get("charts-subtitle").textContent=catalog[kind].description;for(const button of get("chart-tabs").children)button.setAttribute("aria-pressed",String(button.dataset.kind===kind));}
  for(const [key,value] of Object.entries(catalog)){const button=element("button","",value.tab);button.type="button";button.dataset.kind=key;button.setAttribute("aria-pressed","false");button.addEventListener("click",()=>{selectKind(key);if(taskId&&selected())void load();});get("chart-tabs").append(button);}
  function invalidate(){sequence++;taskId=null;current=null;get("view-chart").disabled=false;status("任务已更改，点击“查看图表”加载所选任务。");}
  get("chart-task").addEventListener("change",()=>{get("chart-task-id").value="";invalidate();});
  get("chart-task-id").addEventListener("input",invalidate);
  get("chart-form").addEventListener("submit",event=>{event.preventDefault();void load();});
  get("close-charts").addEventListener("click",()=>get("charts-dialog").close());
  get("charts-dialog").addEventListener("close",()=>{sequence++;get("chart-plot").replaceChildren();get("chart-table").replaceChildren();current=null;chart=null;cache.clear();});

  globalThis.openTaskCharts=async (value,initialTask=null)=>{
    if(!catalog[value])return;
    const token=++sequence;taskId=null;current=null;cache.clear();selectKind(value);
    get("chart-task-id").value="";get("chart-task").replaceChildren(new Option("选择已完成任务…",""));
    get("chart-task-id").closest("details").open=false;get("view-chart").disabled=true;status("正在读取可用任务…","loading");
    if(!get("charts-dialog").open)get("charts-dialog").showModal();
    try{
      const tasks=await request("/api/tasks");if(token!==sequence)return;
      for(const task of tasks.filter(item=>item.state==="completed"))get("chart-task").append(new Option(`${task.task_id} · ${task.clean_table_available?"报告与净表":"仅报告"}`,task.task_id));
      if(initialTask){const existing=Array.from(get("chart-task").options).some(option=>option.value===initialTask);if(existing)get("chart-task").value=initialTask;else{get("chart-task-id").value=initialTask;get("chart-task-id").closest("details").open=true;}}
      status(tasks.some(task=>task.state==="completed")?"选择任务后，点击“查看图表”。质量图只需报告；其余图表需要完整净表。":"没有已完成任务。可按 ID 查找其他任务，或先完成一次清洗。");
    }catch(error){if(token===sequence)status(`无法读取任务：${error.message}\n可在“按 ID 查找”中输入任务号后重试。`,"error");}
    finally{if(token===sequence)get("view-chart").disabled=false;}
  };

  async function load(){
    const id=selected();if(!id){status("请先选择任务，或输入有效的任务 ID。");return;}
    const token=++sequence,requestedKind=kind;taskId=id;current=null;chart=null;
    const family=kind.startsWith("quality-")?"quality":kind==="genre"?"genre":"movies",key=`${id}:${family}`;
    get("view-chart").disabled=true;status(family==="quality"?"正在读取正式报告…":"正在核验完整净表并汇总评分；首次计算可能需要数十秒…","loading");
    try{
      let payload=cache.get(key);
      if(!payload){payload=family==="quality"?await request(`/api/tasks/${encodeURIComponent(id)}/report`):await request(`/api/tasks/${encodeURIComponent(id)}/charts`,{kind:requestedKind});if(token!==sequence)return;cache.set(key,payload);}
      if(token!==sequence)return;
      current=payload;get("chart-table-details").open=false;renderOptions();render();
    }catch(error){if(token===sequence)status(`暂时无法显示图表：${error.message}`,"error");}
    finally{if(token===sequence){get("view-chart").disabled=false;get("chart-plot").setAttribute("aria-busy","false");}}
  }
  function renderOptions(){
    const options=get("chart-options");options.replaceChildren();
    if(kind==="scatter"){
      const minimumLabel=element("label","","最少评分次数"),input=element("input");input.type="number";input.min="1";input.max="10000000";input.step="1";input.value=minimum;minimumLabel.append(input);
      input.addEventListener("input",()=>{if(!input.checkValidity()||!input.value)return;minimum=Number(input.value);render();});
      const logLabel=element("label"),checkbox=element("input");checkbox.type="checkbox";checkbox.checked=logarithmic;checkbox.addEventListener("change",()=>{logarithmic=checkbox.checked;render();});logLabel.append(checkbox,element("span","","横轴使用对数刻度"));options.append(minimumLabel,logLabel);
    }else if(kind==="distribution"){
      for(const [text,value] of [["柱状图",false],["环形图",true]]){const button=element("button","",text);button.type="button";button.setAttribute("aria-pressed",String(value===donut));button.addEventListener("click",()=>{donut=value;renderOptions();render();});options.append(button);}
    }
  }
  function dataTable(result){
    const target=get("chart-table"),table=element("table","detail-table"),thead=element("thead"),head=element("tr"),body=element("tbody");target.replaceChildren();
    result.headers.forEach(value=>head.append(element("th","",value)));thead.append(head);table.append(thead);
    for(const values of result.rows.slice(0,200)){const row=element("tr");values.forEach(value=>row.append(element("td","",value)));body.append(row);}table.append(body);target.append(table);
    if(result.rows.length>200)target.append(element("p","detail-note",`表格展示前 200 行；图表包含全部 ${result.rows.length.toLocaleString("zh-CN")} 个点，完整记录可下载数据查看。`));
  }
  function render(){
    if(!current)return;
    chart=kind.startsWith("quality-")?MovieLensCharts.quality(current,kind==="quality-radar"):kind==="genre"?MovieLensCharts.genre(current):kind==="distribution"?MovieLensCharts.distribution(current,donut):MovieLensCharts.scatter(current,{minimum,logarithmic});
    get("chart-plot").replaceChildren(chart.svg);get("chart-summary").textContent=chart.summary;
    const notes=get("chart-notes");notes.replaceChildren();for(const text of chart.notes)notes.append(element("p","",text));
    if(kind.startsWith("quality-")){
      if(chart.rows.some(row=>row.includes("无数据")))notes.append(element("p","chart-warning","部分维度缺少评分，图中保留缺失状态，不将缺失值当作 0 分。"));
      get("chart-source").textContent=`任务：${taskId} · 规则：${current.rule_version||"未记录"} · 依据：runs/${taskId}/report.json`;
    }else{
      const coverage=current.coverage;
      notes.append(element("p","",`净表共 ${coverage.total_ratings.toLocaleString("zh-CN")} 次评分，本次统计覆盖 ${coverage.matched_ratings.toLocaleString("zh-CN")} 次；无法关联电影或缺少所需类型而排除 ${(coverage.missing_movie_ratings+coverage.missing_genre_ratings).toLocaleString("zh-CN")} 次。`));
      // Whole-movie ranking warnings do not describe a global star distribution
      // or the currently filtered scatter population. Render their own checks.
      for(const warning of current.validation.warnings||[]){if(kind!=="genre"&&warning.startsWith("部分展示分组不足 30"))continue;notes.append(element("p","chart-warning",warning));}
      for(const warning of chart.warnings||[])notes.append(element("p","chart-warning",warning));
      if(kind==="scatter"&&!chart.rows.length)notes.append(element("p","chart-warning","没有电影达到当前门槛，请降低“最少评分次数”。"));
      get("chart-source").textContent=`任务：${taskId} · 规则：${current.source.rule_version} · 净表：${current.source.clean_data_version||"未记录"} · 已核验 SHA-256：${current.source.clean_data_sha256} · 依据：${current.evidence}`;
    }
    dataTable(chart);get("chart-status").hidden=true;get("chart-result").hidden=false;
  }
  function download(blob,filename){const url=URL.createObjectURL(blob),link=element("a");link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  get("download-chart").addEventListener("click",()=>{if(chart)download(new Blob([new XMLSerializer().serializeToString(chart.svg)],{type:"image/svg+xml;charset=utf-8"}),`${taskId}-${kind}.svg`);});
  get("download-chart-data").addEventListener("click",()=>{if(current)download(new Blob([JSON.stringify({task_id:taskId,chart:kind,filters:kind==="scatter"?{min_ratings:minimum,logarithmic}:{},data:current},null,2)],{type:"application/json;charset=utf-8"}),`${taskId}-${kind}.json`);});
})();
