/* Evidence cards use text nodes only, including all model-authored plan text. */
(() => {
  const labels = {running:"执行中",pending:"待执行",completed:"已取得结果",failed:"未完成",submitted:"等待任务",
    awaiting_task:"等待任务",passed:"可回答",limited:"有局限",insufficient:"证据不足"};
  function el(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = String(text);
    return value;
  }
  function detailTable(headers, rows) {
    const wrap=el("div","evidence-table-wrap"), table=el("table","evidence-table"), head=el("thead"), body=el("tbody"), row=el("tr");
    for(const text of headers)row.append(el("th","",text));head.append(row);table.append(head);
    for(const items of rows){const item=el("tr");for(const text of items)item.append(el("td","",text));body.append(item);}
    table.append(body);wrap.append(table);return wrap;
  }
  globalThis.renderWorkflow = (target, workflow) => {
    const wasOpen=Boolean(target.querySelector("details")?.open);
    target.replaceChildren();
    if(!workflow)return;
    const card=el("details","workflow-card"), summary=el("summary");
    card.open=wasOpen;
    summary.append(el("strong","","分析计划"),el("span",`workflow-state ${workflow.state}`,labels[workflow.state]||"处理中"));
    card.append(summary);
    const body=el("div","workflow-body");
    body.append(el("p","workflow-goal",workflow.goal),el("p","evidence-note",`所需数据：${(workflow.needed_data||[]).join("、")}`));
    const steps=el("ol","workflow-steps");
    for(const step of workflow.steps||[]){const row=el("li",`workflow-step ${step.state}`);row.append(el("span","",step.purpose),el("small","",`${labels[step.state]||step.state} · ${step.detail||""}`));steps.append(row);}
    body.append(steps,el("p","evidence-note",`预期结果：${workflow.expected_result}`));
    if(workflow.validation){const validation=workflow.validation, checks=el("div","goal-checks");
      checks.append(el("strong","","目标与证据检查"),el("p","",validation.reason));
      for(const check of validation.checks||[])checks.append(el("p","evidence-note",`${check.passed?"✓":"○"} ${check.name}`));
      for(const warning of validation.warnings||[])checks.append(el("p","evidence-warning",warning));
      checks.append(el("p","evidence-note",validation.note));body.append(checks);
    }
    card.append(body);target.append(card);
  };
  globalThis.renderAnalysis = (result) => {
    const card=el("details","analysis-card"), summary=el("summary");
    const byMovie=result.query.group_by==="movie";
    summary.append(el("strong","",byMovie?"电影评分统计":"电影类型统计"),el("span","",`${result.groups.returned} 个分组`));card.append(summary);
    const body=el("div","analysis-body"), query=result.query, coverage=result.coverage;
    const periodNames={all:"全部时间",train:"训练时间段",validation:"验证时间段",test:"测试时间段"};
    body.append(el("p","evidence-note",`复用已有净表 · ${result.reused?"复用相同统计结果":"完成统计"} · ${query.sort_by==="mean_rating"?"按平均分排序":"按评分次数排序"} · 至少 ${query.min_ratings} 次评分 · ${periodNames[query.period]||query.period}`));
    body.append(el("p","evidence-note",`全部 ${coverage.total_ratings.toLocaleString("zh-CN")} 次评分，所选时段 ${coverage.period_ratings.toLocaleString("zh-CN")} 次，匹配 ${coverage.matched_ratings.toLocaleString("zh-CN")} 次；条件排除 ${coverage.filtered_ratings.toLocaleString("zh-CN")} 次，关联或类型缺失 ${(coverage.missing_movie_ratings+coverage.missing_genre_ratings).toLocaleString("zh-CN")} 次。低于次数门槛的分组 ${result.groups.below_min_ratings} 个。`));
    const disposition=result.source.rating_disposition;
    if(disposition?.input!=null)body.append(el("p","evidence-note",`此前清洗：输入 ${disposition.input.toLocaleString("zh-CN")} 次评分，净表保留 ${disposition.output.toLocaleString("zh-CN")} 次，隔离 ${disposition.quarantine?.toLocaleString("zh-CN")??"未记录"} 次，去重 ${disposition.duplicate?.toLocaleString("zh-CN")??"未记录"} 次。`));
    if(query.movie_query||query.genre)body.append(el("p","evidence-note",`筛选：${query.movie_query?`标题包含“${query.movie_query}”` : "全部标题"} · ${query.genre||"全部类型"}`));
    body.append(detailTable([byMovie?"电影":"类型","平均分","评分次数","人数"],result.rows.map(row=>[row.label,Number(row.mean_rating).toFixed(3),row.rating_count.toLocaleString("zh-CN"),row.user_count.toLocaleString("zh-CN")])));
    for(const warning of result.validation.warnings||[])body.append(el("p","evidence-warning",warning));
    const distribution=el("details","distribution-card");distribution.append(el("summary","","查看 1—5 分分布与统计口径"));
    distribution.append(detailTable(["分组","1 分","2 分","3 分","4 分","5 分"],result.rows.map(row=>[row.label,...[1,2,3,4,5].map(i=>row.distribution[String(i)].toLocaleString("zh-CN"))])));
    for(const text of Object.values(result.method||{}))distribution.append(el("p","evidence-note",text));
    for(const check of result.validation.checks||[])distribution.append(el("p","evidence-note",`${check.passed?"✓":"○"} ${check.name}`));
    body.append(distribution,el("p","evidence-note",`来源任务：${result.source.task_id} · 规则：${result.source.rule_version}`));
    const link=el("a","analysis-download","下载完整统计与依据");
    link.href=`/api/tasks/${encodeURIComponent(result.source.task_id)}/analyses/${encodeURIComponent(result.analysis_id)}?download=1`;
    body.append(link);card.append(body);return card;
  };
})();
