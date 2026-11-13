// No browser dependency: exercise chart lifecycle, stale responses and gaps in a DOM stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
let selectedText='', fallbackResult=true, copied=[];
function node() {
  let text='';
  return {children:[],attributes:{},value:'',hidden:false,
    get textContent(){return text+this.children.map(child=>child.textContent).join('');},
    set textContent(value){text=value;this.children=[];},
    setAttribute(name,value){this.attributes[name]=value;},
    replaceChildren(...children){text='';this.children=children;},
    appendChild(child){child.parent=this;this.children.push(child);},
    remove(){this.parent.children=this.parent.children.filter(child=>child!==this);},
    select(){selectedText=this.value;},focus(){},reset(){}};
}
function element(id) { if(!elements.has(id)) {const item=node();item.value=id==='hours'?'24':'';elements.set(id,item);} return elements.get(id); }
let created=0, updated=0;
const body={samples:[[100,10,'ok'],[160,null,'loss'],[400,20,'ok']],now:400,interval:60};
const document={getElementById:element,createElement:node,body:node(),activeElement:node(),execCommand:command=>{assert.equal(command,'copy');return fallbackResult;}};
const context=vm.createContext({document,navigator:{clipboard:{writeText:async text=>copied.push(text)}},
  fetch:async path=>({ok:true,json:async()=>path==='/api/targets'?[{id:1,name:'one',address:'1.1.1.1'},{id:2,name:'two',address:'8.8.8.8'}]:body}),
  Chart: class {constructor(_,config){ created++;this.data=config.data;this.options=config.options;}update(){updated++;}destroy(){}},
  setTimeout(){}, confirm:()=>true, console});
vm.runInContext(fs.readFileSync('web/app.js','utf8'),context);
async function run(){
  await vm.runInContext('refreshTargets()',context);
  await vm.runInContext('load()',context);
  assert.equal(created,1);
  assert.equal(vm.runInContext('chart.options.scales.y.max',context),300);
  element('cap').value='12';
  await element('filter').onsubmit({preventDefault(){}});
  assert.equal(vm.runInContext('chart.options.scales.y.max',context),12);
  assert.equal(vm.runInContext('chart.data.datasets[3].data[0].actual_ms',context),20);
  assert.equal(vm.runInContext('chart.data.datasets[3].data[0].y',context),12);
  element('cap').value='-1';
  await element('filter').onsubmit({preventDefault(){}});
  assert.equal(vm.runInContext('maxLatency',context),12);
  await element('reset').onclick();
  assert.equal(vm.runInContext('maxLatency',context),300);
  assert.ok(updated>=1);
  assert.equal(element('heading').textContent,'one · 1.1.1.1');
  await element('heading').children[1].onclick();
  assert.deepEqual(copied,['1.1.1.1']);
  assert.equal(element('heading').children[2].textContent,'已复制');
  context.navigator.clipboard.writeText=async()=>{throw Error('Permission denied');};
  await vm.runInContext("copyAddress('2606:4700:4700::1111')",context);
  assert.equal(selectedText,'2606:4700:4700::1111');
  assert.equal(document.body.children.length,0);
  context.navigator.clipboard=undefined;
  await vm.runInContext("copyAddress('8.8.8.8')",context);
  assert.equal(selectedText,'8.8.8.8');
  fallbackResult=false;
  await element('heading').children[1].onclick();
  assert.match(element('heading').children[2].textContent,/复制失败/);
  assert.equal(document.body.children.length,0);
  vm.runInContext("renderHeading({name:'::1',address:'::1'})",context);
  assert.equal(element('heading').children[0].textContent,'');
  assert.equal(element('heading').children[1].textContent,'::1');
  vm.runInContext("renderHeading(targets[0])",context);
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
  context.fetch=async path=>({ok:true,json:async()=>path==='/api/targets'?[{id:1,name:'one',address:'1.1.1.1'},{id:2,name:'two',address:'8.8.8.8'}]:body});
  await element('next').onclick();
  assert.equal(vm.runInContext('selected',context),1);
  await element('previous').onclick();
  assert.equal(vm.runInContext('selected',context),2);
  console.log('Frontend lifecycle, target race, gaps, stats, title and clipboard success/fallback/failure: PASS');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
