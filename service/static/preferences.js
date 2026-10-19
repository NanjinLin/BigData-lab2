/* Registered experiment results only. Text is always inserted through textContent. */
(() => {
  const $p=id=>document.getElementById(id), dialog=$p("preferences-dialog");
  const labels={kmeans:"K-Means",dbscan:"DBSCAN",pca:"PCA",truncated_svd:"截断 SVD"};
  const stages={waiting_for_worker:"等待启动",verify_clean_data:"核验净表与报告",build_train_profiles:"构建训练期画像",cluster_kmeans:"K-Means 聚类",cluster_dbscan:"DBSCAN 聚类",reduce_pca:"PCA 与聚类对照",reduce_truncated_svd:"截断 SVD 与聚类对照",save_report:"登记模型与报告",completed:"已完成"};
  const colors=["#6366f1","#14b8a6","#f59e0b","#ec4899","#8b5cf6","#06b6d4","#84cc16","#f97316"];
  const color=n=>n===-1?"#94a3b8":colors[n%colors.length];
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
  const fmt=n=>n==null?"不适用":Number(n).toFixed(4);
  const pct=n=>n==null?"不适用":`${(n*100).toFixed(1)}%`;
  let seq=0,timer=null,report=null;
  function note(text){return el("p","pref-note",text);}
  function table(headers,rows){const wrap=el("div","pref-table-wrap"),t=el("table","pref-table"),head=el("thead"),h=el("tr"),body=el("tbody");for(const a of headers)h.append(el("th","",a));head.append(h);for(const row of rows){const tr=el("tr");for(const a of row)tr.append(el("td","",a??"—"));body.append(tr);}t.append(head,body);wrap.append(t);return wrap;}
  function section(title){const s=el("section","pref-section");s.append(el("h3","",title));return s;}
  function status(text,error=false){const n=$p("preferences-status");n.textContent=text;n.classList.toggle("error",error);}
  function stop(){if(timer!==null)clearTimeout(timer);timer=null;}
  const endpoint=id=>`/api/tasks/${encodeURIComponent(id)}/preferences`;
  function params(){const out={};for(const k of ["min_history","n_clusters","eps","min_samples","dimensions"]){out[k]=Number($p("pref-"+k.replaceAll("_","-")).value);}out.retention=$p("pref-retention").value?Number($p("pref-retention").value):null;return out;}
  function setParams(config){for(const [k,v] of Object.entries(config))$p("pref-"+k.replaceAll("_","-")).value=v??"";}
  function download(parent,id,name,title){const a=el("a","",title);a.href=name==="report"?`${endpoint(id)}/report?download=1`:`${endpoint(id)}/data/${name}?download=1`;parent.append(a);}
  function groupRows(summaries){return summaries.map(s=>[s.noise?"噪声":`群体 ${s.cluster}`,s.users,fmt(s.mean_rating),pct(s.high_ratio),s.top_genres.map(g=>`${g.genre} ${pct(g.mean_share)}`).join(" · "),s.representative_movies.map(m=>`${m.title} (${m.train_high_ratings})`).join("；")]);}
  function qualityRow(space,algo,item,ari){const m=item.metrics;return [space,labels[algo],m.clusters,m.noise_count,fmt(m.silhouette),fmt(m.calinski_harabasz),fmt(m.davies_bouldin),ari==null?"—":fmt(ari),fmt(item.fit_seconds)];}
  function svgNode(tag,attrs,text){const n=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=String(text);return n;}
  function scatter(data,groups,title){
    const svg=svgNode("svg",{viewBox:"0 0 500 350",role:"img","aria-label":title}),users=data.users;
    const xs=users.map(u=>u.coordinates[0]),ys=users.map(u=>u.coordinates[1]||0);
    let xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);
    if(xmax===xmin){xmin-=1;xmax+=1;}if(ymax===ymin){ymin-=1;ymax+=1;}
    const x=v=>50+(v-xmin)/(xmax-xmin)*420,y=v=>300-(v-ymin)/(ymax-ymin)*260;
    svg.append(svgNode("rect",{x:50,y:40,width:420,height:260,fill:"white",stroke:"#cbd5e1"}));
    for(let i=0;i<=4;i++){const xv=xmin+(xmax-xmin)*i/4,yv=ymin+(ymax-ymin)*i/4;svg.append(svgNode("text",{x:x(xv),y:319,"text-anchor":"middle",fill:"#64748b","font-size":10},xv.toFixed(1)),svgNode("text",{x:43,y:y(yv)+3,"text-anchor":"end",fill:"#64748b","font-size":10},yv.toFixed(1)));}
    for(const u of users){const label=groups[u.user_id],dot=svgNode("circle",{cx:x(u.coordinates[0]),cy:y(u.coordinates[1]||0),r:2.4,fill:color(label),opacity:.55});dot.append(svgNode("title",{},`UserID ${u.user_id} · ${label===-1?"噪声":"群体 "+label}`));svg.append(dot);}
    svg.append(svgNode("text",{x:260,y:344,"text-anchor":"middle",fill:"#64748b","font-size":11},"分量 1"),svgNode("text",{x:16,y:175,transform:"rotate(-90 16 175)","text-anchor":"middle",fill:"#64748b","font-size":11},data.actual_dimensions===1?"单维表示（第二轴为 0）":"分量 2"));return svg;
  }
  function saveSvg(svg,title){const blob=new Blob([new XMLSerializer().serializeToString(svg)],{type:"image/svg+xml"}),url=URL.createObjectURL(blob),a=el("a");a.href=url;a.download=title+".svg";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function plots(target,data,original,clusterer){target.replaceChildren();const before=Object.fromEntries(original.users.map(u=>[u.user_id,u.cluster])),after=Object.fromEntries(data.users.map(u=>[u.user_id,u.clusters[clusterer]]));for(const [title,groups] of [["原始画像上的群体（投影显示）",before],["降维表示上的群体",after]]){const card=el("div","pref-plot"),svg=scatter(data,groups,title),button=el("button","secondary-button","下载 SVG");button.type="button";button.addEventListener("click",()=>saveSvg(svg,`${data.algorithm}-${clusterer}-${title}`));card.append(el("h4","",`${labels[clusterer]} · ${title}`),svg,button);target.append(card);}const legend=el("div","pref-legend");for(const n of [...new Set([...Object.values(before),...Object.values(after)])].sort((a,b)=>a-b)){const span=el("span"),dot=el("i","pref-dot");dot.style.background=color(n);span.append(dot,document.createTextNode(n===-1?"噪声":`群体 ${n}`));legend.append(span);}target.after(legend);}
  async function render(saved,id,version){
    const root=$p("preferences-results");root.replaceChildren();report=saved;setParams(saved.parameters);
    const metrics=el("div","pref-metrics");for(const [value,label] of [[saved.eligible_users,"可分析用户"],[saved.excluded_users,"历史不足用户"],[saved.profile_dimensions,"原始画像维数"]]){const card=el("div","pref-metric");card.append(el("strong","",value),el("span","",label));metrics.append(card);}root.append(metrics,note(`数据版本 ${saved.source.clean_data_version} · 训练截止 ${saved.source.T1} · 验证截止 ${saved.source.T2}`),note(`画像版本 ${saved.experiment_id}`),note(saved.profile_policy));
    const links=el("div","pref-links");for(const [name,title] of [["report","评价报告"],["profiles.json","用户画像"],["kmeans.json","K-Means 群体"],["dbscan.json","DBSCAN 群体"],["pca.json","PCA 坐标"],["truncated_svd.json","SVD 坐标"]])download(links,id,name,title);root.append(links);
    const clustering=section("原始画像 · 两种聚类"),rows=[];
    for(const [a,item] of Object.entries(saved.clusterings))rows.push(qualityRow("原始",a,item));
    clustering.append(table(["空间","算法","群数","噪声人数","轮廓系数 ↑","CH ↑","DB ↓","ARI","拟合秒数"],rows));
    for(const [a,item] of Object.entries(saved.clusterings)){const d=el("details","pref-config");d.append(el("summary","",`${labels[a]} · 群体摘要`),note(`参数 ${JSON.stringify(item.parameters)} · 模型 ${item.model_version}`),table(["群体","用户数","均分","高分占比","偏好类型","代表电影（训练期高分次数）"],groupRows(item.summaries)),note(item.metrics.unavailable_reason||item.metrics.metric_note));clustering.append(d);}root.append(clustering);
    for(const [a,item] of Object.entries(saved.reductions)){
      const s=section(`${labels[a]} · 压缩与聚类对照`);s.append(note(`${item.original_dimensions} → ${item.actual_dimensions} 维 · 方差保留 ${pct(item.retained_variance)} · 重建 MSE ${fmt(item.reconstruction_mse)}`),note(`降维拟合及变换 ${fmt(item.fit_transform_seconds)} 秒 · 输入矩阵 ${item.input_matrix_bytes.toLocaleString()} 字节 · 坐标 ${item.coordinate_bytes.toLocaleString()} 字节 · 模型 ${item.model_bytes.toLocaleString()} 字节`));
      const comparison=[];for(const [c,v] of Object.entries(item.comparisons)){comparison.push(qualityRow("原始",c,saved.clusterings[c]));comparison.push(qualityRow(labels[a],c,v,v.adjusted_rand_vs_original));}
      s.append(table(["空间","算法","群数","噪声人数","轮廓系数 ↑","CH ↑","DB ↓","前后 ARI","聚类秒数"],comparison));
      const select=el("select");select.setAttribute("aria-label",`${labels[a]}可视化聚类算法`);select.append(new Option("K-Means 群体对照","kmeans"),new Option("DBSCAN 群体对照","dbscan"));const host=el("div","pref-plots");s.append(note(item.information_note),select,host,note("两图使用相同降维坐标。左图按原始画像聚类着色，右图按压缩后重新聚类着色；不同模型的相同群号不表示相同群体。"));root.append(s);
      const [data,kmeans,dbscan]=await Promise.all([`${a}.json`,"kmeans.json","dbscan.json"].map(name=>api(`${endpoint(id)}/data/${name}`)));
      if(seq!==version||$p("preferences-task").value!==id)return;
      const draw=()=>{const legend=host.nextElementSibling;if(legend?.classList.contains("pref-legend"))legend.remove();plots(host,data,select.value==="kmeans"?kmeans:dbscan,select.value);};select.addEventListener("change",draw);draw();
    }
    const limits=section("依据与适用限制");limits.append(note(saved.selection_policy));for(const t of saved.limitations)limits.append(note(t));root.append(limits);$p("preferences-query").hidden=false;
  }
  async function load(){
    stop();const version=++seq,id=$p("preferences-task").value;report=null;$p("preferences-results").replaceChildren();$p("preferences-user-results").replaceChildren();$p("preferences-query").hidden=true;$p("preferences-run").disabled=!id;
    if(!id){status("请选择一个已完成且具有完整净表的任务。");return;}
    try{const state=await api(`${endpoint(id)}/status`);if(version!==seq)return;
      if(["running","queued"].includes(state.state)){status(`执行中 · ${stages[state.stage]||state.stage}`);$p("preferences-run").disabled=true;timer=setTimeout(()=>{if(dialog.open)void load();},2200);}
      else if(state.state==="completed"){status("正在读取已登记结果…");const saved=await api(`${endpoint(id)}/report`);if(version!==seq)return;await render(saved,id,version);if(version===seq)status("已完成 · 可查询用户、比较算法并下载真实结果。");}
      else status(`${stages[state.stage]||state.stage||"尚未运行"} · ${state.error||state.message||"尚无聚类与降维结果，点击运行。"}`,["failed","interrupted"].includes(state.state));
    }catch(e){if(version===seq)status(e.message,true);}
  }
  globalThis.openPreferences=async taskId=>{stop();if(!dialog.open)dialog.showModal();status("读取可用任务…");try{const tasks=await api("/api/tasks"),select=$p("preferences-task");select.replaceChildren(new Option("选择清洗任务",""));for(const t of tasks)if(t.state==="completed")select.append(new Option(`${t.task_id}${t.clean_table_available?"":"（缺少净表）"}`,t.task_id));const preferred=taskId||(typeof selectedTaskId!=="undefined"?selectedTaskId:null);if(preferred&&[...select.options].some(o=>o.value===preferred))select.value=preferred;else{const usable=tasks.find(t=>t.state==="completed"&&t.clean_table_available);if(usable)select.value=usable.task_id;}await load();}catch(e){status(e.message,true);}};
  globalThis.renderPreferenceResult=result=>{const card=el("details","pref-evidence");card.append(el("summary","",`${labels[result.algorithm]} · ${result.returned_users} 位用户${result.sample_only?"（样例）":""}`),note(`版本 ${result.experiment_id} · ${result.source.clean_data_version}`),note(result.subset_policy),table(["UserID","状态 / 群体","坐标"],result.users.map(u=>[u.user_id,u.reason||(u.noise?"噪声":u.cluster!=null?`群体 ${u.cluster}`:"已生成"),u.coordinates?u.coordinates.map(fmt).join(", "):"—"])));const details=el("details");details.append(el("summary","","参数、评价及局限"),el("pre","",JSON.stringify(result.model,null,2)));card.append(details);for(const t of result.limitations||[])card.append(note(t));return card;};
  $p("preferences-open").addEventListener("click",()=>{if(typeof closeMobileMenu==="function")closeMobileMenu();void openPreferences();});$p("preferences-close").addEventListener("click",()=>dialog.close());dialog.addEventListener("close",()=>{stop();++seq;});$p("preferences-task").addEventListener("change",()=>void load());
  $p("preferences-form").addEventListener("submit",async e=>{e.preventDefault();const id=$p("preferences-task").value;if(!id)return;$p("preferences-run").disabled=true;status("提交分析…");try{await api(`${endpoint(id)}/train`,jsonOptions("POST",params()));if($p("preferences-task").value===id)await load();}catch(error){status(error.message,true);$p("preferences-run").disabled=false;}});
  $p("preferences-query").addEventListener("submit",async e=>{e.preventDefault();const id=$p("preferences-task").value,version=seq,algorithm=$p("preferences-algorithm").value,parts=$p("preferences-users").value.trim().split(/[,，\s]+/),user_ids=parts.map(Number);if(!report||user_ids.some(x=>!Number.isSafeInteger(x)||x<1)){status("请填写有效的正整数UserID。",true);return;}try{const result=await api(`${endpoint(id)}/query`,jsonOptions("POST",{algorithm,category:["kmeans","dbscan"].includes(algorithm)?"cluster":"reduce",user_ids}));if(seq!==version)return;const card=renderPreferenceResult(result);card.open=true;$p("preferences-user-results").replaceChildren(card);status("用户查询已完成；未知或历史不足逐项标注。");}catch(error){if(seq===version)status(error.message,true);}});
})();
