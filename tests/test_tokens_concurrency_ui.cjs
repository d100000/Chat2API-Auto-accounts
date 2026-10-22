// Offline import interactions; all credentials and API replies are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(id) { this.id=id; this.value=''; this.hidden=false; this.disabled=false; this.textContent=''; this.type='password'; this.open=false; this.events={}; this.attrs={}; this.children=[]; this.dataset={}; }
  addEventListener(name, handler) { this.events[name]=handler; }
  setAttribute(name,value) { this.attrs[name]=value; }
  focus() { this.focused=true; }
  reportValidity() { return true; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children.flatMap(c => c.fragment ? c.children : c); }
  showModal() { this.open=true; }
  close() { this.open=false; if(this.events.close) this.events.close({}); }
  async emit(name) { return this.events[name]?.({preventDefault(){},target:this}); }
}
function harness(file, prefix='') {
  const elements=new Map(); const get=(id)=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id)};
  const requests=[], replies=[], copies=[], redirects=[];
  const document={getElementById:get,body:{dataset:{apiPrefix:prefix}},createElement:(tag)=>new Element(tag),createDocumentFragment:()=>Object.assign(new Element('fragment'),{fragment:true})};
  const window={setTimeout,clearTimeout,location:{assign:url=>redirects.push(url)},ConsoleUI:{toast(){},copyText:async text=>{copies.push(text);return true}}};
  const context=vm.createContext({document,window,AbortController,URLSearchParams,TypeError,fetch:async(url,options)=>{requests.push({url,options});const next=replies.shift();if(!next)throw new Error('Unexpected API request');return {ok:next.status>=200&&next.status<300,status:next.status,json:async()=>next.data}}});
  get('token-reason-limit').value='inherit'; get('token-image-limit').value='inherit'; get('token-overwrite-existing').checked=false;
  vm.runInContext(fs.readFileSync(file,'utf8'),context);
  return {get,requests,replies,copies,redirects};
}
(async()=>{
 const h=harness('static/local-console/tokens.js','custom-api');
 assert.equal(h.get('token-upload').disabled,true);
 h.get('error-tokens-section').hidden=true;
 h.get('token-input').value=' # comment\n  test-token-A \n\n test-token-B';
 await h.get('token-input').emit('input'); assert.equal(h.get('token-line-count').textContent,'2 条待导入');
 h.replies.push({status:500,data:{detail:'服务暂不可用'}});
 await h.get('uploadForm').emit('submit'); assert.match(h.get('tokens-error').textContent,/HTTP 500/); assert.ok(h.get('token-input').value.includes('test-token-A')); assert.equal(h.requests[0].url,'/custom-api/tokens/upload'); assert.equal(h.requests[0].options.body.get('text'),'test-token-A\ntest-token-B');
 h.get('token-reason-limit').value='5'; h.get('token-image-limit').value='2'; h.get('token-overwrite-existing').checked=true; await h.get('token-overwrite-existing').emit('change'); assert.equal(h.get('token-overwrite-warning').hidden,false);
 h.replies.push({status:200,data:{status:'success',tokens_count:2,added_credentials:2,duplicate_credentials:1,new_accounts:1,existing_accounts:1,settings_applied:2,pending_identity:1,account_ids:['acct_'+'a'.repeat(24),'bad<script>']}});
 await h.get('uploadForm').emit('submit'); assert.equal(h.get('token-input').value,''); assert.equal(h.get('token-count').textContent,'2'); assert.equal(h.get('token-upload').disabled,true); assert.equal(h.get('token-upload').attrs['aria-busy'],'false');
 assert.equal(h.requests.at(-1).options.body.get('reason_limit'),'5'); assert.equal(h.requests.at(-1).options.body.get('image_gen_limit'),'2'); assert.equal(h.requests.at(-1).options.body.get('overwrite_existing'),'true');
 assert(h.get('tokens-feedback').textContent.includes('跳过 1 条重复')); assert(h.get('tokens-feedback').textContent.includes('1 个身份待确认')); assert(h.get('tokens-import-accounts').href.includes('acct_')); assert(!h.get('tokens-import-accounts').href.includes('script'));
 h.get('token-input').value='synthetic-credential-C'; h.get('token-reason-limit').value='0'; const invalidBefore=h.requests.length; await h.get('uploadForm').emit('submit'); assert.equal(h.requests.length,invalidBefore); assert(h.get('tokens-error').textContent.includes('1–10')); h.get('token-reason-limit').value='inherit';
 h.replies.push({status:200,data:{status:'success',tokens_count:3}}); await h.get('uploadForm').emit('submit'); assert(h.get('tokens-feedback').textContent.includes('检查状态')); assert.equal(h.get('tokens-import-accounts').href,'/accounts');
 const hostile='<img src=x onerror=alert(1)>';
 h.replies.push({status:200,data:{status:'success',error_tokens:[hostile,'test-token-C']}});
 await h.get('errorButton').emit('click'); assert.equal(h.get('error-token-list').children[0].children[1].textContent,'••••••••••••••••'); assert.equal(h.get('error-tokens-section').hidden,false);
 await h.get('error-token-reveal').emit('click'); assert.equal(h.get('error-token-list').children[0].children[1].textContent,hostile); assert.equal(h.get('error-token-reveal').attrs['aria-pressed'],'true');
 await h.get('error-token-copy').emit('click'); assert.equal(h.copies[0],hostile+'\ntest-token-C');
 const before=h.requests.length;
 await h.get('token-clear').emit('click'); assert.equal(h.get('clear-dialog').open,true); assert.equal(h.get('clear-cancel').focused,true); assert.equal(h.requests.length,before);
 await h.get('clear-cancel').emit('click'); assert.equal(h.get('clear-dialog').open,false); assert.equal(h.requests.length,before);
 await h.get('token-clear').emit('click'); h.replies.push({status:200,data:{status:'success',tokens_count:0}}); await h.get('clearForm').emit('submit'); assert.equal(h.requests.at(-1).url,'/custom-api/tokens/clear'); assert.equal(h.get('token-count').textContent,'0'); assert.equal(h.get('error-token-list').children.length,0); assert.equal(h.get('clear-dialog').open,false);
 h.replies.push({status:401,data:{detail:'请先登录'}}); await h.get('errorButton').emit('click'); assert.equal(h.get('tokens-session-expired').hidden,false); assert.equal(h.get('token-upload').disabled,true);
 console.log('PASS import UI: independent limits, inherit/overwrite fields, new receipt counters, pending identity, safe links, old receipt compatibility, failed input retention, validation, clear confirmation, expired session.');
})().catch(error=>{console.error(error);process.exitCode=1});
