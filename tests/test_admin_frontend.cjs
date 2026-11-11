const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const app=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),admin=fs.readFileSync(path.join(__dirname,'../static/admin.js'),'utf8');
async function run(source){
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{id,innerHTML:'',open:false,classList:{add(){},remove(){}},addEventListener(){},close(){this.open=false},showModal(){this.open=true}});return nodes.get(id)};
 let member=false,poll;
 const base={deployment:'cloud',auth_enabled:true,username:'小姜',is_admin:true,suppliers:[],rules:[],events:[],history:[],drafts:[],job:{status:'idle',running:false,mode:'real',total:0,done:0},model_service:{}};
 const context=vm.createContext({URLSearchParams,URL,console,document:{querySelector:node,addEventListener(){},activeElement:{}},window:{scrollTo(){}},location:{search:'?mode=real&page=team',pathname:'/',hostname:'example.com',assign(){}},localStorage:{getItem(){return null},setItem(){}},setTimeout(){},clearTimeout(){},setInterval(fn){poll=fn},fetch:async url=>({status:200,ok:true,json:async()=>url.includes('admin/overview')?{members:[{username:'PRIVATE_MEMBER_LABEL',suppliers:3,contacts_found:1,qualified:0,drafts:0,jobs:[]}]}:{...base,...(member?{username:'fo',is_admin:false}:{})}})});
 vm.runInContext(source,context);vm.runInContext(admin,context);await new Promise(setImmediate);await new Promise(setImmediate);
 assert(node('#sidebar').innerHTML.includes('data-nav="team"'));assert(node('#main').innerHTML.includes('PRIVATE_MEMBER_LABEL'));
 member=true;node('#modal').open=true;node('#drawer').open=true;await poll();
 assert(!node('#main').innerHTML.includes('PRIVATE_MEMBER_LABEL'),'account switch must remove cached admin data');assert(!node('#sidebar').innerHTML.includes('data-nav="team"'));assert(!node('#modal').open&&!node('#drawer').open);
}
(async()=>{
 const fixed="const newState=await api('state');const wasBusy=state.job.running,identityChanged=newState.username!==state.username;state=newState;if(identityChanged){detailRequest++;contactEvidenceRequest++;drawerId=null;if($('#modal').open)$('#modal').close();if($('#drawer').open)$('#drawer').close();if(typeof teamRequest!=='undefined'){teamRequest++;teamOverview=null;teamMember=null;teamBusy=false;teamError='';teamSelected=''}if(!state.is_admin&&page==='team')page='dashboard';renderApp();return}";
 const old=app.replace(fixed,"const newState=await api('state');const wasBusy=state.job.running;state=newState;");assert.notEqual(old,app);
 await assert.rejects(run(old),/account switch must remove cached admin data/);await run(app);console.log('PASS: administrator UI visible only to admin; identity change clears cached team data and dialogs; old behavior reproduced');
})().catch(e=>{console.error(e);process.exit(1)});
