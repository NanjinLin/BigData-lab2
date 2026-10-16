const assert = require("node:assert/strict");
const {test} = require("node:test");

class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.textContent=""; }
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  querySelector(tag){for(const item of this.children){if(item.tagName===tag)return item;const nested=item.querySelector(tag);if(nested)return nested;}return null;}
}
globalThis.document={createElement:tag=>new Element(tag)};
require("../service/static/workflow.js");
const textOf = item => item.textContent + item.children.map(textOf).join(" ");
const allOf = item => [item,...item.children.flatMap(allOf)];

test("model-authored plan text remains inert and open plan survives progress updates",()=>{
  const host=new Element("div");
  const workflow={goal:'<img src=x onerror="alert(1)">',needed_data:["净表"],expected_result:"类型均分",
    state:"running",steps:[{purpose:"计算均分",state:"running",detail:"正在计算"}]};
  renderWorkflow(host,workflow);
  assert.ok(textOf(host).includes("<img"));
  assert.ok(!allOf(host).some(item=>item.tagName==="img"||item.innerHTML!==undefined));
  host.querySelector("details").open=true;
  renderWorkflow(host,{...workflow,state:"insufficient",validation:{reason:"无匹配数据",checks:[{name:"存在结果",passed:false}],warnings:["不能排名"],note:"依据工具检查"}});
  assert.equal(host.querySelector("details").open,true);
  assert.ok(textOf(host).includes("证据不足"));
  assert.ok(textOf(host).includes("不能排名"));
});

test("statistics card preserves sample counts, distribution, warnings and evidence download",()=>{
  const result={query:{group_by:"movie",sort_by:"mean_rating",min_ratings:1,period:"all",movie_query:"",genre:""},
    groups:{returned:1,below_min_ratings:0},coverage:{total_ratings:2,period_ratings:2,matched_ratings:2,filtered_ratings:0,missing_movie_ratings:0,missing_genre_ratings:0},
    rows:[{label:"<script>movie</script>",mean_rating:4,rating_count:2,user_count:1,distribution:{"1":0,"2":0,"3":1,"4":0,"5":1}}],
    source:{task_id:"fixture",rule_version:"v1"},analysis_id:"analysis-123",method:{scope:"仅描述所选净表"},
    validation:{warnings:["样本较少"],checks:[{name:"分布合计一致",passed:true}]}};
  const card=renderAnalysis(result), text=textOf(card);
  for(const expected of ["电影评分统计","4.000","评分次数","人数","样本较少","分布合计一致","<script>movie</script>"])assert.ok(text.includes(expected));
  assert.equal(card.querySelector("a").href,"/api/tasks/fixture/analyses/analysis-123?download=1");
  assert.ok(!allOf(card).some(item=>item.tagName==="script"||item.innerHTML!==undefined));
});
