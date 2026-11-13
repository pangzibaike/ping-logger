'use strict';
const $ = id => document.getElementById(id);
let targets = [], selected = null, chart, generation = 0, targetGeneration = 0, refreshMs = 60000, softDeleteSupported = false;
let maxLatency = 300;
async function switchTarget(step) {
  if (!targets.length) return;
  selected = targets[(targets.findIndex(t => t.id === selected)+step+targets.length)%targets.length].id;
  ++generation;
  await refreshTargets();
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}
function message(text) { $('message').textContent = text; }
async function copyAddress(address) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(address);
      return;
    }
  } catch (_) { /* Fall back when clipboard permission is unavailable. */ }
  const previousFocus = document.activeElement;
  const field = document.createElement('textarea');
  field.value = address;
  field.className = 'clipboard-helper';
  field.setAttribute('readonly', '');
  document.body.appendChild(field);
  try {
    field.select();
    if (!document.execCommand('copy')) throw Error('复制失败');
  } finally {
    field.remove();
    previousFocus?.focus({preventScroll:true});
  }
}
function renderHeading(target) {
  const name = document.createElement('span');
  name.textContent = target.name === target.address ? '' : `${target.name} · `;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy-ip';
  button.textContent = target.address;
  button.title = '点击复制 IP';
  button.setAttribute('aria-label', `复制 IP ${target.address}`);
  const feedback = document.createElement('span');
  feedback.className = 'copy-status';
  feedback.setAttribute('role', 'status');
  button.onclick = async () => {
    try { await copyAddress(target.address); feedback.textContent = '已复制'; }
    catch (_) { feedback.textContent = '复制失败，请选中 IP 手动复制'; }
  };
  $('heading').replaceChildren(name, button, feedback);
}
async function refreshTargets() {
  const token = ++targetGeneration;
  const received = await api('/api/targets');
  if (token !== targetGeneration) return;
  targets = received;
  $('previous').disabled = $('next').disabled = targets.length < 2;
  if (!targets.some(t => t.id === selected)) selected = targets[0]?.id ?? null;
  $('targets').replaceChildren(...targets.map(t => {
    const button = document.createElement('button');
    button.textContent = t.name === t.address ? t.address : `${t.name} · ${t.address}`;
    button.className = t.id === selected ? 'active' : '';
    button.onclick = () => { selected = t.id; ++generation; refreshTargets().catch(e=>message(e.message)); };
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
  if (request !== generation || selected !== target.id) return;
  refreshMs = data.interval * 1000;
  const rows = data.samples, good = rows.filter(r=>r[2]==='ok'), loss = rows.filter(r=>r[2]==='loss').length;
  const errors = rows.length - good.length - loss, denominator = good.length + loss;
  const avg = good.length ? (good.reduce((s,r)=>s+r[1],0)/good.length).toFixed(1)+' ms' : '—';
  renderHeading(target);
  const s = data.summary;
  softDeleteSupported = Boolean(s && data.sample_details);
  const ms = value => value == null ? '—' : value.toFixed(2)+' ms';
  $('stats').textContent = s ? `所选 ${hours} 小时：最小 ${ms(s.min_ms)}　｜　平均 ${ms(s.avg_ms)}　｜　最大 ${ms(s.max_ms)}　｜　P95 ${ms(s.p95_ms)}　｜　丢包 ${s.loss_pct == null ? '—' : s.loss_pct.toFixed(1)+'%'}　｜　连续全丢 ${s.consecutive_loss_cycles} 个周期　｜　本机错误 ${s.probe_errors} 次` : `平均延迟 ${avg}　｜　丢包率 ${denominator ? (100*loss/denominator).toFixed(1)+'%' : '—'}　｜　采样 ${rows.length} 次　｜　本机错误 ${errors} 次`;
  const last = rows.at(-1);
  const labels = {success:'响应正常',partial_loss:'部分丢包',loss:'目标无响应',probe_error:'本机探测错误',interrupted:'采样中断',pending:'等待首次采样'};
  const clock = value => value == null ? '—' : new Date(value*1000).toLocaleString();
  $('freshness').textContent = data.state ? `当前状态：${labels[data.state.state] || data.state.state}（${data.state.reason || 'pending'}） · 最近采样 ${clock(data.state.latest)} · 所选范围最近成功 ${clock(s.last_success)} · 最近失败 ${clock(s.last_failure)}` : last ? `最近采样：${clock(last[0])}${data.now-last[0]>180?' · 数据已过期':''}` : '等待首次采样，通常在 60 秒内出现。';
  $('freshness').title = $('freshness').textContent;
  $('freshness').textContent = `${data.state ? labels[data.state.state] || data.state.state : '采样中'}；最近采样 ${clock(data.state?.latest ?? last?.[0])}；每 ${data.interval} 秒采样，保留参数 ${data.retention_days ?? 7} 天。`;
  const details = data.sample_details;
  const points=[];
  rows.forEach((r,i)=> { const interval = Math.max(details?.[i]?.interval || data.interval, details?.[i-1]?.interval || data.interval); if(i && r[0]-rows[i-1][0]>interval*1.5) points.push({x:(rows[i-1][0]+interval)*1000,y:null}); points.push({x:r[0]*1000,y:r[2]==='ok' && r[1]<=maxLatency?r[1]:null}); });
  const datasets = [{label:'Ping 延迟 (ms)',data:points,borderColor:'#1683ff',pointBackgroundColor:'#1683ff',pointRadius:1,borderWidth:1,spanGaps:false},
    {label:'丢包率 (%)',yAxisID:'loss',data:details ? details.filter(r=>r.sent>r.received && r.status!=='error').map(r=>({x:r.ts*1000,y:100*(r.sent-r.received)/r.sent})) : rows.filter(r=>r[2]==='loss').map(r=>({x:r[0]*1000,y:100})),showLine:false,pointRadius:4,backgroundColor:'#df5b48'},
    {label:'本机错误',data:rows.filter(r=>r[2]==='error').map(r=>({x:r[0]*1000,y:0})),showLine:false,pointRadius:4,backgroundColor:'#888'},
    {label:'高延迟（原值见悬停）',data:rows.filter(r=>r[2]==='ok' && r[1]>maxLatency).map(r=>({x:r[0]*1000,y:maxLatency,actual_ms:r[1]})),showLine:false,pointRadius:4,backgroundColor:'#ff9800'}];
  if (chart) {
    chart.data.datasets = datasets;
    chart.options.scales.x.min = (data.now-hours*3600)*1000;
    chart.options.scales.x.max = data.now*1000;
    chart.options.scales.y.max = maxLatency;
    chart.update('none');
  } else chart = new Chart($('chart'),{type:'line',data:{datasets},options:{animation:false,responsive:true,maintainAspectRatio:false,parsing:false,scales:{x:{type:'linear',min:(data.now-hours*3600)*1000,max:data.now*1000,ticks:{maxTicksLimit:12,callback:v=>new Date(v).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}},y:{beginAtZero:true,max:maxLatency},loss:{position:'right',min:0,max:100,grid:{drawOnChartArea:false},title:{display:true,text:'丢包率 %'}}},plugins:{legend:{labels:{boxHeight:10,font:{size:12}}},tooltip:{callbacks:{title:items=>items.length?new Date(items[0].parsed.x).toLocaleString():'',label:ctx=>ctx.datasetIndex===0?`${ctx.parsed.y} ms`:ctx.datasetIndex===1?`${ctx.parsed.y.toFixed(1)}% 丢包`:ctx.datasetIndex===3?`高延迟：${ctx.raw.actual_ms} ms`:ctx.dataset.label}}}}});
}
$('previous').onclick = () => switchTarget(-1).catch(e=>message(e.message));
$('next').onclick = () => switchTarget(1).catch(e=>message(e.message));
$('filter').onsubmit = async event => {
  event.preventDefault();
  const value = Number($('cap').value);
  if (!Number.isFinite(value) || value<=0 || value>10000) {message('请输入 1–10000 ms 的延迟上限');return;}
  maxLatency = value;
  $('cap-label').textContent = `当前阈值：${value} ms`;
  message('');
  try {await load();} catch(e) {message(e.message);}
};
$('reset').onclick = () => { $('cap').value = '300'; return $('filter').onsubmit({preventDefault(){}}); };
$('add').onsubmit = async event => { event.preventDefault(); try { await api('/api/targets',{address:$('address').value,name:$('name').value}); $('add').reset(); message('已添加，后台将持续采样。'); await refreshTargets(); } catch(e){ message(e.message); } };
$('delete').onclick = async () => { if (!softDeleteSupported) { message('旧版后端仍在运行：为保护历史数据，暂不允许删除目标。请安排升级后再操作。'); return; } if (!confirm('停用此目标？历史数据会保留，重新添加相同 IP 可恢复。')) return; try { await api('/api/delete',{id:selected}); ++generation; await refreshTargets(); } catch(e){message(e.message);} };
$('hours').onchange = () => load().catch(e=>message(e.message));
async function tick() { try { await load(); } catch(e) { message('加载失败：'+e.message); } finally { setTimeout(tick, refreshMs); } }
setTimeout(tick, refreshMs);
refreshTargets().catch(e=>message('加载失败：'+e.message));
