const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() { this.children=[]; this.events={}; this.textContent=""; this.classList={remove(){},contains(){return false;}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener(name, fn) { this.events[name]=fn; }
  setAttribute(name, value) { this[name]=value; }
}
function app(confirm=true) {
  const elements=new Map(), requests=[], alerts=[];
  let tasks=[{task_id:"done",state:"completed"},{task_id:"busy",state:"running"}];
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const context=vm.createContext({
    document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[]},
    window:{confirm:()=>confirm,alert:value=>alerts.push(value)},
    fetch:async(path,options={})=>{
      requests.push({path,...options});
      if(options.method==="DELETE")tasks=tasks.filter(task=>path!==`/api/tasks/${task.task_id}`);
      return {ok:true,json:async()=>options.method==="DELETE"?{deleted:true}:tasks};
    },
  });
  const source=fs.readFileSync(require.resolve("../service/static/app.js"),"utf8");
  vm.runInContext(source.replace("void initialize();",""),context);
  return {context,get,requests,alerts};
}

test("history exposes deletion only for terminal tasks",async()=>{
  const {context,get}=app();
  await vm.runInContext("loadTasks()",context);
  const rows=get("task-list").children;
  assert.equal(rows[0].children[1].disabled,false);
  assert.equal(rows[1].children[1].disabled,true);
  assert.equal(rows[0].children[1]["aria-label"],"删除任务 done");
});

test("confirmed deletion clears selected context, inspector and cached report",async()=>{
  const {context,get,requests,alerts}=app();
  vm.runInContext('selectedTaskId="done";inspectorState={taskId:"done"};reportCache.set("done",{});',context);
  await vm.runInContext('deleteTask("done")',context);
  assert.equal(requests[0].method,"DELETE");
  assert.equal(vm.runInContext("selectedTaskId",context),null);
  assert.equal(vm.runInContext('reportCache.has("done")',context),false);
  assert.equal(get("task-list").children.length,1);
  assert.equal(get("inspector-body").children[0].textContent,"任务记录已删除");
  assert.deepEqual(alerts,[]);
});

test("cancelled deletion sends no request",async()=>{
  const {context,requests}=app(false);
  await vm.runInContext('deleteTask("done")',context);
  assert.equal(requests.length,0);
});
