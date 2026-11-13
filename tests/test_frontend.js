// No browser dependency: exercise chart lifecycle, stale responses and gaps in a DOM stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) { if(!elements.has(id)) elements.set(id,{value:id==='hours'?'24':'',textContent:'',hidden:false,replaceChildren(){},reset(){}}); return elements.get(id); }
let created=0, updated=0;
const body={samples:[[100,10,'ok'],[160,null,'loss'],[400,20,'ok']],now:400,interval:60};
const context=vm.createContext({document:{getElementById:element,createElement:()=>({})},
  fetch:async path=>({ok:true,json:async()=>path==='/api/targets'?[{id:1,name:'one',address:'1.1.1.1'},{id:2,name:'two',address:'8.8.8.8'}]:body}),
  Chart: class {constructor(_,config){ created++;this.data=config.data;this.options=config.options;}update(){updated++;}destroy(){}},
  setTimeout(){}, confirm:()=>true, console});
vm.runInContext(fs.readFileSync('web/app.js','utf8'),context);
async function run(){
  await vm.runInContext('refreshTargets()',context);
  await vm.runInContext('load()',context);
  assert.equal(created,1);
  assert.ok(updated>=1);
  assert.equal(vm.runInContext('chart.data.datasets[0].data.some(p=>p.y===null)',context),true);
  assert.equal(vm.runInContext('chart.data.datasets[1].data[0].y',context),100);
  await element('delete').onclick();
  assert.match(element('message').textContent,/旧版后端/);
  // A response belonging to the old selection must not overwrite the new one.
  let resolve;
  context.fetch=()=>new Promise(r=>resolve=r);
  const pending=vm.runInContext('load()',context);
  vm.runInContext('selected=2; ++generation;',context);
  const before=element('heading').textContent;
  resolve({ok:true,json:async()=>body});
  await pending;
  assert.equal(element('heading').textContent,before);
  context.fetch=async()=>({ok:true,json:async()=>({...body,summary:{min_ms:10,avg_ms:15,max_ms:20,p95_ms:19.5,loss_pct:33.3,consecutive_loss_cycles:0,probe_errors:0},state:{state:'partial_loss',reason:'timeout',latest:400},sample_details:[{ts:100,sent:3,received:2,status:'ok',interval:60}]})});
  await vm.runInContext('load()',context);
  assert.match(element('stats').textContent,/P95/);
  assert.match(element('freshness').textContent,/部分丢包/);
  assert.ok(Math.abs(vm.runInContext('chart.data.datasets[1].data[0].y',context)-100/3)<.001);
  assert.equal(created,1);
  console.log('Frontend lifecycle, target race, gaps, partial loss and stats: PASS');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
