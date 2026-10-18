/* Offline SVG charts. All task labels and values are written as text nodes. */
(() => {
  const NS="http://www.w3.org/2000/svg";
  const colors={before:"#a8b9cf",after:"#3269ad",grid:"#e9eef5",text:"#6d829d",ink:"#294b75"};
  const dimensions=[["Accurate","准确性"],["Complete","完整性"],["Unique","唯一性"],["Up-to-date","时效性"],["Consistent","一致性"]];
  const genres={Action:"动作",Adventure:"冒险",Animation:"动画","Children's":"儿童",Comedy:"喜剧",Crime:"犯罪",Documentary:"纪录片",Drama:"剧情",Fantasy:"奇幻","Film-Noir":"黑色电影",Horror:"恐怖",Musical:"歌舞",Mystery:"悬疑",Romance:"爱情","Sci-Fi":"科幻",Thriller:"惊悚",War:"战争",Western:"西部"};
  const count=value=>Number(value).toLocaleString("zh-CN");
  const valid=value=>typeof value==="number"&&Number.isFinite(value);
  const score=value=>valid(value)?value.toFixed(2):"无数据";
  const movieLabel=row=>!row.label||row.label==="\\N"?`标题缺失（电影 ${row.key}）`:row.label;
  function svgNode(tag,attrs={},value){const item=document.createElementNS(NS,tag);for(const [key,text] of Object.entries(attrs))item.setAttribute(key,String(text));if(value!==undefined)item.textContent=String(value);return item;}
  function root(title,height=440,className=""){
    const svg=svgNode("svg",{xmlns:NS,viewBox:`0 0 900 ${height}`,role:"img","aria-label":title,class:className,"font-family":"Segoe UI, Microsoft YaHei, sans-serif"});
    svg.append(svgNode("title",{},title),svgNode("rect",{width:900,height,fill:"#fff"}));return svg;
  }
  function label(svg,value,x,y,anchor="start",extra={}){svg.append(svgNode("text",{x,y,fill:colors.text,"font-size":13,"text-anchor":anchor,...extra},value));}
  function line(svg,x1,y1,x2,y2,extra={}){svg.append(svgNode("line",{x1,y1,x2,y2,stroke:colors.grid,...extra}));}
  function title(mark,text){mark.append(svgNode("title",{},text));return mark;}
  function legend(svg,items,y=27){let x=70;for(const [name,color] of items){svg.append(svgNode("rect",{x,y:y-10,width:12,height:12,rx:3,fill:color}));label(svg,name,x+20,y);x+=120;}}
  function yAxis(svg,max,bottom=345,top=65,left=75,right=855){
    for(let i=0;i<=5;i++){const value=max*i/5,y=bottom-(bottom-top)*i/5;line(svg,left,y,right,y);label(svg,Number.isInteger(value)?count(value):value.toFixed(1),left-12,y+4,"end");}
    return value=>bottom-(bottom-top)*value/max;
  }
  function empty(svg,message){label(svg,message,450,210,"middle");}
  function quality(report,radar=false){
    const before=report.quality_before?.overall||{},after=report.quality_after?.overall||{};
    const svg=root(radar?"清洗前后五维质量雷达图":"清洗前后五维质量对比",radar?510:430,radar?"radar-chart":"");
    legend(svg,[["清洗前",colors.before],["清洗后",colors.after]]);
    const rows=dimensions.map(([key,name])=>[name,score(before[key]),score(after[key]),valid(before[key])&&valid(after[key])?(after[key]-before[key]).toFixed(2):"—"]);
    if(!radar){
      const y=yAxis(svg,100);
      dimensions.forEach(([key,name],i)=>{
        const x=155+i*151;label(svg,name,x,377,"middle");
        for(const [value,color,offset,series] of [[before[key],colors.before,-27,"清洗前"],[after[key],colors.after,7,"清洗后"]]){
          if(!valid(value)){label(svg,"缺失",x+offset+10,330,"middle",{"font-size":10});continue;}
          const bar=svgNode("rect",{x:x+offset,y:y(value),width:24,height:Math.max(0,345-y(value)),rx:3,fill:color,"data-mark":"quality"});
          svg.append(title(bar,`${name} · ${series}：${score(value)} / 100`));label(svg,score(value),x+offset+12,y(value)-9,"middle",{"font-size":11,fill:color});
        }
      });label(svg,"质量评分（0—100）",75,49);
    }else{
      const cx=450,cy=260,r=170;
      const point=(i,radius)=>[cx+Math.sin(i*2*Math.PI/5)*radius,cy-Math.cos(i*2*Math.PI/5)*radius];
      for(let tick=20;tick<=100;tick+=20){svg.append(svgNode("polygon",{points:dimensions.map((_,i)=>point(i,r*tick/100).join(",")).join(" "),fill:"none",stroke:colors.grid}));label(svg,String(tick),cx+8,cy-r*tick/100+4,"start",{"font-size":10});}
      dimensions.forEach(([,name],i)=>{const [x,y]=point(i,r),[lx,ly]=point(i,r+35);line(svg,cx,cy,x,y);label(svg,name,lx,ly+4,"middle");});
      for(const [values,color,series] of [[before,colors.before,"清洗前"],[after,colors.after,"清洗后"]]){
        if(dimensions.every(([key])=>valid(values[key])))svg.append(svgNode("polygon",{points:dimensions.map(([key],i)=>point(i,r*values[key]/100).join(",")).join(" "),fill:color,"fill-opacity":.12,stroke:color,"stroke-width":2}));
        dimensions.forEach(([key,name],i)=>{if(valid(values[key])){const [cx,cy]=point(i,r*values[key]/100);svg.append(title(svgNode("circle",{cx,cy,r:4,fill:color}),`${name} · ${series}：${score(values[key])} / 100`));}});
      }
    }
    return {svg,headers:["维度","清洗前","清洗后","变化（百分点）"],rows,summary:"五维质量 · 统一百分制",notes:["评分使用本任务登记的规则与权重；清洗后完整性可能因冲突字段置空而下降。",...(radar?["雷达图用于概览；精确数值见数据表。缺失维度不补零，不连接包含缺失值的多边形。"]:[])]};
  }
  function genre(data){
    const rows=data.rows,svg=root("电影类型平均评分对比",Math.max(300,100+rows.length*36));
    const left=145,right=810,top=57;
    for(let tick=0;tick<=5;tick++){const x=left+(right-left)*tick/5;line(svg,x,top,x,top+rows.length*36);label(svg,String(tick),x,top-15,"middle");}
    label(svg,"平均评分（1—5 分；横轴从 0 起）",left,22);
    rows.forEach((row,i)=>{const y=top+i*36+6;
      label(svg,genres[row.key]||row.label,left-15,y+18,"end");
      const bar=svgNode("rect",{x:left,y,width:(right-left)*row.mean_rating/5,height:23,rx:4,fill:colors.after,"data-mark":"genre"});
      svg.append(title(bar,`${row.label}：平均 ${row.mean_rating.toFixed(3)} 分；${count(row.rating_count)} 次评分；${count(row.user_count)} 位用户`));label(svg,row.mean_rating.toFixed(3),left+(right-left)*row.mean_rating/5+9,y+17,"start",{fill:colors.ink,"font-size":12});
    });if(!rows.length)empty(svg,"该任务没有可用的电影类型评分。");
    return {svg,headers:["电影类型","平均分","评分次数","参与人数"],rows:rows.map(row=>[`${genres[row.key]||row.label} (${row.key})`,row.mean_rating.toFixed(3),count(row.rating_count),count(row.user_count)]),summary:`${rows.length} 个类型 · 按平均分排序`,notes:["多类型电影的评分分别计入其每个类型；各类型评分次数不能相加作为总评分量。","悬停条形可查看评分次数与人数；均分差异不能证明因果或统计显著性。"]};
  }
  function distribution(data,donut=false){
    const values=[1,2,3,4,5].map(star=>Number(data.rating_distribution[String(star)]||0)),total=values.reduce((a,b)=>a+b,0);
    const svg=root(donut?"评分占比环形图":"1—5 分评分次数分布",430),palette=["#b9c8db","#8faed1","#6794c4","#417ab6","#245b9e"];
    if(!total)empty(svg,"当前任务没有可统计的评分。");
    else if(donut){
      const cx=310,cy=219,r=124,circumference=2*Math.PI*r;let offset=0;
      svg.append(svgNode("circle",{cx,cy,r,fill:"none",stroke:colors.grid,"stroke-width":47}));
      values.forEach((value,i)=>{if(value){const length=circumference*value/total;svg.append(title(svgNode("circle",{cx,cy,r,fill:"none",stroke:palette[i],"stroke-width":47,"stroke-dasharray":`${length} ${circumference-length}`,"stroke-dashoffset":-offset,transform:`rotate(-90 ${cx} ${cy})`}),`${i+1} 分：${count(value)} 次，占 ${(100*value/total).toFixed(2)}%`));offset+=length;}
        svg.append(svgNode("rect",{x:540,y:106+i*45,width:12,height:12,rx:3,fill:palette[i]}));label(svg,`${i+1} 分`,565,117+i*45);label(svg,`${count(value)} · ${(100*value/total).toFixed(1)}%`,820,117+i*45,"end");});
      label(svg,"评分总次数",cx,cy-8,"middle");label(svg,count(total),cx,cy+25,"middle",{"font-size":25,fill:colors.ink,"font-weight":650});
    }else{
      const max=Math.max(...values,1),magnitude=10**Math.floor(Math.log10(max)),ceiling=Math.ceil(max/magnitude)*magnitude,y=yAxis(svg,ceiling);
      values.forEach((value,i)=>{const x=125+i*151;svg.append(title(svgNode("rect",{x,y:y(value),width:65,height:345-y(value),rx:5,fill:palette[i],"data-mark":"distribution"}),`${i+1} 分：${count(value)} 次，占 ${(100*value/total).toFixed(2)}%`));label(svg,count(value),x+32.5,y(value)-10,"middle",{"font-size":12,fill:colors.ink});label(svg,`${i+1} 分`,x+32.5,377,"middle");});label(svg,"评分次数",75,45);
    }
    return {svg,headers:["评分","次数","占比"],rows:values.map((value,i)=>[`${i+1} 分`,count(value),total?`${(100*value/total).toFixed(2)}%`:"—"]),summary:`${count(total)} 次评分 · 1—5 分`,warnings:total&&total<30?["当前统计不足 30 次评分，分布可能受少量样本影响；该门槛不是显著性检验。"]:[],notes:["按评分事件计数，每次评分只计入一个分值；重复参与评分的用户仍可能贡献多次事件。","统计范围为已关联电影的净表评分，未按电影类型重复累计。"]};
  }
  function scatter(data,{minimum=1,logarithmic=true}={}){
    const rows=data.rows.filter(row=>row.rating_count>=minimum),svg=root("电影评分次数与平均分散点图",450);
    const left=78,right=853,top=60,bottom=355,max=Math.max(1,...rows.map(row=>row.rating_count));
    const ceiling=logarithmic?10**Math.max(1,Math.ceil(Math.log10(max))):Math.ceil(max/10)*10||10;
    const x=value=>left+(right-left)*(logarithmic?Math.log10(value)/Math.log10(ceiling):value/ceiling),y=value=>bottom-(bottom-top)*(value-1)/4;
    for(let value=1;value<=5;value++){line(svg,left,y(value),right,y(value));label(svg,String(value),left-13,y(value)+4,"end");}
    const ticks=logarithmic?Array.from({length:Math.log10(ceiling)+1},(_,i)=>10**i):Array.from({length:6},(_,i)=>ceiling*i/5);
    for(const value of ticks){line(svg,x(value),top,x(value),bottom);label(svg,count(value),x(value),bottom+25,"middle",{"font-size":12});}
    label(svg,"平均评分（1—5 分）",left,30);label(svg,`评分次数${logarithmic?"（对数刻度）":"（线性刻度）"}`,450,417,"middle");
    for(const row of rows){const point=svgNode("circle",{cx:x(row.rating_count),cy:y(row.mean_rating),r:3.1,fill:row.rating_count<30?"#9bacc1":colors.after,opacity:.55,"data-point":row.key});svg.append(title(point,`${movieLabel(row)} · ID ${row.key}\n平均 ${row.mean_rating.toFixed(3)} 分 · ${count(row.rating_count)} 次评分 · ${count(row.user_count)} 位用户`));}
    if(!rows.length)empty(svg,"没有电影达到当前最低评分次数，请降低筛选门槛。");
    const small=rows.filter(row=>row.rating_count<30||row.user_count<30);
    return {svg,headers:["电影","ID","评分次数","平均分","参与人数"],rows:rows.map(row=>[movieLabel(row),row.key,count(row.rating_count),row.mean_rating.toFixed(3),count(row.user_count)]),summary:`${count(rows.length)} 部电影 · 共 ${count(data.rows.length)} 部有评分电影`,warnings:small.length?[`当前展示的 ${count(small.length)} 部电影不足 30 次评分或 30 位用户，小样本均分需谨慎解读；该门槛不是显著性检验。`]:[],notes:["每个点代表一部电影，悬停可查看电影名、评分次数和人数。图中包含所有满足当前门槛的电影，未截取前 50 名。","灰色点不足 30 次评分，仅作小样本提示；评分次数与平均分的关系不代表因果，未预设线性拟合。"]};
  }
  globalThis.MovieLensCharts={quality,genre,distribution,scatter};
})();
