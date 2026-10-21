// Offline API key interactions, including new ledger usage. Synthetic fixtures only.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('templates/local_connection.html','utf8');
let document;
class Node{
 constructor(tag='div'){this.tagName=tag;this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.dataset={};this.attrs={};this.listeners={};this.children=[];this.open=false;this.className='';}
 setAttribute(k,v){this.attrs[k]=String(v);} addEventListener(k,v){this.listeners[k]=v;} focus(){document.activeElement=this;} append(...items){for(const n of items)this.children.push(...(n.tagName==='fragment'?n.children:[n]));} replaceChildren(...items){this.children=[];this.append(...items);} closest(){return this;} showModal(){this.open=true;} close(){this.open=false;} querySelectorAll(selector){const all=this.children.flatMap(n=>[n,...n.querySelectorAll('*')]);return selector==='*'?all:all.filter(n=>n.tagName===selector);}
}
const ids=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Node()]));ids.set('connection-logout',new Node());
const examples=['cloud','local'].map(n=>{const node=ids.get('connection-'+n+'-tab');node.dataset.example=n;return node;});
const kinds=['models','chat','images'].map(n=>{const node=ids.get('connection-kind-'+n);node.dataset.kind=n;return node;});
const sections=['overview','keys','examples'].map(n=>{const node=ids.get('connection-section-'+n);node.dataset.section=n;return node;});
document={activeElement:null,visibilityState:'visible',listeners:{},getElementById:id=>ids.get(id),querySelectorAll:s=>s==='[data-section]'?sections:s==='[data-kind]'?kinds:s==='[data-example]'?examples:[],createElement:t=>new Node(t),createDocumentFragment:()=>new Node('fragment'),addEventListener(k,v){this.listeners[k]=v;}};
const timers=new Map();let timerId=0,mode='ok',calls=[],copied=[],keys=[],keyId=0,keyUsage={},usageStatus='ok';
const connection={base_url:'http://127.0.0.1:5005/v1',key_configured:true,key_count:1,default_model:'auto',public_status:'running',public_base_url:'https://synthetic.trycloudflare.com/v1',public_streaming:false,model_catalog:{object:'list',data:[{id:'auto',type:'chat',source:'chatgpt_web',description:'Auto selection'},{id:'gpt-synthetic',type:'chat',source:'chatgpt_web',description:'Dynamic model'},{id:'chatgpt-image-latest',type:'image',source:'chatgpt_web',description:'Web image'},{id:'gpt-image-2',type:'image',source:'codex_images',description:'Default disabled'}]}};
const window={listeners:{},addEventListener(k,v){this.listeners[k]=v;},ConsoleUI:{copyText:async v=>{copied.push(v);return true;},toast(){}},location:{hash:'#examples',assign(v){this.last=v;}}};
const context=vm.createContext({document,window,AbortController,Error,Date,setTimeout(fn,ms){timers.set(++timerId,{fn,ms});return timerId;},clearTimeout(id){timers.delete(id);},fetch:async(path,options={})=>{
 calls.push({path,options});let data={},status=mode==='expired'?401:mode==='failed'?500:200;
 if(path==='/local-console/connection')data=connection;
 else if(path==='/local-console/connection/key')data={api_key:'synthetic-system-secret'};
 else if(path==='/local-console/api-keys'&&!options.method)data={keys,usage:keyUsage,usage_status:usageStatus,usage_days:30,usage_since:'2026-10-21T00:00:00+00:00',system_key:{configured:true,count:1,read_only:true}};
 else if(path==='/local-console/api-keys'&&options.method==='POST'){
  if(mode==='no-key')status=503;
  else{const key={id:'key'+(++keyId),name:JSON.parse(options.body).name,prefix:'sk-c2a-syn',last4:'1234',status:'active',created_at:'2026-10-21T12:00:00+00:00',last_used_at:null,request_count:0};keys=[key,...keys];data={key,api_key:'synthetic-one-time-secret-'+keyId};}
 }else if(path.startsWith('/local-console/api-keys/')){const id=path.split('/').at(-1);const status=options.method==='DELETE'?'revoked':JSON.parse(options.body).status;keys=keys.map(k=>k.id===id?{...k,status}:k);data={key:keys.find(k=>k.id===id)};}
 return{status,ok:status<400,json:async()=>structuredClone(data)};
}});
vm.runInContext(fs.readFileSync('static/local-console/connection.js','utf8'),context);
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
const click=async id=>{const n=typeof id==='string'?ids.get(id):id;n.focus();n.listeners.click();await flush();};
const submit=async name=>{ids.get('connection-key-name').value=name;ids.get('connection-create-form').listeners.submit({preventDefault(){}});await flush();};
const buttons=()=>ids.get('connection-key-rows').querySelectorAll('button');
const flattened=n=>n.textContent+n.children.map(flattened).join(' ');
(async()=>{
 await flush();assert.equal(ids.get('connection-examples-panel').hidden,false);assert.equal(calls.length,2);assert.equal(ids.get('connection-model-rows').children.length,4);assert.equal(ids.get('connection-create-key').disabled,false);assert.equal(ids.get('connection-keys-empty').hidden,false);assert(!calls.some(c=>c.path.endsWith('/key')));
 await click('connection-open-keys');assert.equal(ids.get('connection-keys-panel').hidden,false);assert.equal(ids.get('connection-overview-panel').hidden,true);
 await submit('云端 <img src=x onerror=alert(1)>');assert.equal(ids.get('connection-created-dialog').open,true);assert.equal(ids.get('connection-created-value').value,'synthetic-one-time-secret-1');assert.equal(ids.get('connection-keys-count').textContent,'1');assert(!flattened(ids.get('connection-key-rows')).includes('one-time-secret'));assert(flattened(ids.get('connection-key-rows')).includes('<img src=x'));
 document.visibilityState='hidden';document.listeners.visibilitychange();assert.equal(ids.get('connection-created-value').value,'synthetic-one-time-secret-1');document.visibilityState='visible';const event={preventDefault(){this.prevented=true;}};window.listeners.beforeunload(event);assert(event.prevented);
 const cancel={preventDefault(){this.prevented=true;}};ids.get('connection-created-dialog').listeners.cancel(cancel);assert(cancel.prevented);assert.equal(ids.get('connection-created-dialog').open,true);
 await click('connection-created-copy');assert.equal(copied.at(-1),'synthetic-one-time-secret-1');await click('connection-created-close');assert.equal(ids.get('connection-created-value').value,'');assert.equal(ids.get('connection-created-dialog').open,false);
 keyUsage={key1:{known_cost_requests:1,unknown_cost_requests:2,estimated_cost_usd:0.125,image_requests:3,generated_images:2}};
 await click('connection-keys-refresh');assert(flattened(ids.get('connection-key-rows')).includes('$0.125'));assert(flattened(ids.get('connection-key-rows')).includes('2 次未估值'));assert(flattened(ids.get('connection-key-rows')).includes('3 次 / 2 张'));assert(ids.get('connection-key-rows').querySelectorAll('a').some(a=>a.href==='/dashboard?api_key_id=key1'));
 keyUsage={key1:{known_cost_requests:0,unknown_cost_requests:2,estimated_cost_usd:0,image_requests:2,generated_images:0}};
 await click('connection-keys-refresh');assert(flattened(ids.get('connection-key-rows')).includes('未估值'));assert(!flattened(ids.get('connection-key-rows')).includes('$0.00'));
 usageStatus='unavailable';await click('connection-keys-refresh');assert(flattened(ids.get('connection-key-rows')).includes('用量读取失败'));assert(!flattened(ids.get('connection-key-rows')).includes('尚无新采集记录'));assert.equal(buttons().length,2);usageStatus='ok';
 await click(buttons().find(b=>b.dataset.keyFocus==='toggle:key1'));assert.equal(keys[0].status,'disabled');assert(flattened(ids.get('connection-key-rows')).includes('已停用'));await click(buttons().find(b=>b.dataset.keyFocus==='toggle:key1'));assert.equal(keys[0].status,'active');
 await click(buttons().find(b=>b.dataset.keyFocus==='revoke:key1'));assert.equal(ids.get('connection-revoke-dialog').open,true);const deletes=calls.filter(c=>c.options.method==='DELETE').length;await click('connection-revoke-cancel');assert.equal(calls.filter(c=>c.options.method==='DELETE').length,deletes);
 await click(buttons().find(b=>b.dataset.keyFocus==='revoke:key1'));await click('connection-revoke-confirm');assert.equal(keys[0].status,'revoked');assert.equal(buttons().length,0);assert(flattened(ids.get('connection-key-rows')).includes('不可恢复'));
 mode='no-key';await submit('blocked');assert.equal(ids.get('connection-keys-error').hidden,false);assert(ids.get('connection-keys-error-text').textContent.includes('AUTHORIZATION'));mode='ok';await click('connection-keys-retry');
 await click('connection-section-examples');assert.equal(ids.get('connection-examples-panel').hidden,false);assert(ids.get('connection-curl').textContent.includes(connection.public_base_url));assert(ids.get('connection-curl').textContent.includes('"stream": false'));assert(!ids.get('connection-curl').textContent.includes('secret'));
 await click('connection-local-tab');assert(ids.get('connection-curl').textContent.includes('"stream": true'));

 assert.equal(calls.filter(c=>c.path==='/local-console/connection/key').length,0);
 await click('connection-kind-models');assert(ids.get('connection-curl').textContent.includes('/models"'));assert(!ids.get('connection-curl').textContent.includes('-d '));assert(!ids.get('connection-curl').textContent.includes('Content-Type'));
 await click('connection-kind-images');assert(ids.get('connection-curl').textContent.includes('/images/generations'));assert(ids.get('connection-curl').textContent.includes('"model": "chatgpt-image-latest"'));assert(!ids.get('connection-curl').textContent.includes('"stream"'));assert.equal(ids.get('connection-image-options').hidden,false);assert(ids.get('connection-image-note').textContent.includes('不保证底层原生 Image2'));assert(ids.get('connection-image-note').textContent.includes('仅返回 b64_json'));assert(!ids.get('connection-image-note').textContent.includes('url 或'));assert(ids.get('connection-curl').textContent.includes('"response_format": "b64_json"'));
 ids.get('connection-image-model').listeners.change({target:{value:'gpt-image-2'}});assert(ids.get('connection-curl').textContent.includes('"model": "gpt-image-2"'));assert(ids.get('connection-image-note').textContent.includes('默认未启用'));await click('connection-copy-example');assert(!copied.at(-1).includes('synthetic-system-secret'));assert(copied.at(-1).includes('$'+'{CHAT2API_API_KEY}'));
 ids.get('connection-kind-images').listeners.keydown({key:'Home',preventDefault(){}});assert.equal(document.activeElement,ids.get('connection-kind-models'));assert(ids.get('connection-curl').textContent.includes('/models"'));
 ids.get('connection-model-search').value='gpt-synthetic';ids.get('connection-model-search').listeners.input();assert.equal(ids.get('connection-model-rows').children.length,1);await click(ids.get('connection-model-rows').querySelectorAll('button')[0]);assert.equal(copied.at(-1),'gpt-synthetic');
 ids.get('connection-model-search').value='missing';ids.get('connection-model-search').listeners.input();assert.equal(ids.get('connection-model-empty').hidden,false);ids.get('connection-model-search').value='';ids.get('connection-model-search').listeners.input();assert.equal(ids.get('connection-model-rows').children.length,4);
 connection.public_status='stopped';connection.public_base_url=null;await click('connection-refresh');await click('connection-cloud-tab');assert(!ids.get('connection-curl').textContent.includes('synthetic.trycloudflare'));assert.equal(ids.get('connection-copy-public').disabled,true);
 await click('connection-reveal-key');assert.equal(ids.get('connection-api-key').value,'synthetic-system-secret');const timer=[...timers.values()].find(t=>t.ms===60000);assert(timer);timer.fn();assert(!ids.get('connection-api-key').value.includes('secret'));
 mode='failed';await click('connection-keys-refresh');assert.equal(ids.get('connection-keys-error').hidden,false);assert(flattened(ids.get('connection-key-rows')).includes('不可恢复'));
 mode='ok';await submit('expire-test');assert.equal(ids.get('connection-created-dialog').open,true);mode='expired';await click('connection-refresh');assert.equal(ids.get('connection-expired').hidden,false);assert.equal(ids.get('connection-created-value').value,'');assert.equal(ids.get('connection-created-dialog').open,false);assert.equal(ids.get('connection-create-key').disabled,true);
 console.log('PASS API management UI: initial metadata only, safe names, one-time create/copy/explicit close, no blur loss, navigation warning, enable/disable, revoke confirmation, no-key/failed/expired states, public/local examples, system key hiding.');
})().catch(e=>{console.error(e);process.exitCode=1;});
