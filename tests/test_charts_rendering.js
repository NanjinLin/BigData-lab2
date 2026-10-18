const assert=require("node:assert/strict");
const {test}=require("node:test");
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attributes={};this.textContent="";}
  setAttribute(key,value){this.attributes[key]=String(value);}
  append(...items){this.children.push(...items);}
}
globalThis.document={createElementNS:(_,tag)=>new Element(tag)};
require("../service/static/chart-renderers.js");
const all=item=>[item,...item.children.flatMap(all)];
const text=item=>item.textContent+item.children.map(text).join(" ");
const report={quality_before:{overall:{Accurate:92,Complete:99,Unique:95,"Up-to-date":2,Consistent:84}},quality_after:{overall:{Accurate:97,Complete:null,Unique:100,"Up-to-date":3,Consistent:100}}};

test("quality charts preserve missing values instead of plotting them as zero",()=>{
  const bar=MovieLensCharts.quality(report),radar=MovieLensCharts.quality(report,true);
  assert.equal(all(bar.svg).filter(item=>item.attributes["data-mark"]==="quality").length,9);
  assert.equal(bar.rows[1][2],"无数据");
  assert.equal(all(radar.svg).filter(item=>item.tagName==="polygon").length,6);
  for(const result of [bar,radar])for(const node of all(result.svg))assert.ok(!Object.values(node.attributes).some(value=>value.includes("NaN")||value.includes("Infinity")));
});

test("distribution bar and donut use the unique-event distribution supplied by the server",()=>{
  const data={rating_distribution:{"1":10,"2":20,"3":30,"4":25,"5":15}};
  const bar=MovieLensCharts.distribution(data),donut=MovieLensCharts.distribution(data,true);
  assert.deepEqual(bar.rows[2],["3 分","30","30.00%"]);
  assert.equal(all(bar.svg).filter(item=>item.attributes["data-mark"]==="distribution").length,5);
  assert.ok(text(donut.svg).includes("100"));
  assert.equal(all(donut.svg).filter(item=>item.attributes["stroke-dasharray"]).length,5);
});

test("scatter renders all supplied movies, filters sample counts and spaces log decades evenly",()=>{
  const rows=Array.from({length:70},(_,i)=>({key:String(i),label:`Movie ${i}`,rating_count:i+1,mean_rating:3,user_count:1}));
  const full=MovieLensCharts.scatter({rows});
  assert.equal(all(full.svg).filter(item=>item.attributes["data-point"]!==undefined).length,70);
  assert.equal(MovieLensCharts.scatter({rows},{minimum:61}).rows.length,10);
  const decade=MovieLensCharts.scatter({rows:[1,10,100,1000].map((value,i)=>({...rows[i],rating_count:value}))});
  const xs=all(decade.svg).filter(item=>item.attributes["data-point"]!==undefined).map(item=>Number(item.attributes.cx));
  assert.ok(Math.abs((xs[1]-xs[0])-(xs[3]-xs[2]))<1e-8);
  const empty=MovieLensCharts.scatter({rows},{minimum:1000});
  assert.equal(empty.rows.length,0);assert.ok(text(empty.svg).includes("没有电影"));
});

test("movie names remain literal text in SVG tooltips and data tables",()=>{
  const payload='<script>alert(1)</script><img onerror="bad()">';
  const rendered=MovieLensCharts.scatter({rows:[{key:"1",label:payload,rating_count:3,mean_rating:4,user_count:2}]});
  assert.ok(text(rendered.svg).includes(payload));
  assert.equal(rendered.rows[0][0],payload);
  assert.ok(!all(rendered.svg).some(item=>item.tagName==="script"||item.tagName==="img"||item.innerHTML!==undefined));
});
