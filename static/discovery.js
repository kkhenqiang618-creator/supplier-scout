'use strict';
/* 「发现」页：探测已有名单 → DeepSeek 关键词搜索 → 滚雪球，再只读探测 new-api / K3.1 Pro（P25，仅本机）。 */
(()=>{
 const STATUS={new:'新发现',ignored:'已忽略',enqueued:'已入队'};
 let discoRows=null,discoLoading=false,discoError='',discoNote='',discoBusy=false;
 const checked=new Set();
 const hostLink=host=>`<a href="${safeLink('https://'+host+'/')}" target="_blank" rel="noopener noreferrer">${esc(host)} ${I('external')}</a>`;
 const money=v=>typeof v==='number'&&Number.isFinite(v)?'$'+Number(v.toFixed(4)):'—';
 const panel=(title,body,actions='')=>`<section class="panel ws-panel"><div class="panel-head"><h2>${title}</h2>${actions}</div>${body}</section>`;
 function entries(item){
  if(!(item.k31||[]).length)return '<span class="ws-muted">未发现 K3.1 Pro</span>';
  return item.k31.map(entry=>{
   const unit=entry.unit_price||{};
   const raw=[entry.quota_type==null?'':`quota_type=${entry.quota_type}`,entry.model_price==null?'':`model_price=${entry.model_price}`,(entry.enable_groups||[]).length?`分组 ${(entry.enable_groups||[]).join(' / ')}`:''].filter(Boolean).join(' · ');
   return `<div class="ws-note" style="margin:0 0 6px"><strong>${esc(entry.model_name)}</strong><br>输入 ${money(unit.input_price)} / 输出 ${money(unit.output_price)} ${unit.unit||''}${raw?`<small class="ws-muted" style="display:block">${esc(raw)}</small>`:''}${entry.model_ratio==null?'':`<small class="ws-muted" style="display:block">model_ratio=${esc(entry.model_ratio)} · completion_ratio=${esc(entry.completion_ratio)} · cache_ratio=${esc(entry.cache_ratio)}</small>`}</div>`;
  }).join('');
 }
 function row(item){
  const badge=item.status==='enqueued'?'qualified':item.status==='ignored'?'skipped':'pending';
  return `<tr>
   <td><input type="checkbox" data-disco-check="${item.id}" ${checked.has(item.id)?'checked':''} aria-label="选择 ${esc(item.host)}"></td>
   <td>${hostLink(item.host)}${item.found_by?`<small class="ws-muted" style="display:block;white-space:normal;max-width:340px">${esc(item.found_by)}</small>`:''}<small class="ws-muted" style="display:block">${item.is_newapi?'new-api':'非 new-api'}${item.system_name?' · '+esc(item.system_name):''}</small>${item.error?`<small class="ws-muted" style="display:block;color:#b4553f">${esc(item.error)}</small>`:''}</td>
   <td>${entries(item)}</td>
   <td>${item.selfhost_label?`<span class="badge pending">${esc(item.selfhost_label)}</span><small class="ws-muted" style="display:block;white-space:normal;max-width:260px">${esc(item.selfhost_sentence)}</small>`:'<span class="ws-muted">—</span>'}${item.group_ratio&&typeof item.group_ratio==='object'?`<small class="ws-muted" style="display:block">分组倍率：${esc(Object.entries(item.group_ratio).map(([k,v])=>k+' ×'+v).slice(0,4).join('，'))}</small>`:''}</td>
   <td><span class="badge ${badge}">${STATUS[item.status]||item.status}</span><small class="ws-muted" style="display:block">${time(item.discovered_at)}</small></td>
   <td><div class="ws-actions"><button class="text-btn" data-disco-ignore="${item.id}">忽略</button>${item.source_url?`<a class="text-btn" href="${safeLink(item.source_url)}" target="_blank" rel="noopener noreferrer">来源页</a>`:''}</div></td>
  </tr>`;
 }
 function renderPage(){
  const title=heading('发现','一键依次探测已有名单、用 DeepSeek 搜索找站、再从来源页滚一层外链，最后只读检查是否为 new-api、是否转售 K3.1 Pro。不会登录、注册或下单。','','DISCOVERY');
  const search=state?.search_service||{};
  const searchText=search.key_error?'搜索 Key 无法解密，请到「能力接入-模型服务与外部接入」重新填写':search.configured?'搜索已接入（关键词和排除站点在「能力接入-模型服务与外部接入」里改）':'搜索未接入：未填写 DeepSeek 搜索 Key，本轮会跳过搜索这一步';
  const form=panel('自动发现',`<div class="settings-content"><div class="form-note">顺序：① 探测已有候选 → ② DeepSeek 关键词搜索 → ③ 从来源页滚一层外链。${esc(searchText)}</div><div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn primary" data-disco-run ${discoBusy?'disabled':''}>${I('globe')}开始发现</button><button class="btn" data-disco-recheck ${discoBusy?'disabled':''}>${I('refresh')}检查已有名单</button></div><label class="field" style="margin-top:14px">人工粘贴网址（可选，仅在需要时补一个来源页）<input id="disco-source" placeholder="例如 https://example.com" autocomplete="off"></label><div><button class="btn small" data-disco-paste ${discoBusy?'disabled':''}>只查这个来源页</button></div><div class="form-note" id="disco-note">${esc(discoNote||'只读取候选站的 /api/status、/api/pricing；遵守 robots.txt，超时 10 秒、并发 4、每轮最多 200 个主机、最多 20 次搜索。')}</div></div>`);
  if(discoRows===null){
   if(!discoLoading&&!discoError)void load();
   return title+form+`<div class="panel empty" role="status">${discoError?`读取失败：${esc(discoError)}`:'正在读取发现名单…'}</div>`;
  }
  const items=discoRows;
  const k31=items.filter(item=>(item.k31||[]).length).length;
  const checks=items.filter(item=>checked.has(item.id)).length;
  const table=panel('候选主机 <span class="chip">'+items.length+' 个</span>',`<div class="table-tools"><span class="form-note">${k31} 个主机找到 K3.1 Pro · 已勾选 ${checks} 个</span><div style="display:flex;gap:10px"><button class="text-btn" data-disco-selectall>${checks===items.length&&items.length?'取消全选':'全选'}</button><button class="btn primary" data-disco-enqueue ${checks?'':'disabled'}>勾选入队</button></div></div><div class="table-scroll"><table><thead><tr><th style="width:34px"></th><th>主机 / 发现来源</th><th>K3.1 Pro 条目与站内标价</th><th>自部署线索 / 分组倍率</th><th>状态 / 发现时间</th><th>操作</th></tr></thead><tbody>${items.map(row).join('')||'<tr><td colspan="6" class="empty">还没有候选主机。点「开始发现」自动探测、搜索并滚一层外链。</td></tr>'}</tbody></table></div>`);
  return title+form+table;
 }
 async function load(){
  if(discoLoading)return;discoLoading=true;discoError='';
  try{const data=await api('discovery');discoRows=data.candidates||[];}catch(e){discoError=e.message;}
  finally{discoLoading=false;if(page==='discovery')renderApp();}
 }
 async function act(label,fn){
  if(discoBusy)return;discoBusy=true;discoNote=label;if(page==='discovery')renderApp();
  try{await fn();}catch(e){discoNote='失败：'+e.message;}finally{discoBusy=false;if(page==='discovery')renderApp();}
 }
 const note=n=>{discoNote=n;if(page==='discovery')renderApp();};
 function pick(){return [...checked];}
 async function run(){
  const typed=($('#disco-source')?.value||'').split(/[\s,，]+/).map(item=>item.trim()).filter(Boolean);
  await act('正在探测已有名单、搜索关键词并滚动外链（每个主机最多 3 个只读请求）…',async()=>{
   const data=await api('discovery/run',{sources:typed});
   discoRows=data.candidates||[];checked.clear();
   const parts=[`探测已有主机 ${data.probed} 个`];
   if(data.search_calls)parts.push(`搜索 ${data.search_calls} 次，找到 ${data.search_hosts} 个主机`);
   if(data.snowball_pages)parts.push(`滚动 ${data.snowball_pages} 个来源页，找到 ${data.snowball_hosts} 个主机`);
   parts.push(`新增候选 ${data.new_hosts} 个`);
   let text=parts.join('；')+'。';
   if((data.skipped||[]).length)text+=' '+(data.skipped||[]).join('；')+'。';
   if((data.errors||[]).length)text+=' 出错 '+(data.errors||[]).length+' 条：'+(data.errors||[])[0];
   note(text);
  });
 }
 async function paste(){
  const source=$('#disco-source')?.value||'';
  if(!source.trim())return note('请先填写要检查的来源网址');
  await act('正在抓取来源页并逐个探测（每个主机最多 3 个只读请求）…',async()=>{
   const data=await api('discovery',{source});
   discoRows=data.candidates||[];checked.clear();
   note(`来源页发现 ${data.found_hosts} 个站外主机：新增 ${data.new_hosts} 个，已在供应商名单 ${data.known_hosts} 个。`);
  });
 }
 async function recheck(){
  await act('正在重新检查已有名单…',async()=>{
   const data=await api('discovery/recheck',{});
   discoRows=data.candidates||[];
   note(`已重新检查 ${data.checked} 个主机，状态保持不变。`);
  });
 }
 async function enqueue(){
  const ids=pick();if(!ids.length)return note('请先勾选要加入名单的主机');
  await act('正在加入供应商名单…',async()=>{
   const data=await api('discovery/enqueue',{ids});
   discoRows=data.candidates||[];checked.clear();
   note(`已加入名单：新增 ${data.added} 家，重复 ${data.duplicates} 条。`);
   await load();
  });
 }
 async function ignore(id){
  await act('正在忽略…',async()=>{
   await api('discovery/ignore',{ids:[id]});checked.delete(id);await load();
   note('已标记为忽略（记录保留，可再次探测）。');
  });
 }
 const originalNav=renderNav;
 renderNav=function(){
  originalNav();
  if(!state?.local)return;
  const logs=$('#sidebar')?.querySelector('[data-nav="logs"]');
  if(!logs)return;
  logs.insertAdjacentHTML('afterend',`<button class="nav ${page==='discovery'?'active':''}" data-nav="discovery" title="发现">${I('globe')}<span>发现</span></button>`);
 };
 const originalRender=renderApp;
 renderApp=function(){
  if(page==='discovery'&&state?.local){renderNav();$('#main').innerHTML=renderPage();lastBusy=state.job.running;return;}
  return originalRender();
 };
 document.addEventListener('change',e=>{
  const id=Number(e.target.dataset?.discoCheck||0);
  if(id){if(e.target.checked)checked.add(id);else checked.delete(id);if(page==='discovery')renderApp();}
 });
 document.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target.id==='disco-source'){e.preventDefault();void paste();}
 });
 document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.hasAttribute('data-disco-run')){e.preventDefault();return void run();}
  if(b.hasAttribute('data-disco-paste')){e.preventDefault();return void paste();}
  if(b.hasAttribute('data-disco-recheck')){e.preventDefault();return void recheck();}
  if(b.hasAttribute('data-disco-enqueue')){e.preventDefault();return void enqueue();}
  if(b.hasAttribute('data-disco-selectall')){e.preventDefault();if(checked.size===discoRows.length)checked.clear();else discoRows.forEach(item=>checked.add(item.id));return renderApp();}
  if(b.dataset.discoIgnore){e.preventDefault();return void ignore(Number(b.dataset.discoIgnore));}
 });
 pages.discovery='发现';
 if(state)renderApp();
})();
