'use strict';
/* Page-specific UI and analytics. Collection and authentication stay in app.js. */
(()=>{
 const EMAIL=/^[^\s@]+@[^\s@]+\.[^\s@]+$/;
 const followLabels={waiting:'待回复',replied:'已回复',quote:'报价待补充',done:'已完成'};
 function identity(row){const name=String(row.name||'').trim();let host=row.url;try{host=new URL(row.url).host}catch{}return {number:/^\d+$/.test(name)?name:String(row.id),name:/^\d+$/.test(name)?'':name,host};}
 function emailEligible(row,includeSkipped=false){return (row.contacts?.email||[]).some(v=>EMAIL.test(v))&&(includeSkipped||!(row.status==='skipped'&&String(row.reason||'').startsWith('人工复核：')));}
 function inFollowup(row,followups){return Boolean(followLabels[followups?.[row.id]?.status]);}
 function resource(value){return ({'自部署':'self','自部署/运营商':'self','运营商':'self',self_deployed:'self','官key':'key','官 key':'key',official_key:'key','官转':'official','官转聚合':'official',official_transfer:'official'})[value]||'unknown';}
 const modelKey=value=>String(value||'').toLowerCase().replace(/[\s_\-.（）()]/g,'');
 function models(data){
  const out=new Map();for(const r of data.rules||[])out.set(r.model,{value:r.model,label:r.label||r.model});
  for(const row of data.suppliers||[])for(const o of row.offers||[]){
   if(!o.model)continue;
   if(!out.has(o.model))out.set(o.model,{value:o.model,label:modelKey(o.model_name)===modelKey(o.model)?o.model_name:o.model});
   if(o.model_name&&modelKey(o.model_name)!==modelKey(o.model)){const key='actual:'+JSON.stringify([o.model,o.model_name]);out.set(key,{value:key,label:o.model_name});}
  }
  return [...out.values()];
 }
 function matchesModel(o,value){if(value?.startsWith('actual:')){try{const [model,name]=JSON.parse(value.slice(7));return o.model===model&&o.model_name===name}catch{return false}}return !value||o.model===value&&(!o.model_name||modelKey(o.model_name)===modelKey(o.model));}
 function matching(o,f){return matchesModel(o,f.model)&&(!f.resource||resource(o.resource_form||o.resource_type)===f.resource)&&(!f.currency||(o.currency||'未说明')===f.currency)&&(!f.unit||(o.unit||'未说明')===f.unit)&&(!f.group||(o.group||'未说明')===f.group);}
 function discount(o,rule,price='input_price'){
  if(resource(o.resource_form||o.resource_type)==='unknown'||!o.currency||!o.unit)return null;
  const uncertain=/条件(?:未确认|不清|待|未说明)|(?:需|待).*询价|最低.*起|起价|价格范围|价格待确认|阶梯价|充值优惠/;
  if(o.conditions_unclear||uncertain.test([o.price_text,o.notes,...(o.risk_flags||[])].filter(Boolean).join('；'))||o.basis==='group_multiplier'||o.basis==='recharge_discount'||o.basis==='recharge')return null;
  const refKey={input_price:'reference_input',output_price:'reference_output',cache_price:'reference_cache'}[price];
  const exactModel=!o.model_name||modelKey(o.model_name)===modelKey(rule?.model)||modelKey(o.model_name)===modelKey(rule?.label);
  const ref=exactModel?rule?.[refKey]:null;
  const calculated=typeof ref==='number'&&ref>0&&rule.currency===o.currency&&rule.unit===o.unit&&typeof o[price]==='number'&&Number.isFinite(o[price])&&o[price]>0?o[price]/ref:null;
  const published=o.basis==='published_discount'&&(!o.ratio_scope||o.ratio_scope==='all'||o.ratio_scope===price||o.ratio_scope===price.replace('_price',''))&&typeof o.ratio==='number'&&Number.isFinite(o.ratio)&&o.ratio>0?o.ratio:null;
  if(calculated!=null&&published!=null&&Math.abs(calculated-published)>1e-6)return null;
  return published??calculated;
 }
 function distribution(data,f){
  const bins=[0,0,0,0,0];let unknown=0,comparable=0;
  for(const row of data.suppliers||[]){
   const offers=(row.offers||[]).filter(o=>matching(o,f));if(!offers.length)continue;
   const values=offers.map(o=>discount(o,(data.rules||[]).find(r=>r.model===o.model),f.price));
   const unique=[...new Set(values.filter(v=>v!=null).map(v=>Number(v.toFixed(8))))];
   // Multiple conflicting same-scope quotes need review, never silently pick the cheapest.
   if(values.some(v=>v==null)||unique.length!==1){unknown++;continue;}
   const r=unique[0];bins[r<.3?0:r<.4?1:r<.5?2:r<.6?3:4]++;comparable++;
  }
  return {bins,unknown,comparable};
 }
 function collectionReason(row){
  const result=row.contact_result||{},diagnostics=result.diagnostics||[];
  if(diagnostics.length)return [...new Set(diagnostics.map(d=>d.label).filter(Boolean))].slice(0,3).join('；');
  const warnings=(result.warnings||[]).join('；');
  if(/域名无法解析|ERR_NAME_NOT_RESOLVED/i.test(warnings))return '域名解析失败';
  if(/ERR_CERT_|SSL:|证书/i.test(warnings))return '网站证书异常';
  if(/ERR_TIMED_OUT|timed out|Timeout/i.test(warnings))return '网站访问超时';
  if(/robots.txt 不允许/i.test(warnings))return '页面访问规则限制';
  if(/只读采集/i.test(warnings))return '页面操作被只读保护拦截';
  return result.contact_summary||'';
 }
 function entrySummary(row){const history=row.contact_result?.entry_history||[];const latest=history[history.length-1];return latest?`${latest.from} → ${latest.to}`:'';}
 const core={entrySummary,collectionReason,identity,emailEligible,inFollowup,resource,models,matching,discount,distribution};
 if(typeof module!=='undefined'&&module.exports)module.exports=core;
 if(typeof document==='undefined')return;
 window.ScoutWorkspace=core;
 pages.contacts='联系与复核';pages.followup='后续处理';pages.review='联系与复核';
 let pricingData=null,pricingLoading=false,pricingError='',pricingRequest=0,pricingOwner='',pricingStamp='';
 let quoteFilter={model:'',resource:'',currency:'',unit:'',group:'',price:'input_price',supplier:''};
 let logSearch='',logDate='',logType='all',draftSkipped=false,followSearch='';
 const originalRender=renderApp,originalNav=renderNav,originalDraft=openDraft,originalSaveDraft=saveDraft;
 const owner=()=>`${state?.username||'local'}:${mode}`;
 const pricingFingerprint=()=>JSON.stringify([state.rules,state.suppliers.map(r=>[r.id,r.checked_at,r.offer_count??r.offers?.length]),state.history?.map(h=>[h.id,h.created_at])]);
 const follow=()=>state.followups||{};
 const mailList=r=>(r.contacts?.email||[]).filter(v=>EMAIL.test(v));
 function site(row){const id=identity(row);return `<div class="ws-site"><span class="ws-number">${esc(id.number)}</span><div><a href="${safeLink(row.url)}" target="_blank" rel="noopener noreferrer">${esc(id.host)} ${I('external')}</a>${id.name?`<small>${esc(id.name)}</small>`:''}</div></div>`;}
 function currentInitial(){return rows().filter(r=>!inFollowup(r,follow()));}
 function shortProgress(){if(!state.job.running)return '';return `<div class="ws-run" role="status"><span>${I('refresh','spin')} ${esc(state.job.current||'采集中')} · ${state.job.done} / ${state.job.total}</span>${stopButton()}</div>`;}
 function pager(total,size=15){const max=Math.max(1,Math.ceil(total/size));pageNo=Math.max(1,Math.min(pageNo,max));return `<div class="table-footer"><span>共 ${total} 家 · 第 ${pageNo} / ${max} 页</span><div class="pagination"><button data-action="prev" ${pageNo<=1?'disabled':''}>‹</button><button data-action="next" ${pageNo>=max?'disabled':''}>›</button></div></div>`;}
 function panel(title,body,actions=''){return `<section class="panel ws-panel"><div class="panel-head"><h2>${title}</h2>${actions}</div>${body}</section>`;}
 function followBadge(r){const f=follow()[r.id];return f?`<span class="badge ${f.status==='done'?'qualified':'pending'}">${followLabels[f.status]||'待处理'}</span>`:'';}
 function reviewButton(r){return `<button class="btn small" data-ws-review="${r.id}">核对报价</button>`;}
 function recordButton(r){return `<button class="btn small ${inFollowup(r,follow())?'':'primary'}" data-ws-follow="${r.id}">${inFollowup(r,follow())?'更新进展':'联系并记录'}</button>`;}
 renderNav=function(){originalNav();const nav=$('#sidebar');const review=nav.querySelector?.('[data-nav="review"]');if(review)review.outerHTML=`<button class="nav ${page==='followup'?'active':''}" data-nav="followup">${I('clock')}<span>后续处理</span><b class="count">${rows().filter(r=>inFollowup(r,follow())).length}</b></button>`;};
 tableRows=function(data){return data.map(r=>`<tr><td>${site(r)}</td><td>${offerModels(r).slice(0,2).map(m=>`<span class="model-label">${esc(modelName(m))}</span>`).join('')||'尚无目标报价'}${offerModels(r).length>2?`<small class="ws-muted">另 ${offerModels(r).length-2} 类模型</small>`:''}</td><td>${badge(r.status)}${followBadge(r)}</td><td>${contactIcons(r)}</td><td>${time(r.contact_checked_at||r.checked_at)}</td><td><div class="ws-actions"><button class="btn small" data-detail="${r.id}">报价与详情 · ${offerCount(r)}</button><button class="text-btn" data-agent-retry="${r.id}">重新采集</button></div></td></tr>`).join('');};
 function suppliers(){
  const all=rows().filter(r=>(filter==='all'||r.status===filter)&&(!search||`${r.name} ${r.id} ${r.url} ${offerModels(r).join(' ')}`.toLowerCase().includes(search.toLowerCase())));
  const footer=pager(all.length);const shown=all.slice((pageNo-1)*15,pageNo*15);
  return heading('供应商名录','完整网站名单、采集状态与最近检查。',importButton()+runButton())+notice()+shortProgress()+panel('全部网站',`<div class="table-tools"><label class="search">${I('search')}<input id="ws-supplier-search" value="${esc(search)}" placeholder="搜索编号、网址、名称或模型" aria-label="搜索网站"></label><select id="ws-supplier-status" aria-label="采集状态"><option value="all">全部状态</option>${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${filter===k?'selected':''}>${v}</option>`).join('')}</select></div><div class="table-scroll"><table><thead><tr><th>编号 / 网站</th><th>目标模型</th><th>处理状态</th><th>联系渠道</th><th>最近检查</th><th>操作</th></tr></thead><tbody>${tableRows(shown)||'<tr><td colspan="6" class="empty">暂无匹配网站，可添加地址或导入表格。</td></tr>'}</tbody></table></div>${footer}`,`<a class="text-btn" href="/api/export?mode=${mode}">导出 CSV</a>`);
 }
 function contactsPage(){
  const initial=currentInitial();
  const tasks=[['all','待处理'],['todo_manual','待联系'],['todo_email','可邮件联系'],['todo_price','报价待复核'],['todo_contact','线索待补查']];
  const all=initial.filter(r=>(filter==='all'||matchesTask(r,filter))&&(!search||`${r.id} ${r.name} ${r.url} ${JSON.stringify(r.contacts)}`.toLowerCase().includes(search.toLowerCase())));
  const footer=pager(all.length),shown=all.slice((pageNo-1)*15,pageNo*15);
  return heading('联系与复核','先核实线索、完成首次联系；保存联系记录后进入后续处理。',importButton()+runButton(),'CONTACT & REVIEW')+notice()+shortProgress()+panel('联系线索与待办',`<div class="ws-tabs">${tasks.map(([key,label])=>`<button data-filter="${key}" class="filter ${filter===key?'active':''}">${label} <b>${initial.filter(r=>key==='all'||matchesTask(r,key)).length}</b></button>`).join('')}</div><div class="table-tools"><label class="search">${I('search')}<input id="ws-contact-search" value="${esc(search)}" placeholder="搜索编号、网址或联系方式" aria-label="搜索联系待办"></label><button class="text-btn" data-nav="followup">查看已联系网站 ${I('arrow')}</button></div><div class="table-scroll"><table class="ws-contact-table"><thead><tr><th>编号 / 网站</th><th>联系渠道 / 用途</th><th>待处理事项</th><th>操作</th></tr></thead><tbody>${shown.map(r=>`<tr><td>${site(r)}</td><td>${contactColumn(r)}</td><td>${stageBadges(r)}<small class="ws-muted ws-clamp">${esc(collectionReason(r))}</small>${entrySummary(r)?`<small class="ws-muted ws-clamp" title="${esc(entrySummary(r))}">网址已核验更新 · ${esc(entrySummary(r))}</small>`:''}</td><td><div class="ws-actions">${Object.values(r.contacts||{}).some(v=>v.length)?recordButton(r):`<button class="btn small" data-agent-retry="${r.id}">继续查找</button>`}${followupStages(r).some(s=>s.key==='todo_price')||r.status==='review'?reviewButton(r):''}</div></td></tr>`).join('')||'<tr><td colspan="4" class="empty">当前暂无匹配待办；已联系的网站在“后续处理”中。</td></tr>'}</tbody></table></div>${footer}`,`<a class="text-btn" href="/api/export-contacts?mode=${mode}">导出联系方式</a>`);
 }
 function followupPage(){
  const allRows=rows().filter(r=>inFollowup(r,follow()));
  const all=allRows.filter(r=>(filter==='all'||follow()[r.id].status===filter)&&(!followSearch||`${r.name} ${r.url} ${r.id} ${follow()[r.id].note}`.toLowerCase().includes(followSearch.toLowerCase())));
  const footer=pager(all.length),shown=all.slice((pageNo-1)*15,pageNo*15);
  return heading('后续处理','集中处理已联系网站的回复、补充报价与跟进结果。',`<button class="btn" data-nav="contacts">返回待处理</button>`,'FOLLOW-UP')+notice()+shortProgress()+panel('已联系的网站',`<div class="ws-tabs">${[['all','全部'],...Object.entries(followLabels)].map(([key,label])=>`<button class="filter ${filter===key?'active':''}" data-filter="${key}">${label} <b>${allRows.filter(r=>key==='all'||follow()[r.id].status===key).length}</b></button>`).join('')}</div><div class="table-tools"><label class="search">${I('search')}<input id="ws-follow-search" value="${esc(followSearch)}" placeholder="搜索网站或跟进备注" aria-label="搜索后续处理"></label></div><div class="table-scroll"><table class="ws-contact-table"><thead><tr><th>编号 / 网站</th><th>联系渠道</th><th>处理状态 / 最近备注</th><th>操作</th></tr></thead><tbody>${shown.map(r=>{const f=follow()[r.id];return `<tr><td>${site(r)}</td><td>${contactColumn(r)}</td><td>${followBadge(r)}<p class="ws-note">${esc(f.note||'暂无备注')}</p><small class="ws-muted">${time(f.updated_at)}</small>${followupStages(r).some(s=>s.key==='todo_price')?'<small class="ws-muted">报价仍待核对</small>':''}</td><td><div class="ws-actions">${recordButton(r)}${reviewButton(r)}<button class="text-btn" data-ws-history="${r.id}">跟进记录</button></div></td></tr>`}).join('')||'<tr><td colspan="4" class="empty">暂无此状态记录。完成首次联系并保存后，网站会出现在这里。</td></tr>'}</tbody></table></div>${footer}`);
 }
 function chartBars(values){const max=Math.max(1,...values.map(v=>v[1]));return `<div class="ws-bars">${values.map(([label,n,color])=>`<div class="ws-bar"><span>${esc(label)}</span><div><i style="width:${n/max*100}%;background:${color||'#7c6bcd'}"></i></div><strong>${n} 家</strong></div>`).join('')}</div>`;}
 function dashboard(){
  const all=rows(),contactCounts=[['已找到',0,'#65a88f'],['待核验',0,'#d1ad64'],['未查清 / 受阻',0,'#b88b9b'],['尚未检查',0,'#aaa9bd']];
  for(const r of all){const k=r.contact_status;contactCounts[k==='found'?0:['legacy','needs_review'].includes(k)?1:['incomplete','blocked'].includes(k)?2:3][1]++;}
  const c=counts(),found=all.filter(r=>Object.values(r.contacts||{}).some(v=>v.length)).length,pending=currentInitial().length,followCount=all.length-pending;
  return heading('供应商工作台','采集进展、联系方式覆盖和人工待办。',importButton()+runButton())+notice()+`<div class="ws-summary"><div><span>供应商网站</span><strong>${all.length}</strong></div><div><span>有联系方式</span><strong>${found}<small> / ${all.length}</small></strong></div><div><span>后续处理中</span><strong>${followCount}</strong></div></div>`+pipeline()+`<div class="ws-two">${panel('联系方式覆盖',chartBars(contactCounts),`<button class="text-btn" data-nav="contacts">处理线索 ${I('arrow')}</button>`)}${panel('采集与判断进展',chartBars([['待采集',c.pending||0],['报价待复核',c.review||0],['符合区间',c.qualified||0],['已跳过',c.skipped||0],['采集异常',c.error||0]]),`<button class="text-btn" data-nav="suppliers">查看名单 ${I('arrow')}</button>`)}</div><div class="ws-two">${panel('待处理事项',`<div class="ws-todo"><button data-nav="contacts"><strong>${pending}</strong><span>家网站待联系 / 复核</span>${I('arrow')}</button><button data-nav="followup"><strong>${followCount}</strong><span>家网站已进入后续处理</span>${I('arrow')}</button></div>`)}${panel('最近动态',`<div class="activity">${activities(3)}</div>`,`<button class="text-btn" data-nav="logs">全部日志 ${I('arrow')}</button>`)}</div>`;
 }
 function draftsPage(){
  const saved=state.drafts.filter(d=>rows().some(r=>r.id===d.supplier_id)),eligible=rows().filter(r=>emailEligible(r,draftSkipped));
  return heading('询价草稿','有有效邮箱即可准备询价；报价待复核也可先询问条件。','','OUTREACH')+notice()+panel('有邮箱 · 待准备询价',`<div class="table-tools"><span class="form-note">${eligible.length} 家网站 · ${saved.length} 份已保存草稿</span><label class="ws-checkbox"><input type="checkbox" id="ws-draft-skipped" ${draftSkipped?'checked':''}>包含人工明确跳过的网站</label></div><div class="table-scroll"><table><thead><tr><th>编号 / 网站</th><th>邮箱 / 用途</th><th>报价状态</th><th>草稿</th></tr></thead><tbody>${eligible.map(r=>`<tr><td>${site(r)}</td><td>${mailList(r).map(v=>`<p class="ws-note">${esc(v)}<small class="ws-muted">${esc(contactEntries(r).find(c=>c.type==='email'&&c.value===v)?.purpose||'用途待确认')}</small></p>`).join('')}</td><td>${badge(r.status)}</td><td><button class="btn small" data-draft="${r.id}">${saved.some(d=>d.supplier_id===r.id)?'编辑草稿':'准备询价'}</button></td></tr>`).join('')||'<tr><td colspan="4" class="empty">暂无有效邮箱；可先到“联系与复核”查找线索。</td></tr>'}</tbody></table></div>`)+panel('已保存草稿 · 未发送',saved.map(d=>{const r=rows().find(r=>r.id===d.supplier_id);return `<div class="ws-draft-row"><div>${site(r)}<p class="ws-note">${esc(d.subject)}</p><small class="ws-muted">${esc(d.recipient||mailList(r)[0]||'邮箱待核对')} · ${time(d.created_at)} · 未发送</small></div><button class="btn small" data-draft="${r.id}">编辑 / 下载</button></div>`}).join('')||'<div class="empty">尚未保存草稿。选择上方网站准备第一封询价邮件。</div>');
 }
 openDraft=function(id){originalDraft(id);const r=state.suppliers.find(r=>r.id===id);if(!r)return;const emails=mailList(r);if(!emails.length)return;const saved=state.drafts.find(d=>d.supplier_id===id);const input=$('#modal .dialog-body .field input');if(input){input.outerHTML=`<select id="ws-draft-recipient">${emails.map(v=>`<option value="${esc(v)}" ${saved?.recipient===v?'selected':''}>${esc(v)}</option>`).join('')}</select>`;}if(!identity(r).name&&!saved){$('#draft-body').value=$('#draft-body').value.replace(`您好，${r.name}团队：`,'您好：');}};
 saveDraft=async function(id,download){const recipient=$('#ws-draft-recipient')?.value;if(!recipient)return originalSaveDraft(id,download);try{await api('drafts',{id,subject:$('#draft-subject').value,body:$('#draft-body').value,recipient});await load();if(download){const a=document.createElement('a');a.href='/api/draft?id='+id;a.download='inquiry-'+id+'.eml';document.body.append(a);a.click();a.remove();}toast(download?'草稿已保存并下载，邮件未发送':'草稿已保存，邮件未发送');}catch(e){$('#draft-error').textContent=e.message;}};
 function resetPricing(){pricingRequest++;pricingData=null;pricingError='';pricingLoading=false;pricingOwner=owner();quoteFilter={model:'',resource:'',currency:'',unit:'',group:'',price:'input_price',supplier:''};}
 async function loadPricing(){
  if(pricingLoading)return;pricingLoading=true;pricingError='';const request=++pricingRequest,key=owner(),stamp=pricingFingerprint();
  try{const data=await api('pricing?mode='+encodeURIComponent(mode));if(request!==pricingRequest||key!==owner())return;pricingData=data;pricingStamp=stamp;}
  catch(e){if(request===pricingRequest)pricingError=e.message;}
  finally{if(request===pricingRequest){pricingLoading=false;if(page==='insights')renderApp();}}
 }
 function selectHTML(id,label,options,value){return `<label class="ws-field">${label}<select id="${id}">${options.map(([k,v])=>`<option value="${esc(k)}" ${String(value)===String(k)?'selected':''}>${esc(v)}</option>`).join('')}</select></label>`;}
 function quoteOptions(data){return (data.suppliers||[]).flatMap(r=>r.offers||[]).filter(o=>matchesModel(o,quoteFilter.model));}
 function normalizeFilters(data){
  const ms=models(data);if(!ms.some(m=>m.value===quoteFilter.model))quoteFilter.model=ms[0]?.value||'';
  const offers=quoteOptions(data);for(const [key,get,empty]of [['currency',o=>o.currency||'未说明','未说明'],['unit',o=>o.unit||'未说明','未说明'],['group',o=>o.group||'未说明','未说明']]){const values=[...new Set(offers.map(get))];if(!values.includes(quoteFilter[key]))quoteFilter[key]=values[0]||empty;}
  const forms=[...new Set(offers.map(o=>resource(o.resource_form||o.resource_type)))];if(!quoteFilter.resource)quoteFilter.resource=forms[0]||'unknown';
  const suppliers=data.suppliers||[];if(!suppliers.some(r=>String(r.id)===String(quoteFilter.supplier)))quoteFilter.supplier=String(suppliers[0]?.id||'');
 }
 function trendHTML(data){
  const supplier=(data.suppliers||[]).find(r=>String(r.id)===String(quoteFilter.supplier));
  if(!supplier)return '<div class="empty">暂无供应商历史记录。</div>';
  const entries=(data.history||[]).filter(h=>h.supplier_id===supplier.id).map(h=>{const matchingOffers=(h.offers||[]).filter(o=>matching(o,quoteFilter));const values=[...new Set(matchingOffers.map(o=>o[quoteFilter.price]).filter(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0))];return {date:Date.parse(h.created_at),value:values.length===1?values[0]:null};}).filter(h=>Number.isFinite(h.date)).sort((a,b)=>a.date-b.date).slice(-40);
  const points=entries.filter(p=>p.value!=null);if(!points.length)return '<div class="empty">当前筛选下暂无同口径历史单价；未识别的数值不补画。</div>';
  const lo=Math.min(...points.map(p=>p.value)),hi=Math.max(...points.map(p=>p.value)),pad=(hi-lo)*.2||Math.max(hi*.1,1),min=Math.max(0,lo-pad),max=hi+pad;
  const first=entries[0].date,last=entries.at(-1).date;const x=p=>55+(last===first?260:(p.date-first)/(last-first)*500),y=p=>140-(p.value-min)/(max-min)*105;
  const segments=[];let segment=[];for(const p of entries){if(p.value==null){if(segment.length)segments.push(segment);segment=[];}else segment.push(p);}if(segment.length)segments.push(segment);
  const fmt=v=>Number(v.toFixed(4)).toLocaleString('zh-CN');
  return `<p class="ws-muted ws-chart-unit">${esc(quoteFilter.currency)} / ${esc(quoteFilter.unit)} · ${points.length} 个有效历史记录${entries.some(p=>p.value==null)?' · 缺失/冲突报价处断线':''}</p><svg class="ws-line-chart" viewBox="0 0 620 185" role="img" aria-label="${esc(identity(supplier).host)} 的同口径历史价格"><line x1="55" y1="35" x2="555" y2="35"/><line x1="55" y1="140" x2="555" y2="140"/><text x="48" y="39" text-anchor="end">${fmt(max)}</text><text x="48" y="144" text-anchor="end">${fmt(min)}</text>${segments.map(ps=>`<polyline points="${ps.map(p=>`${x(p)},${y(p)}`).join(' ')}"/>`).join('')}${points.map(p=>`<circle cx="${x(p)}" cy="${y(p)}" r="3"><title>${new Date(p.date).toLocaleString('zh-CN')} · ${p.value}</title></circle>`).join('')}<text x="55" y="173">${new Date(first).toLocaleDateString('zh-CN')}</text><text x="555" y="173" text-anchor="end">${new Date(last).toLocaleDateString('zh-CN')}</text></svg><details class="ws-definition"><summary>查看历史数值</summary><div class="table-scroll"><table><thead><tr><th>记录时间</th><th>单价</th></tr></thead><tbody>${points.map(p=>`<tr><td>${new Date(p.date).toLocaleString('zh-CN')}</td><td>${fmt(p.value)} ${esc(quoteFilter.currency)} / ${esc(quoteFilter.unit)}</td></tr>`).join('')}</tbody></table></div></details>`;
 }
 function insightsPage(){
  const title=heading('报价洞察','查看同口径折扣分布与真实历史变化。',`<button class="btn" data-ws-pricing-refresh>${I('refresh')}刷新报价</button>`,'PRICING INTELLIGENCE')+notice();
  if(!pricingData){if(!pricingLoading&&!pricingError)void loadPricing();return title+`<div class="panel empty" role="status">${pricingError?`报价加载失败：${esc(pricingError)}<br><button class="btn small" data-ws-pricing-refresh>重试</button>`:'正在加载完整报价与历史…'}</div>`;}
  normalizeFilters(pricingData);const data=pricingData,offers=quoteOptions(data),unique=(key)=>[...new Set(offers.map(o=>o[key]||'未说明'))].map(v=>[v,v]);
  const selector=selectHTML('ws-model','模型',models(data).map(m=>[m.value,m.label]),quoteFilter.model)+selectHTML('ws-price','计价项',[['input_price','输入单价'],['output_price','输出单价'],['cache_price','缓存读取单价']],quoteFilter.price)+selectHTML('ws-resource','统一折扣 · 资源类型',[['key','官 key'],['self','自部署/运营商'],['official','官转聚合'],['unknown','待确认']],quoteFilter.resource)+selectHTML('ws-currency','币种',unique('currency'),quoteFilter.currency)+selectHTML('ws-unit','计价单位',unique('unit'),quoteFilter.unit)+selectHTML('ws-group','报价分组',unique('group'),quoteFilter.group);
  const selectedRule=data.rules.find(r=>matchesModel({model:r.model},quoteFilter.model));
  const baseKnown=selectedRule?.currency===quoteFilter.currency&&selectedRule?.unit===quoteFilter.unit;
  const refs=[['目录价：输入单价','reference_input'],['目录价：输出单价','reference_output'],['目录价：缓存读取','reference_cache']].map(([label,key])=>`<div><span>${label}</span><strong>${baseKnown&&Number.isFinite(selectedRule[key])?esc(selectedRule[key])+' '+esc(quoteFilter.currency):'待确认'}</strong></div>`).join('');
  const result=distribution(data,quoteFilter),max=Math.max(1,...result.bins),names=['＜3 折','3–4 折','4–5 折','5–6 折','≥6 折'];
  return title+shortProgress()+`<div class="ws-quote-filters">${selector}</div><div class="ws-reference">${refs}</div>`+panel('折扣区间分布',`<div class="ws-chart-context">${esc(models(data).find(m=>m.value===quoteFilter.model)?.label||'暂无模型')} · ${esc(({self:'自部署/运营商',key:'官 key',official:'官转聚合',unknown:'待确认'})[quoteFilter.resource])} · ${esc(quoteFilter.currency)} / ${esc(quoteFilter.unit)}</div><div class="ws-chart-summary"><strong>${result.comparable}</strong> 家可比供应商 <span>${result.unknown} 家待核对 · 未计入分布</span></div>${result.comparable?`<div class="ws-histogram" role="img" aria-label="折扣区间供应商家数：${names.map((n,i)=>n+result.bins[i]+'家').join('；')}">${names.map((name,i)=>`<div class="ws-bin"><span>${result.bins[i]} 家<small>${Math.round(result.bins[i]/result.comparable*100)}%</small></span><i style="height:${result.bins[i]/max*150}px"></i><label>${name}</label></div>`).join('')}</div>`:'<div class="empty">当前口径暂无可核实的折扣数据。缺少基准或条件不清的报价保留为待核对。</div>'}<details class="ws-definition"><summary>完整字段与统计口径</summary><p>每家供应商取当前模型、币种、单位、资源类型及分组下的报价；冲突或缺依据的记录不自动选低价。3 折计入 3–4 折，6 折计入 ≥6 折。</p><p>目录价、输入/输出/缓存单价、分组倍率、资源类型、适用条件、报价来源与证据分别保留；充值优惠和分组倍率不冒充目录价折扣。统计使用完整报价，列表摘要不参与计算。</p><div class="table-scroll"><table><thead><tr><th>编号 / 网站</th><th>实际模型</th><th>输入 / 输出 / 缓存</th><th>分组 / 倍率</th><th>来源与条件</th></tr></thead><tbody>${data.suppliers.flatMap(r=>(r.offers||[]).filter(o=>matching(o,quoteFilter)).map(o=>`<tr><td>${site(r)}</td><td>${esc(o.model_name||o.model)}</td><td>${[o.input_price,o.output_price,o.cache_price].map(v=>v==null?'—':esc(v)).join(' / ')}<small class="ws-muted">${esc(o.currency||'未说明')} / ${esc(o.unit||'未说明')}</small></td><td>${esc(o.group||'未说明')} / ${o.group_multiplier==null?'待确认':'×'+esc(o.group_multiplier)}</td><td>${o.source?`<a href="${safeLink(o.source)}" target="_blank" rel="noopener noreferrer">报价来源</a>`:'来源待确认'}<p class="ws-note">${esc(o.notes||o.price_text||o.reason||'条件待确认')}</p></td></tr>`)).join('')||'<tr><td colspan="5">暂无同口径报价</td></tr>'}</tbody></table></div></details>`)+panel('历史价格变化',`<div class="ws-trend-picker">${selectHTML('ws-trend-supplier','供应商',(data.suppliers||[]).map(r=>[r.id,identity(r).host]),quoteFilter.supplier)}</div>${trendHTML(data)}`);
 }
 function logsPage(){
  const events=state.events.filter(e=>e.mode===mode&&(!logSearch||`${e.message} ${state.suppliers.find(r=>r.id===e.supplier_id)?.url||''}`.toLowerCase().includes(logSearch.toLowerCase()))&&(!logDate||String(e.created_at).slice(0,10)===logDate)&&(logType==='all'||/失败|异常|受阻|失联|错误/.test(e.message)));
  const footer=pager(events.length,20),shown=events.slice((pageNo-1)*20,pageNo*20),groups=new Map();
  for(const e of shown){const date=String(e.created_at).slice(0,10);if(!groups.has(date))groups.set(date,[]);groups.get(date).push(e);}
  return heading('运行日志','按日期查看采集与人工处理记录；详细过程展开阅读。',`<button class="btn" data-action="refresh">${I('refresh')}刷新</button>`,'ACTIVITY LOG')+notice()+panel('工作区动态',`<div class="ws-log-tools"><label class="search">${I('search')}<input id="ws-log-search" value="${esc(logSearch)}" aria-label="搜索日志" placeholder="搜索网站或日志内容"></label><label class="ws-field">日期<input type="date" id="ws-log-date" value="${esc(logDate)}"></label>${selectHTML('ws-log-type','结果类型',[['all','全部记录'],['error','失败 / 异常']],logType)}</div><div class="ws-log-groups">${[...groups].map(([date,entries])=>`<section><h3>${esc(date)}</h3>${entries.map(e=>`<details class="ws-log-entry"><summary><time>${time(e.created_at)}</time><span>${esc(e.message.slice(0,95))}${e.message.length>95?'…':''}</span></summary><div><p>${esc(e.message)}</p>${e.supplier_id?`<button class="text-btn" data-detail="${e.supplier_id}">查看相关网站 ${I('arrow')}</button>`:''}</div></details>`).join('')}</section>`).join('')||'<div class="empty">当前筛选下暂无日志</div>'}</div>${footer}<p class="ws-muted ws-log-limit">显示当前工作区已加载的最近 ${state.events.filter(e=>e.mode===mode).length} 条记录，每页 20 条。</p>`);
 }
 renderApp=function(){
  if(!state)return;if(pricingOwner!==owner())resetPricing();
  if(page==='insights'&&pricingData&&pricingStamp!==pricingFingerprint()){pricingData=null;pricingError='';}
  if(page==='review'){page='contacts';filter='all';}
  const renderers={dashboard,suppliers,contacts:contactsPage,followup:followupPage,drafts:draftsPage,insights:insightsPage,logs:logsPage};
  if(!renderers[page])return originalRender();renderNav();$('#main').innerHTML=renderers[page]();lastBusy=state.job.running;
 };
 function openFollowup(id,historyOnly=false){
  const r=rows().find(r=>r.id===id);if(!r)return;const f=follow()[id];
  if(historyOnly){openModal(modalHead('跟进记录',identity(r).host)+`<div class="dialog-body">${(f?.history||[]).slice().reverse().map(h=>`<div class="history-item"><strong>${esc(followLabels[h.status]||h.status)}</strong> · ${time(h.updated_at)}<p>${esc(h.note||'')}</p></div>`).join('')||'<p>暂无历史跟进记录</p>'}</div>`,'followup-history');return;}
  openModal(modalHead(f?'更新后续处理':'联系并记录',identity(r).host)+`<div class="dialog-body"><p class="form-note">请在你已人工添加或联系后记录。保存后进入后续处理；不会发送任何消息。</p><label class="field">处理状态<select id="ws-follow-status">${Object.entries(followLabels).map(([key,label])=>`<option value="${key}" ${(f?.status||'waiting')===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="field">联系渠道 / 跟进备注<textarea id="ws-follow-note" rows="4" maxlength="1500" placeholder="例如：已添加企业微信，等待对方回复">${esc(f?.note||'')}</textarea></label><div class="form-error" id="ws-follow-error"></div></div><div class="dialog-footer"><button class="btn" data-action="close-modal">取消</button><button class="btn primary" data-ws-save-follow="${id}">${f?'保存进展':'保存并进入后续处理'}</button></div>`,'followup');
 }
 function openReview(id){const r=rows().find(r=>r.id===id);if(!r)return;openModal(modalHead('核对报价',identity(r).host)+`<div class="dialog-body"><p class="form-note">报价结论独立保存，不会把网站标记为已联系。</p><label class="field">复核结果<select id="review-status"><option value="qualified" ${r.status==='qualified'?'selected':''}>符合区间 · 继续跟进</option><option value="review" ${!['qualified','skipped'].includes(r.status)?'selected':''}>待复核 · 继续补查</option><option value="skipped" ${r.status==='skipped'?'selected':''}>暂不跟进</option></select></label><label class="field">判断依据<textarea id="review-note" rows="4">${esc(r.notes||'')}</textarea></label><div class="form-error" id="review-error"></div></div><div class="dialog-footer"><button class="btn" data-detail="${id}">查看完整报价</button><button class="btn primary" data-ws-save-review="${id}">保存复核结果</button></div>`,'ws-review');}
 document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.wsFollow)return openFollowup(Number(b.dataset.wsFollow));
  if(b.dataset.wsHistory)return openFollowup(Number(b.dataset.wsHistory),true);
  if(b.dataset.wsReview)return openReview(Number(b.dataset.wsReview));
  if(b.hasAttribute('data-ws-pricing-refresh')){pricingData=null;pricingError='';void loadPricing();return renderApp();}
  if(b.dataset.wsSaveFollow){b.disabled=true;const username=state.username;try{await api('followup',{id:Number(b.dataset.wsSaveFollow),status:$('#ws-follow-status').value,note:$('#ws-follow-note').value});if(state.username!==username)return;$('#modal').close();page='followup';filter='all';pageNo=1;await load();toast('联系记录已保存，已进入后续处理');}catch(err){const box=$('#ws-follow-error');if(box)box.textContent=err.message;}finally{b.disabled=false;}return;}
  if(b.dataset.wsSaveReview){b.disabled=true;try{await api('review',{id:Number(b.dataset.wsSaveReview),status:$('#review-status').value,note:$('#review-note').value});$('#modal').close();await load();toast('复核结果已保存，联系进度保持独立');}catch(err){$('#review-error').textContent=err.message;}finally{b.disabled=false;}return;}
 });
 document.addEventListener('change',e=>{
  const mapping={'ws-model':'model','ws-price':'price','ws-resource':'resource','ws-currency':'currency','ws-unit':'unit','ws-group':'group','ws-trend-supplier':'supplier'};
  if(mapping[e.target.id]){quoteFilter[mapping[e.target.id]]=e.target.value;if(e.target.id==='ws-model'){quoteFilter.resource='';quoteFilter.currency='';quoteFilter.unit='';quoteFilter.group='';}renderApp();}
  if(e.target.id==='ws-supplier-status'){filter=e.target.value;pageNo=1;renderApp();}
  if(e.target.id==='ws-draft-skipped'){draftSkipped=e.target.checked;renderApp();}
  if(e.target.id==='ws-log-date'){logDate=e.target.value;pageNo=1;renderApp();}
  if(e.target.id==='ws-log-type'){logType=e.target.value;pageNo=1;renderApp();}
 });
 document.addEventListener('input',e=>{
  const id=e.target.id;if(!['ws-supplier-search','ws-contact-search','ws-follow-search','ws-log-search'].includes(id))return;
  const value=e.target.value,pos=e.target.selectionStart;
  if(id==='ws-follow-search')followSearch=value;else if(id==='ws-log-search')logSearch=value;else search=value;pageNo=1;renderApp();const input=$('#'+id);input.focus();input.setSelectionRange(pos,pos);
 });
 if(state)renderApp();
})();
