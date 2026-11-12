const test=require('node:test'),assert=require('node:assert/strict');
const W=require('../static/workspace.js');
const rows=[{id:4,name:'007',url:'https://a.example',mode:'real',status:'review',contacts:{email:['sales@a.example']},offers:[]},{id:9,name:'真实公司',url:'https://b.example',mode:'real',status:'skipped',reason:'人工复核：暂不跟进',contacts:{email:['biz@b.example']},offers:[]}];
test('numeric import identifiers appear once and optional real names survive',()=>{
 assert.deepEqual(W.identity(rows[0]),{number:'007',name:'',host:'a.example'});
 assert.deepEqual(W.identity(rows[1]),{number:'9',name:'真实公司',host:'b.example'});
});
test('emails awaiting quote review are eligible; explicitly skipped remain opt-in',()=>{
 assert.equal(W.emailEligible(rows[0]),true);assert.equal(W.emailEligible(rows[1]),false);assert.equal(W.emailEligible(rows[1],true),true);
 assert.equal(W.emailEligible({...rows[0],contacts:{email:['not-email']}}),false);
});
test('saved contact progress determines placement independently from price status',()=>{
 assert.equal(W.inFollowup(rows[0],{}),false);assert.equal(W.inFollowup(rows[0],{'4':{status:'waiting'}}),true);
 assert.equal(W.inFollowup(rows[0],{'9':{status:'done'}}),false);
});
const offer=(ratio,over={})=>({model:'m',model_name:'M',ratio,basis:'published_discount',currency:'CNY',unit:'1M tokens',group:'default',resource_form:'自部署',...over});
test('analytics counts offers beyond cloud summary limit and bins boundaries exactly once',()=>{
 const data={rules:[{model:'m',label:'M'}],suppliers:[{id:1,offers:[offer(.2)]},{id:2,offers:[offer(.3)]},{id:3,offers:[offer(.4)]},{id:4,offers:[offer(.5)]},{id:5,offers:[offer(.6)]},{id:6,offers:[offer(null)]}]};
 const got=W.distribution(data,{model:'m',resource:'self',currency:'CNY',unit:'1M tokens',group:'default',price:'input_price'});
 assert.deepEqual(got.bins,[1,1,1,1,1]);assert.equal(got.comparable,5);assert.equal(got.unknown,1);
});
test('same supplier is counted once, with ambiguous groups excluded rather than choosing cheapest',()=>{
 const data={rules:[],suppliers:[{id:1,offers:[offer(.4),offer(.4)]},{id:2,offers:[offer(.3),offer(.6)]},{id:3,offers:[offer(.2,{currency:'USD'})]}]};
 const got=W.distribution(data,{model:'m',resource:'self',currency:'CNY',unit:'1M tokens',group:'default',price:'input_price'});
 assert.deepEqual(got.bins,[0,0,1,0,0]);assert.equal(got.unknown,1);assert.equal(got.comparable,1);
});
test('multipliers and input-only ratios cannot become output discounts; missing resource is not self-hosted',()=>{
 const data={rules:[],suppliers:[{id:1,offers:[offer(.3,{basis:'group_multiplier'})]},{id:2,offers:[offer(.4,{basis:'official_price',input_price:4})]},{id:3,offers:[offer(.3,{resource_form:'待确认'})]},{id:4,offers:[offer(.5,{basis:'published_discount',ratio_scope:'input'})]}]};
 const got=W.distribution(data,{model:'m',resource:'self',currency:'CNY',unit:'1M tokens',group:'default',price:'output_price'});
 assert.equal(got.comparable,0);assert.equal(got.unknown,3);
});
test('all configured models plus actual observed variants appear without duplicating canonical labels',()=>{
 const got=W.models({rules:[{model:'m',label:'M'},{model:'empty',label:'Empty'}],suppliers:[{offers:[offer(.4),offer(.3,{model_name:'M Pro'}),offer(.5,{model:'new',model_name:'New'})]}]});
 assert(got.some(m=>m.value==='empty'));assert(got.some(m=>m.label==='M Pro'));assert(got.some(m=>m.label==='New'));assert.equal(got.filter(m=>m.label==='M').length,1);
});
test('range prices and explicitly uncertain conditions cannot imply a verified discount',()=>{
 const rule={model:'m',reference_input:10,currency:'CNY',unit:'1M tokens'};
 assert.equal(W.discount(offer(null,{input_price:2,price_text:'最低2美元起，具体条件需询价',notes:'条件未确认'}),rule),null);
});
test('contradictory published discount and price-derived discount stay for review',()=>{
 assert.equal(W.discount(offer(.4,{input_price:10}),{model:'m',reference_input:10,currency:'CNY',unit:'1M tokens'}),null);
});
test('canonical rules do not mix actual versions or lend their baseline to a distinct variant',()=>{
 const quote=offer(null,{model:'qwen',model_name:'Qwen3.8-Max',input_price:2});
 assert.equal(W.matching(quote,{model:'qwen'}),false);
 assert.equal(W.discount(quote,{model:'qwen',reference_input:10,currency:'CNY',unit:'1M tokens'}),null);
 assert.equal(W.matching(quote,{model:'actual:'+JSON.stringify(['qwen','Qwen3.8-Max'])}),true);
});
test('unknown resource or missing billing units remain unverified even with a published ratio',()=>{
 assert.equal(W.discount(offer(.4,{resource_form:'待确认'}),{}),null);
 assert.equal(W.discount(offer(.4,{unit:null}),{}),null);
});
test('contact collection reasons distinguish old errors and preserve found channels',()=>{
 const old={url:'https://a.example',contact_status:'blocked',contact_result:{warnings:['域名无法解析']}};
 assert.match(W.collectionReason(old),/域名/);
 const fresh={...old,contact_result:{diagnostics:[{code:'http',label:'网站返回 HTTP 404',url:'https://a.example/docs',detail:'404'}]}};
 assert.match(W.collectionReason(fresh),/HTTP 404/);
 assert.match(W.collectionReason({...old,contact_status:'found',contact_result:{contact_summary:'找到作者渠道'}}),/找到作者渠道/);
});
test('recovered entry summary retains original and current addresses',()=>{
 const row={url:'https://new.example/',contact_result:{entry_history:[{from:'https://old.example/',to:'https://new.example/',method:'manual'}]}};
 assert.match(W.entrySummary(row),/old\.example/);assert.match(W.entrySummary(row),/new\.example/);
 assert.equal(W.entrySummary({url:'https://unchanged.example'}),'');
});
