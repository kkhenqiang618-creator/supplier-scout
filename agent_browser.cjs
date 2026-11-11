'use strict';
const {chromium}=require(process.env.SCOUT_PLAYWRIGHT_MODULE||'playwright');
const readline=require('node:readline');
(async()=>{
 const browser=await chromium.connectOverCDP(process.argv[2]);
 const context=browser.contexts()[0];let page=context.pages()[0]||await context.newPage();
 page.setDefaultTimeout(10000);
 async function snapshot(){return page.evaluate(()=>{
   const textOf=e=>{if(!e)return '';const copy=e.cloneNode(true);copy.querySelectorAll('script,style,noscript,svg').forEach(n=>n.remove());copy.querySelectorAll('p,div,li,tr,h1,h2,h3,h4,section').forEach(n=>n.append('\n'));return (copy.textContent||'').replace(/[ \t]+/g,' ').replace(/\n\s*\n/g,'\n').trim()};
   const full=textOf(document.body);const nodes=Array.from(document.querySelectorAll('a,button,input,[role="button"],[role="tab"],summary')).slice(0,400);
   const controls=nodes.map((e,i)=>{e.setAttribute('data-scout-ref','c'+i);return {ref:'c'+i,tag:e.tagName,role:e.getAttribute('role'),label:(e.textContent||'').trim().slice(0,150),aria:e.getAttribute('aria-label'),title:e.getAttribute('title'),href:e.getAttribute('href'),type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder'),icon:e.querySelector('svg')?.getAttribute('class'),context:textOf(e.parentElement).slice(0,250)}});
   const images=Array.from(document.querySelectorAll('img,canvas')).slice(0,160).map((e,i)=>{e.setAttribute('data-scout-ref','i'+i);return {ref:'i'+i,src:e.src||'',alt:e.alt||'',context:textOf(e.parentElement).slice(0,250),tag:e.tagName}});
   return {url:location.href,title:document.title,text:full.slice(0,18000),tail:full.length>18000?full.slice(-10000):'',footer:textOf(document.querySelector('footer')).slice(0,10000),dialogs:Array.from(document.querySelectorAll('[role="dialog"],[role="tabpanel"]')).map(e=>textOf(e).slice(0,9000)).slice(0,8),contact_hints:full.split('\n').filter(s=>/@|微信|QQ|联系|客服|商务|Telegram|Discord|[Cc]ontact|[Ss]ales|[Ss]upport/.test(s)).slice(0,60).join('\n').slice(0,8000),controls,images};
 })}
 for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
  try{
   const cmd=JSON.parse(line);
   if(cmd.type==='open')await page.goto(cmd.url,{waitUntil:'domcontentloaded',timeout:22000});
   else if(cmd.type==='click'){
    if(!/^c\d+$/.test(cmd.ref))throw Error('控件编号无效');
    const el=page.locator('[data-scout-ref="'+cmd.ref+'"]');
    const info=await el.evaluate(e=>({tag:e.tagName,type:e.type,label:(e.textContent||'')+' '+(e.getAttribute('aria-label')||''),href:e.getAttribute('href')}));
    if(info.type==='submit'||/确认支付|立即支付|确认充值|提交订单|发送消息|发送邮件|删除|注销|退出登录|登录|注册|sign in|log in|sign up/i.test(info.label))throw Error('该操作超出只读采集范围');
    if(info.href&&!['http:','https:'].includes(new URL(info.href,page.url()).protocol))throw Error('只记录该联系链接，不启动外部应用');
    const before=context.pages();
    await el.evaluate(e=>e.click());
    await page.waitForTimeout(300);
    const opened=context.pages().filter(candidate=>!before.includes(candidate));
    if(opened.length){page=opened[opened.length-1];page.setDefaultTimeout(10000)}
   }else if(cmd.type==='search'){
    if(!/^c\d+$/.test(cmd.ref))throw Error('控件编号无效');
    await page.locator('[data-scout-ref="'+cmd.ref+'"]').evaluate((e,value)=>{
     if(e.tagName!=='INPUT'||!['text','search'].includes(e.type)||!/搜索|检索|查找|search|filter|model/i.test([e.placeholder,e.getAttribute('aria-label'),e.name].join(' ')))throw Error('仅允许填写站内搜索框');
     Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
    },String(cmd.text||'').slice(0,150));
   }else if(cmd.type==='scroll')await page.evaluate(d=>window.scrollBy(0,d==='up'?-700:700),cmd.direction);
   else if(cmd.type==='screenshot'){
    if(!/^i\d+$/.test(cmd.ref))throw Error('图片编号无效');
    await page.locator('[data-scout-ref="'+cmd.ref+'"]').screenshot({path:cmd.path,timeout:10000});
    console.log(JSON.stringify({ok:true,image_path:cmd.path}));continue;
   }else if(!['snapshot','wait'].includes(cmd.type))throw Error('不支持的浏览器动作');
   await page.waitForTimeout(cmd.type==='wait'?1800:650);
   let current=await snapshot();
   if(current.text.length<150){await page.waitForTimeout(1800);current=await snapshot()}
   console.log(JSON.stringify({ok:true,snapshot:current}));
  }catch(e){console.log(JSON.stringify({ok:false,error:String(e.message).slice(0,600)}))}
 }
 await browser.close();
})().catch(error=>{console.log(JSON.stringify({ok:false,error:error.message}));process.exitCode=1});
