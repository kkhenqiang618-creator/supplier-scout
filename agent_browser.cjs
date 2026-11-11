'use strict';
const {chromium}=require(process.env.SCOUT_PLAYWRIGHT_MODULE||'playwright');
const readline=require('node:readline');
// Every command is bounded: Python kills the worker after 30s, so the worker answers within COMMAND_BUDGET.
const COMMAND_BUDGET=24000,EVAL_LIMIT=9000,PROBE_LIMIT=2500,POLL_LIMIT=6000,GOTO_LIMIT=15000,WEAK_TEXT=150,IMAGE_CAP=3*1024*1024;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function bounded(promise,ms,message){let timer;promise.catch(()=>{});return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Object.assign(Error(message),{bounded:true})),Math.max(200,ms))})]).finally(()=>clearTimeout(timer))}
// Moli does not paint drawImage onto canvas, so remember what each canvas drew; decode_qr downloads it through the safe Python path.
const RECORD_CANVAS_SOURCES=()=>{try{const proto=window.CanvasRenderingContext2D&&CanvasRenderingContext2D.prototype;if(!proto)return;const draw=proto.drawImage;proto.drawImage=function(source,...rest){try{const src=source&&(source.currentSrc||source.src);if(typeof src==='string'&&/^(https?:|data:)/i.test(src)&&src.length<4500000&&this.canvas){const list=JSON.parse(this.canvas.getAttribute('data-scout-draw')||'[]');if(!list.includes(src)&&list.length<4){list.push(src);this.canvas.setAttribute('data-scout-draw',JSON.stringify(list))}}}catch(e){}return draw.call(this,source,...rest)}}catch(e){}};
// Chrome fallback only: no WebRTC, so a page cannot reach hosts over UDP around the proxy.
const NO_WEBRTC=()=>{for(const name of ['RTCPeerConnection','webkitRTCPeerConnection','RTCDataChannel','RTCIceCandidate','RTCSessionDescription'])try{delete window[name];Object.defineProperty(window,name,{value:undefined,configurable:false,writable:false})}catch(e){}};
// The Python GuardProxy is the SSRF filter; these flags only stop Chrome from resolving or sending anything outside it.
// No --disable-features here: Chrome honours only the last one, which would drop Playwright's own list (HttpsUpgrades, AvoidCorsURLLoaderRestartOnRedirect, ...).
const CHROME_ARGS=['--disable-dev-shm-usage','--disable-gpu','--disable-extensions','--mute-audio','--no-first-run','--renderer-process-limit=2','--js-flags=--max-old-space-size=256','--disable-site-isolation-trials','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1','--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp','--webrtc-ip-handling-policy=disable_non_proxied_udp','--disable-background-networking','--disable-component-update','--dns-prefetch-disable','--no-pings'];
async function start(mode,target){
 if(mode==='cdp'){const browser=await chromium.connectOverCDP(target);return {browser,context:browser.contexts()[0]||await browser.newContext()}}
 if(mode!=='launch'||!/^\d+$/.test(target||''))throw Error('浏览器启动参数无效');
 // Playwright adds --proxy-bypass-list=<-loopback>, so loopback also goes through the proxy unless this override is set; never honour it.
 delete process.env.PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK;
 const browser=await chromium.launch({headless:true,executablePath:process.env.SCOUT_CHROME_BIN||undefined,proxy:{server:'http://127.0.0.1:'+target},args:CHROME_ARGS,timeout:20000});
 const context=await browser.newContext({serviceWorkers:'block',acceptDownloads:false,viewport:{width:1280,height:900}});
 await context.addInitScript(NO_WEBRTC);
 await context.route('**/*',route=>{const request=route.request();let protocol='';try{protocol=new URL(request.url()).protocol}catch(e){}
  if(!['http:','https:','data:','blob:'].includes(protocol)||['font','media'].includes(request.resourceType()))return route.abort('blockedbyclient').catch(()=>{});return route.continue().catch(()=>{})});
 return {browser,context};
}
(async()=>{
 const {browser,context}=await start(process.argv[2],process.argv[3]);
 // Moli serve or the Chrome main process died (OOM, crash): say so and exit, so Python raises BrowserDeadError instead of burning model steps.
 let closing=false;browser.on('disconnected',()=>{if(closing)return;console.log(JSON.stringify({ok:false,dead:true,error:'浏览器进程已退出'}));process.exit(1)});
 await context.addInitScript(RECORD_CANVAS_SOURCES).catch(()=>{});
 let page=context.pages()[0];
 if(!page||page.isClosed()||!await bounded(page.evaluate(()=>1),3000,'页面无响应').then(()=>true,()=>false))page=await context.newPage();
 page.setDefaultTimeout(10000);
 const lastSnapshot=new WeakMap();
 async function snapshot(deadline){
  // A trivial probe first: a page stuck in a script loop answers nothing, so return quickly instead of waiting for Moli's 10s watchdog.
  try{await bounded(page.evaluate(()=>1),Math.min(PROBE_LIMIT,deadline-Date.now()),'页面脚本无响应');
   const current=await bounded(page.evaluate(()=>{
   const textOf=e=>{if(!e)return '';const copy=e.cloneNode(true);copy.querySelectorAll('script,style,noscript,svg').forEach(n=>n.remove());copy.querySelectorAll('p,div,li,tr,h1,h2,h3,h4,section').forEach(n=>n.append('\n'));return (copy.textContent||'').replace(/[ \t]+/g,' ').replace(/\n\s*\n/g,'\n').trim()};
   const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
   document.querySelectorAll('[data-scout-ref],[data-scout-img]').forEach(e=>{e.removeAttribute('data-scout-ref');e.removeAttribute('data-scout-img')});
   const full=textOf(document.body);const nodes=Array.from(document.querySelectorAll('a,button,input,[role="button"],[role="tab"],summary')).slice(0,400);
   const implicit=e=>e.getAttribute('role')||({A:e.hasAttribute('href')?'link':null,BUTTON:'button',SUMMARY:'button',INPUT:['button','submit','reset','image'].includes(e.type)?'button':['checkbox','radio'].includes(e.type)?e.type:e.type==='search'?'searchbox':'textbox'})[e.tagName]||null;
   const controls=nodes.map((e,i)=>{e.setAttribute('data-scout-ref','c'+i);const label=(e.textContent||'').trim().slice(0,150);const item={ref:'c'+i,tag:e.tagName,role:implicit(e),label,aria:e.getAttribute('aria-label'),title:e.getAttribute('title'),href:e.getAttribute('href'),type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder'),icon:e.querySelector('svg')?.getAttribute('class'),context:textOf(e.parentElement).slice(0,250)};if(!label){const name=(e.getAttribute('aria-label')||e.getAttribute('title')||e.getAttribute('alt')||e.getAttribute('placeholder')||e.value||e.querySelector('img[alt]')?.getAttribute('alt')||'').trim().slice(0,150);if(name)item.name=name}return item});
   const found=[];
   for(const e of Array.from(document.images).slice(0,120))found.push([e,'img',e.currentSrc||e.src||'']);
   for(const e of Array.from(document.querySelectorAll('canvas')).slice(0,40)){if(e.width>=40&&e.height>=40&&found.filter(f=>f[1]==='canvas').length<20)found.push([e,'canvas',''])}
   let backgrounds=0;
   for(const e of Array.from(document.querySelectorAll('body *')).slice(0,4000)){
    if(backgrounds>=30)break;const match=/url\(\s*["']?([^"')]+)["']?\s*\)/.exec(getComputedStyle(e).backgroundImage||'');if(!match)continue;
    const box=e.getBoundingClientRect();if(box.width<40||box.height<40||!visible(e))continue;
    let src=match[1];try{src=new URL(src,document.baseURI).href}catch(x){}found.push([e,'background',src]);backgrounds++;
   }
   const images=found.slice(0,160).map(([e,kind,src],i)=>{e.setAttribute('data-scout-img','i'+i);return {ref:'i'+i,kind,src,alt:e.getAttribute('alt')||e.getAttribute('aria-label')||e.getAttribute('title')||'',context:textOf(e.parentElement).slice(0,250),tag:e.tagName}});
   // Specific dialog selectors win over heuristic class matches; body/html (Element UI puts el-popup-parent--hidden on <body>) and page-level wrappers are never dialogs.
   const pageHeight=document.documentElement.scrollHeight;const candidate=e=>e!==document.body&&e!==document.documentElement&&visible(e);
   const specific=Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"],[aria-modal="true"],.modal,.el-dialog,.el-message-box,.ant-modal,.van-dialog,.layui-layer')).slice(0,200).filter(candidate);
   const broad=Array.from(document.querySelectorAll('[role="tabpanel"],[class*="popup"],[class*="announcement"]')).slice(0,200).filter(e=>candidate(e)&&!specific.includes(e)&&!specific.some(s=>e.contains(s))&&!(pageHeight>innerHeight+200&&e.scrollHeight>=pageHeight*.9));
   const dialogNodes=specific.concat(broad);
   const dialogs=dialogNodes.filter(e=>!dialogNodes.some(other=>other!==e&&other.contains(e))).map(e=>textOf(e).slice(0,9000)).filter(Boolean).slice(0,8);
   return {url:location.href,title:document.title,text:full.slice(0,18000),tail:full.length>18000?full.slice(-10000):'',footer:textOf(document.querySelector('footer')).slice(0,10000),dialogs,contact_hints:full.split('\n').filter(s=>/@|微信|QQ|联系|客服|商务|Telegram|Discord|[Cc]ontact|[Ss]ales|[Ss]upport/.test(s)).slice(0,60).join('\n').slice(0,8000),controls,images};
  }),Math.min(EVAL_LIMIT,deadline-Date.now()),'页面快照超时');
   lastSnapshot.set(page,current);return current;
  }catch(error){
   if(!error.bounded)throw error;
   // Hung or very slow page: keep the run going with the previous observation of this URL, flagged so run_agent can warn.
   const previous=lastSnapshot.get(page);const url=page.url();
   return Object.assign(previous&&previous.url===url?{...previous}:{url,title:'',text:'',tail:'',footer:'',dialogs:[],contact_hints:'',controls:[],images:[]},{page_unresponsive:true,render_weak:true,unresponsive_reason:error.message});
  }
 }
 const element=(attribute,ref)=>page.locator('['+attribute+'="'+ref+'"]');
 console.log(JSON.stringify({ok:true,ready:true}));
 for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
  const deadline=Date.now()+COMMAND_BUDGET;const left=(limit=EVAL_LIMIT)=>Math.min(limit,deadline-Date.now());
  try{
   const cmd=JSON.parse(line);let gotoTimedOut=false;
   if(cmd.type==='open'){
    try{await bounded(page.goto(cmd.url,{waitUntil:'domcontentloaded',timeout:GOTO_LIMIT}),GOTO_LIMIT+1000,'页面加载超时')}
    catch(error){if(error.bounded||error.name==='TimeoutError')gotoTimedOut=true;else throw error}
   }else if(cmd.type==='click'){
    if(!/^c\d+$/.test(cmd.ref))throw Error('控件编号无效');
    const el=element('data-scout-ref',cmd.ref);
    const info=await bounded(el.evaluate(e=>({tag:e.tagName,type:e.type,label:(e.textContent||'')+' '+(e.getAttribute('aria-label')||''),href:e.getAttribute('href')})),left(),'页面脚本无响应，无法读取控件');
    if(info.type==='submit'||/确认支付|立即支付|确认充值|提交订单|发送消息|发送邮件|删除|注销|退出登录|登录|注册|sign in|log in|sign up/i.test(info.label))throw Error('该操作超出只读采集范围');
    if(info.href&&!['http:','https:'].includes(new URL(info.href,page.url()).protocol))throw Error('只记录该联系链接，不启动外部应用');
    const before=context.pages();
    await bounded(el.evaluate(e=>e.click()),left(),'页面脚本无响应，点击未完成');
    await sleep(300);
    const opened=context.pages().filter(candidate=>!before.includes(candidate));
    if(opened.length){page=opened[opened.length-1];page.setDefaultTimeout(10000)}
   }else if(cmd.type==='search'){
    if(!/^c\d+$/.test(cmd.ref))throw Error('控件编号无效');
    await bounded(element('data-scout-ref',cmd.ref).evaluate((e,value)=>{
     if(e.tagName!=='INPUT'||!['text','search'].includes(e.type)||!/搜索|检索|查找|search|filter|model/i.test([e.placeholder,e.getAttribute('aria-label'),e.name].join(' ')))throw Error('仅允许填写站内搜索框');
     Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
    },String(cmd.text||'').slice(0,150)),left(),'页面脚本无响应，搜索未完成');
   }else if(cmd.type==='scroll')await bounded(page.evaluate(d=>window.scrollBy(0,d==='up'?-700:700),cmd.direction),left(),'页面脚本无响应，滚动未完成');
   else if(cmd.type==='screenshot'){
    if(!/^i\d+$/.test(cmd.ref))throw Error('图片编号无效');
    await bounded(element('data-scout-img',cmd.ref).screenshot({path:cmd.path,timeout:left(10000)}),left(10000)+500,'图片截图超时');
    console.log(JSON.stringify({ok:true,image_path:cmd.path}));continue;
   }else if(cmd.type==='image_data'){
    // Read-only: canvas pixels, data: URIs and same-origin/CORS bytes. Background URLs are returned for Python's validated download.
    if(!/^i\d+$/.test(cmd.ref))throw Error('图片编号无效');
    const data=await bounded(element('data-scout-img',cmd.ref).evaluate(async(e,cap)=>{
     const encode=buffer=>{const bytes=new Uint8Array(buffer);let text='';for(let i=0;i<bytes.length;i+=32768)text+=String.fromCharCode.apply(null,bytes.subarray(i,i+32768));return btoa(text)};
     if(e.tagName==='CANVAS'){let uri='';try{uri=e.toDataURL('image/png')}catch(x){}return {kind:'canvas',data_uri:uri.length<=cap*4/3+100?uri:'',sources:(()=>{const raw=e.getAttribute('data-scout-draw')||'';if(!raw||raw.length>4*4600000)return [];try{const list=JSON.parse(raw);return Array.isArray(list)?list.filter(s=>typeof s==='string'&&s.length<4500000).slice(0,4):[]}catch(x){return []}})()}}
     if(e.tagName==='IMG'){
      const src=e.currentSrc||e.src||'';if(/^data:/i.test(src))return {kind:'img',data_uri:src.length<=cap*4/3+100?src:''};
      if(!/^https?:/i.test(src))return {kind:'img'};
      try{const response=await fetch(src,{credentials:'omit'});if(!response.ok||Number(response.headers.get('content-length')||0)>cap)return {kind:'img',url:src};const reader=response.body.getReader(),chunks=[];let size=0;for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>cap){reader.cancel().catch(()=>{});return {kind:'img',url:src}}chunks.push(value)}const buffer=new Uint8Array(size);let offset=0;for(const chunk of chunks){buffer.set(chunk,offset);offset+=chunk.byteLength}return {kind:'img',url:src,data_uri:'data:'+(response.headers.get('content-type')||'application/octet-stream').split(';')[0]+';base64,'+encode(buffer)}}
      catch(x){return {kind:'img',url:src}}
     }
     const match=/url\(\s*["']?([^"')]+)["']?\s*\)/.exec(getComputedStyle(e).backgroundImage||'');let url='';if(match){try{url=new URL(match[1],document.baseURI).href}catch(x){}}
     return /^data:/i.test(url)?{kind:'background',data_uri:url.length<=cap*4/3+100?url:''}:{kind:'background',url};
    },IMAGE_CAP),left(),'页面脚本无响应，无法读取图片');
    console.log(JSON.stringify({ok:true,image:data}));continue;
   }else if(!['snapshot','wait'].includes(cmd.type))throw Error('不支持的浏览器动作');
   await sleep(cmd.type==='wait'?1800:650);
   let current=await snapshot(deadline);
   // Slow single-page apps: poll while the text is still short and changing, within the command budget.
   for(let previous=-1,started=Date.now(),stop=started+POLL_LIMIT;!current.page_unresponsive&&current.text.length<WEAK_TEXT&&!(current.text.length>0&&current.text.length===previous&&Date.now()-started>=1800)&&Date.now()<stop&&deadline-Date.now()>EVAL_LIMIT/3+1200;){previous=current.text.length;await sleep(1200);current=await snapshot(deadline)}
   if(gotoTimedOut||current.text.length<WEAK_TEXT)current=Object.assign(current,{render_weak:true},gotoTimedOut?{goto_timeout:true}:{});
   console.log(JSON.stringify({ok:true,snapshot:current}));
  }catch(e){console.log(JSON.stringify({ok:false,error:String(e.message).slice(0,600)}))}
 }
 closing=true;await browser.close();
})().catch(error=>{console.log(JSON.stringify({ok:false,error:error.message}));process.exitCode=1});
