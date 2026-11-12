const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(process.env.SCOUT_TEST_APP_SOURCE||path.join(__dirname,'../static/app.js'),'utf8');
async function run(){
 const nodes=new Map(),listeners=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'',dataset:{},classList:{add(){},remove(){}},addEventListener(){}});return nodes.get(id)};
 let model={base_url:'https://provider.example/v1',model:'m',configured:true,key_configured:true,tested:true};
 const base={deployment:'cloud',username:'fixture',suppliers:[],rules:[],events:[],history:[],drafts:[],job:{running:false}};
 const context=vm.createContext({URLSearchParams,URL,console,document:{querySelector:node,addEventListener(type,fn){if(type==='click')listeners.push(fn)},activeElement:{}},window:{scrollTo(){}},location:{search:'?mode=real&page=integrations',pathname:'/',hostname:'example.com'},localStorage:{getItem(){return null},setItem(){}},setTimeout(){},clearTimeout(){},setInterval(){},fetch:async url=>{
  if(url.endsWith('/model-service/test')){model={...model,tested:false,last_test_status:'failed',last_test_at:'2026-11-13T08:00:00Z'};return {ok:false,status:400,json:async()=>({error:'连接超时'})};}
  return {ok:true,status:200,json:async()=>url.endsWith('/state')?{...base,model_service:model}:{ok:true}};
 }});
 vm.runInContext(source,context);await new Promise(setImmediate);await new Promise(setImmediate);
 const button={dataset:{action:'test-service'},disabled:false};
 for(const listener of listeners)await listener({target:{closest:()=>button}});
 assert.match(node('#main').innerHTML,/上次测试失败/);
 assert(!node('#main').innerHTML.includes('上次测试通过'));
 assert.match(node('#main').innerHTML,/最近测试/);
 assert.equal(node('#service-error').textContent,'连接超时');
 console.log('PASS: failed connection test refreshes historical success and preserves visible error/time');
}
run().catch(error=>{console.error(error);process.exitCode=1});
