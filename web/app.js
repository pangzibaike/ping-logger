'use strict';
const $ = id => document.getElementById(id);
let targets = [], selected = null, chart, generation = 0;
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}
function message(text) { $('message').textContent = text; }
async function refreshTargets() {
  targets = await api('/api/targets');
  if (!targets.some(t => t.id === selected)) selected = targets[0]?.id ?? null;
  $('targets').replaceChildren(...targets.map(t => {
    const button = document.createElement('button');
    button.textContent = t.name === t.address ? t.address : `${t.name} · ${t.address}`;
    button.className = t.id === selected ? 'active' : '';
    button.onclick = () => { selected = t.id; refreshTargets().catch(e=>message(e.message)); };
    return button;
  }));
  await load();
}
async function load() {
  const request = ++generation, target = targets.find(t=>t.id===selected);
  $('delete').hidden = !target;
  if (!target) {
    if (chart) { chart.destroy(); chart = null; }
    $('heading').textContent = '添加一个目标，开始监测';
    $('stats').textContent = '每 60 秒采样 · 历史保留 7 天';
    $('freshness').textContent = ''; return;
  }
  const hours = Number($('hours').value);
  const data = await api(`/api/data?target=${target.id}&hours=${hours}`);
  if (request !== generation) return;
  const rows = data.samples, good = rows.filter(r=>r[2]==='ok'), loss = rows.filter(r=>r[2]==='loss').length;
  const errors = rows.length - good.length - loss, denominator = good.length + loss;
  const avg = good.length ? (good.reduce((s,r)=>s+r[1],0)/good.length).toFixed(1)+' ms' : '—';
  $('heading').textContent = `${target.name} → ${target.address}`;
  $('stats').textContent = `平均延迟 ${avg}　｜　丢包率 ${denominator ? (100*loss/denominator).toFixed(1)+'%' : '—'}　｜　采样 ${rows.length} 次　｜　本机错误 ${errors} 次`;
  const last = rows.at(-1);
  $('freshness').textContent = last ? `最近采样：${new Date(last[0]*1000).toLocaleString()}${data.now-last[0]>180?' · 数据已过期':''}` : '等待首次采样，通常在 60 秒内出现。';
  const points=[];
  rows.forEach((r,i)=> { if(i && r[0]-rows[i-1][0]>data.interval*1.5) points.push({x:(rows[i-1][0]+data.interval)*1000,y:null}); points.push({x:r[0]*1000,y:r[2]==='ok'?r[1]:null}); });
  const datasets = [{label:'延迟 (ms)',data:points,borderColor:'#127568',pointRadius:1,borderWidth:1.5,spanGaps:false},
    ...[['loss','丢包','#df5b48'],['error','本机错误','#8a9893']].map(([status,label,color])=>({label,data:rows.filter(r=>r[2]===status).map(r=>({x:r[0]*1000,y:0})),showLine:false,pointRadius:4,backgroundColor:color,borderColor:color}))];
  if(chart) chart.destroy();
  chart = new Chart($('chart'),{type:'line',data:{datasets},options:{animation:false,responsive:true,maintainAspectRatio:false,parsing:false,scales:{x:{type:'linear',min:(data.now-hours*3600)*1000,max:data.now*1000,ticks:{maxTicksLimit:8,callback:v=>new Date(v).toLocaleString([], {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}},y:{beginAtZero:true,title:{display:true,text:'ms'}}},plugins:{tooltip:{callbacks:{title:items=>items.length?new Date(items[0].parsed.x).toLocaleString():'',label:ctx=>ctx.datasetIndex===0?`${ctx.parsed.y} ms`:ctx.dataset.label}}}}});
}
$('add').onsubmit = async event => { event.preventDefault(); try { await api('/api/targets',{address:$('address').value,name:$('name').value}); $('add').reset(); message('已添加，后台将持续采样。'); await refreshTargets(); } catch(e){ message(e.message); } };
$('delete').onclick = async () => { if (!confirm('删除此目标及其全部历史记录？')) return; try { await api('/api/delete',{id:selected}); await refreshTargets(); } catch(e){message(e.message);} };
$('hours').onchange = () => load().catch(e=>message(e.message));
setInterval(()=>load().catch(e=>message('加载失败：'+e.message)),60000);
refreshTargets().catch(e=>message('加载失败：'+e.message));
