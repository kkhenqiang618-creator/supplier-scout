const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'..','static',name),'utf8');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{id,innerHTML:'',textContent:'',open:false,classList:{add(){},remove(){}},addEventListener(){},close(){this.open=false},showModal(){this.open=true}});return nodes.get(id)};
const state={deployment:'local',suppliers:[],rules:[],events:[],history:[],drafts:[],job:{status:'idle',running:false,mode:'real',total:0,done:0},model_service:{}};
const context=vm.createContext({URLSearchParams,URL,console,document:{querySelector:node,addEventListener(){},activeElement:{}},window:{scrollTo(){}},location:{search:'',pathname:'/',hostname:'127.0.0.1',assign(){}},localStorage:{getItem(){return null},setItem(){}},setTimeout(){},clearTimeout(){},setInterval(){},fetch:async()=>({status:200,ok:true,json:async()=>state})});
vm.runInContext(read('vendor/qrcode.js'),context);vm.runInContext(read('app.js'),context);
const {qrSvg,qrEligible,qrButton,qrModalHTML,contactBadge,qrImageOnly,qrImageModalHTML}=context;
const wecom='https://work.weixin.qq.com/ca/cawcde1234567890abcdef?customer_channel=site&x=<b>';
const ok=qrSvg(wecom);assert.equal(ok.ok,true);assert.match(ok.svg,/^<svg[^>]*viewBox="0 0 \d+ \d+"/);assert.match(ok.svg,/<path d="M/);assert(!ok.svg.includes('<b>'));
for(const bad of ['javascript:alert(1)','JavaScript://%0aalert(1)','data:text/html,<script>x</script>','vbscript:x','  javascript:https://a.com','ftp://a.com/x','https://a.com/x y'])assert.equal(qrSvg(bad).ok,false,bad);
const long=qrSvg('https://t.me/'+'a'.repeat(1100));assert.equal(long.ok,false);assert.match(long.message,/过长/);
assert.equal(qrEligible({type:'wechat_link',value:wecom}),true);assert.equal(qrEligible({type:'email',value:'https://a.com'}),false);assert.equal(qrEligible({type:'telegram',value:'@abc'}),false);assert.equal(qrEligible({type:'qq_group',value:'javascript:alert(1)'}),false);
assert.match(qrButton({type:'discord',value:'https://discord.gg/abc"onmouseover="x'}),/data-qr="https:\/\/discord\.gg\/abc&quot;onmouseover=&quot;x"/);
const html=qrModalHTML({type:'wechat_link',value:wecom,purpose:'<i>商务</i>',source:'https://example.com/qr.png',risks:'历史线索'});
assert(html.includes('<svg')&&html.includes('不经过任何外部服务')&&html.includes('查看二维码来源')&&html.includes('&lt;i&gt;商务')&&!html.includes('<i>商务')&&html.includes('历史线索')&&html.includes('&lt;b&gt;'));
const unsafe=qrModalHTML({type:'qrcode',value:'https://a.com/'+'b'.repeat(1200),source:'javascript:alert(1)'});
assert(unsafe.includes('qr-fallback')&&!unsafe.includes('<svg version')&&!unsafe.includes('javascript:'));
context.qrcode=()=>{throw new Error('code length overflow')};assert.equal(qrSvg('https://a.com').ok,false);
for(const [key,label] of [['not_started','未开始'],['legacy','旧线索待核验'],['incomplete','已查，暂未找到'],['blocked','访问受阻']]){const badge=contactBadge(key);assert(badge.includes('>'+label+'<')&&/title="[^"]{6,}"/.test(badge)&&/aria-label="[^"]{6,}"/.test(badge),key)}
const qrOnly={contact_records:[{type:'qrcode',value:'https://a.com/qr.png'}]};assert(contactBadge('needs_review',qrOnly).includes('>二维码待人工识别<'));
for(const result of [undefined,{},{contact_summary:'Cloudflare 拦截',contact_records:[]},{contact_records:[{type:'qrcode',value:'x'},{type:'email',value:'a@b.co'}]}]){const badge=contactBadge('needs_review',result);assert(!badge.includes('二维码')&&badge.includes('>已查，暂未找到（保留旧线索）<')&&/title="[^"]{6,}"/.test(badge),JSON.stringify(result))}
const stageOf=(status,contacts={})=>context.followupStages({contact_status:status,contacts,offers:[],status:'pending',reason:''}).find(stage=>stage.key==='todo_contact');
assert.equal(stageOf('not_started').label,'未开始 · 待采集');assert.equal(stageOf('incomplete').label,'暂未找到 · 待继续查找');assert.equal(stageOf('blocked').label,'访问受阻 · 需人工或稍后重试');assert.equal(stageOf('not_started',{other:['x']}).label,'联系方式待核验');
const css=read('style.css');assert(/\.qr-code svg\{[^}]*stroke:none/.test(css)&&/\.qr-code svg \*\{stroke:none\}/.test(css));
// Undecoded QR images: never re-encode the image URL as a scannable "add contact" code.
for(const item of [{type:'qrcode',value:'https://a.com/contact-qr.png'},{type:'wechat_link',value:'https://cdn.a.com/wecom.JPG?v=2'},{type:'other',value:'https://a.com/img/qr.webp#x'}]){
 assert.equal(qrImageOnly(item),true,item.value);assert.equal(qrEligible(item),false,item.value);
 const btn=qrButton(item);assert(btn.includes('data-qr-image=')&&!btn.includes(' data-qr="'),item.value);
 const modal=qrImageModalHTML({...item,source:'https://a.com/contact'});assert(modal.includes('二维码待人工识别')&&modal.includes('没有被成功解码')&&!modal.includes('class="qr-code"')&&!modal.includes('扫码添加联系方式'),item.value)}
assert.equal(qrImageOnly({type:'wechat_link',value:wecom}),false);assert.equal(qrEligible({type:'wechat_link',value:wecom}),true);
assert(qrButton({type:'qq_group',value:'https://qm.qq.com/q/abc'}).includes(' data-qr="'));
{const bad=qrImageModalHTML({type:'qrcode',value:'javascript:alert(1)',source:'javascript:x'});assert(!/href="javascript:/i.test(bad)&&!bad.includes('打开原二维码图片')&&!bad.includes('查看图片所在页面'))}
console.log('contact qr ok');
