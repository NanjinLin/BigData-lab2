const assert=require("node:assert/strict");
const {test}=require("node:test");
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.textContent="";}
  append(...items){this.children.push(...items);}
  addEventListener(){}
}
const elements=new Map();
globalThis.document={createElement:tag=>new Element(tag),getElementById:id=>{
  if(!elements.has(id))elements.set(id,new Element("div"));return elements.get(id);
}};
require("../service/static/preferences.js");
const all=n=>[n,...n.children.flatMap(all)];
const text=n=>n.textContent+" "+n.children.map(text).join(" ");
const result={algorithm:"dbscan",returned_users:3,sample_only:false,experiment_id:"test-version",
 source:{clean_data_version:"test-clean"},subset_policy:"训练期冻结画像",
 users:[{user_id:1,cluster:-1,noise:true},{user_id:2,status:"insufficient_history",reason:"历史不足"},
        {user_id:999,status:"unknown_user",reason:"用户不存在"}],
 model:{metrics:{silhouette:null},parameters:{eps:3}},limitations:["不适用的指标不得作为0显示"]};
test("noise, insufficient history and unknown users remain distinct",()=>{
 const card=renderPreferenceResult(result),body=text(card);
 for(const phrase of ["噪声","历史不足","用户不存在","test-version","test-clean"])assert.ok(body.includes(phrase));
 assert.ok(body.includes('"silhouette": null'));
 assert.equal(all(card).filter(x=>x.tagName==="tr").length,4);
});
test("untrusted result text is displayed literally and never interpreted as HTML",()=>{
 const payload='<img src=x onerror="bad()"><script>bad()</script>';
 const card=renderPreferenceResult({...result,limitations:[payload],users:[{user_id:1,reason:payload}]});
 assert.ok(text(card).includes(payload));
 assert.ok(!all(card).some(n=>["img","script"].includes(n.tagName)||n.innerHTML!==undefined));
});
