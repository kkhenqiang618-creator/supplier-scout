'use strict';
let teamOverview=null,teamMember=null,teamSelected='',teamBusy=false,teamError='',teamRequest=0,teamPage=1;
async function loadTeam(username=teamSelected){
 if(!state?.is_admin)return;
 const request=++teamRequest;teamBusy=true;teamError='';teamSelected=username;teamMember=null;renderTeam();
 try{
  const overview=await api('admin/overview');
  const member=username?await api('admin/member?username='+encodeURIComponent(username)):null;
  if(request!==teamRequest)return;teamOverview=overview;teamMember=member;teamPage=1;
 }catch(error){if(request===teamRequest)teamError=error.message}
 finally{if(request===teamRequest){teamBusy=false;if(page==='team')renderTeam()}}
}
function renderTeam(){
 if(!state)return;renderNav();
 if(!state.is_admin){$('#main').innerHTML=heading('无访问权限','成员询价总览仅对超级管理员开放。');return}
 const members=teamOverview?.members||[];
 $('#main').innerHTML=heading('成员询价总览','各成员的数据独立保存；此处可只读查看全员进度与询价记录。',`<button class="btn" data-team-refresh ${teamBusy?'disabled':''}>${I('refresh')}刷新总览</button>`,'TEAM OVERVIEW')+
 (teamError?`<div class="notice">${esc(teamError)}</div>`:'')+
 `<section class="panel"><div class="panel-head"><strong>成员工作区</strong><span class="chip">仅超级管理员可见</span></div><div class="table-scroll"><table><thead><tr><th>成员</th><th>供应商</th><th>已找到联系方式</th><th>符合区间</th><th>询价草稿</th><th>最近采集</th><th></th></tr></thead><tbody>${members.map(m=>{const job=m.jobs[0];return `<tr><td><strong>${esc(m.username)}</strong></td><td>${m.suppliers}</td><td>${m.contacts_found}</td><td>${m.qualified}</td><td>${m.drafts}</td><td>${job?esc(({completed:'已完成',stopped:'已停止',failed:'失败',queued:'排队中',running:'采集中',stopping:'停止中'})[job.status]||job.status)+' · '+job.done+'/'+job.total:'暂无任务'}</td><td><button class="btn small" data-team-member="${esc(m.username)}" ${teamBusy?'disabled':''}>查看工作区</button></td></tr>`}).join('')||`<tr><td colspan="7">${teamBusy?'正在加载成员数据…':'尚无数据'}</td></tr>`}</tbody></table></div></section>`+
 (teamBusy?'<div class="panel empty">正在加载…</div>':teamMember?teamMemberHTML():`<div class="panel empty">选择成员，查看其供应商、报价、联系方式、询价草稿和日志。</div>`);
 if(!teamOverview&&!teamBusy&&!teamError)loadTeam();
}
function teamMemberHTML(){
 const all=teamMember.suppliers.filter(r=>r.mode==='real'),totalPages=Math.max(1,Math.ceil(all.length/20));teamPage=Math.min(teamPage,totalPages);
 const shown=all.slice((teamPage-1)*20,teamPage*20);
 const contacts=r=>Object.entries(r.contacts||{}).flatMap(([kind,values])=>values.map(value=>esc((contactTypes[kind]||kind)+': '+value))).join('<br>')||'—';
 return `<section class="panel" style="margin-top:20px"><div class="panel-head"><strong>${esc(teamSelected)} · 只读工作区</strong><span class="chip">${all.length} 家供应商</span></div><div class="table-scroll"><table><thead><tr><th>编号 / 供应商</th><th>状态</th><th>报价</th><th>联系方式</th><th></th></tr></thead><tbody>${shown.map(r=>`<tr><td><strong>${esc(r.name)}</strong><small style="display:block">${esc(r.url)}</small></td><td>${badge(r.status)}</td><td>${offerCount(r)} 条</td><td style="overflow-wrap:anywhere;max-width:290px">${contacts(r)}</td><td><button class="btn small" data-team-detail="${r.id}" data-team-owner="${esc(teamSelected)}">查看详情</button></td></tr>`).join('')||'<tr><td colspan="5">该成员尚未导入供应商。</td></tr>'}</tbody></table></div><div class="panel-head"><span>${teamPage} / ${totalPages} 页</span><div><button class="btn small" data-team-page="${teamPage-1}" ${teamPage<=1?'disabled':''}>上一页</button> <button class="btn small" data-team-page="${teamPage+1}" ${teamPage>=totalPages?'disabled':''}>下一页</button></div></div></section>`+
 `<section class="panel" style="margin-top:20px"><div class="panel-head"><strong>询价草稿 · ${teamMember.drafts.length} 份</strong></div><div class="settings-content">${teamMember.drafts.map(d=>`<details class="history-item"><summary>${esc(d.subject)} · ${time(d.created_at)} · 未发送</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(d.body)}</pre></details>`).join('')||'尚无询价草稿'}</div></section>`+
 `<section class="panel" style="margin-top:20px"><div class="panel-head"><strong>最近处理记录</strong></div><div class="settings-content">${teamMember.events.slice(0,50).map(e=>`<div class="history-item">${time(e.created_at)} · ${esc(e.message)}</div>`).join('')||'尚无处理记录'}</div></section>`;
}
document.addEventListener('click',async event=>{
 const el=event.target.closest('button');if(!el||!state?.is_admin)return;
 if(el.hasAttribute('data-team-refresh'))return loadTeam();
 if(el.dataset.teamMember!==undefined)return loadTeam(el.dataset.teamMember);
 if(el.dataset.teamPage!==undefined){teamPage=Number(el.dataset.teamPage);return renderTeam()}
 if(el.dataset.teamDetail){
  el.disabled=true;try{
   const username=el.dataset.teamOwner,r=await api('admin/supplier?username='+encodeURIComponent(username)+'&id='+encodeURIComponent(el.dataset.teamDetail));
   openModal(modalHead(esc(r.name),'只读 · 所属成员：'+esc(username))+`<div class="dialog-body"><a href="${safeLink(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.url)}</a><p>${esc(r.reason)}</p><h3>联系方式</h3>${Object.entries(r.contacts).map(([kind,values])=>`<p>${esc(contactTypes[kind]||kind)}：${values.map(esc).join('、')}</p>`).join('')||'尚无联系线索'}<h3>报价</h3>${quoteCards(r)}<h3>联系证据与来源</h3>${(r.contact_result.contact_records||[]).map(c=>`<div class="history-item"><strong>${esc(c.value)}</strong><p>${esc(c.purpose)}</p><div class="evidence">${esc(c.evidence)}</div><a href="${safeLink(c.source_url)}" target="_blank" rel="noopener noreferrer">${esc(c.source_url)}</a></div>`).join('')}${r.sources.map(s=>`<details class="history-item"><summary>${esc(s.url)}</summary><div class="evidence">${esc(s.excerpt)}</div></details>`).join('')}<h3>人工备注</h3><p>${esc(r.notes||'暂无备注')}</p></div>`,'team-detail');
  }catch(error){toast(error.message)}finally{el.disabled=false}
 }
});
