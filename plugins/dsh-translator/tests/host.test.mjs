import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, mkdir, writeFile, readdir, unlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply, uploadedFileReference, translatorModelSelection, translationDefaultSelection} from '../src/host.js';

test('translation defaults disable supported reasoning without mutating chat defaults',async()=>{
  for(const [provider,off] of [['deepseek-official','off'],['deepseek-account','off'],['department-gateway-member','none']]) {
    const original={provider,model:'deepseek-flash',reasoningEffort:'high'};
    const selected=await translationDefaultSelection({resolveModelInfo:async()=>({reasoning:{efforts:[{id:off},{id:'high'}]}})},original);
    assert.equal(selected.reasoningEffort,off);assert.equal(original.reasoningEffort,'high');
  }
  const current={provider:'other',model:'reasoner',reasoningEffort:'high'};
  assert.deepEqual(await translationDefaultSelection({resolveModelInfo:async()=>({reasoning:{efforts:[{id:'high'}]}})},current),current);
  assert.equal((await translationDefaultSelection({resolveModelInfo:async()=>({})},current)).reasoningEffort,undefined);
});

test('upload receipts unwrap file references and preserve legacy references', () => {
  const ref = {id:'file-fixture',name:'1.pdf'};
  assert.equal(uploadedFileReference({type:'file',attachment:ref}),ref);
  assert.equal(uploadedFileReference(ref),ref);
  assert.equal(uploadedFileReference(undefined),undefined);
  assert.equal(uploadedFileReference({type:'file',attachment:undefined}),undefined);
  assert.throws(()=>uploadedFileReference({type:'image',attachment:ref}),/图片引用/);
});

test('missing host APIs stop startup with an actionable compatibility error', async () => {
  await assert.rejects(apply({}), error => /当前 DSH 缺少必要接口/.test(error.message) && /llm.stream/.test(error.message));
});

for (const [provider, model, defaultEffort, expectedEffort] of [
  ['fake','fixture',undefined,undefined], ['fake','fixture','high','high'], ['fake','fixture','off','off'],
  ['deepseek-official','deepseek-flash','none','off'], ['deepseek-official','deepseek-flash','high','high'],
  ['department-gateway-member','deepseek-flash','off','none'], ['department-gateway-member','deepseek-flash','none','none'],
]) test(`host translation provider compatibility: ${provider}/${defaultEffort}`, async () => {
  const home=await mkdtemp(join(tmpdir(),'translator-test-'));process.env.DSH_HOME=home;
  let route, dispose; let fail=false; let chosen;
  const ctx={connection:{fetch:{register:r=>{route=r.fetch;return ()=>{};}}},
    sessionController:{create:async()=>({sessionId:'test-session'})},
    agentDefaultModel:{currentSelection:()=>({provider,model,...(defaultEffort === undefined ? {} : {reasoningEffort:defaultEffort})})},
    agents:{get:()=>({session:{append:(type,selection)=>{assert.equal(type,'model/selection');assert.equal(selection.reasoningEffort,expectedEffort);chosen=selection;}}})},
    sessionProjections:{stateOf:()=>({pending:chosen})},
    llm:{resolveCallConfig:async selection=>{assert.equal(selection.reasoningEffort,expectedEffort);return selection;},async *stream(request){assert.equal(request.provider,provider);
      assert.equal(request.model,chosen.model);assert.equal(request.reasoningEffort,chosen.reasoningEffort);
      assert.match(request.system,/Simplified Chinese/);
      assert.deepEqual(JSON.parse(request.messages[0].content[0].text),{items:{'0':'Hello'}});
      if(fail){yield {type:'finish',reason:{kind:'error'}};return;}
      yield {type:'text-delta',text:'{"0":"你好"}'};yield {type:'finish',reason:{kind:'stop'}};}},
    effect:fn=>fn(),on:(event,fn)=>{dispose=fn;},get:()=>undefined};
  const fixture=join(home,'sacc-translator','preview-fixture');
  await mkdir(join(fixture,'files'),{recursive:true});
  await writeFile(join(fixture,'files','input'),'original');
  await writeFile(join(fixture,'files','output.pdf'),'translated');
  await writeFile(join(fixture,'job.json'),JSON.stringify({id:'preview-fixture',status:'completed',createdAt:'2000-01-01',files:[{id:'input',name:'中文 #1.pdf'}],artifacts:[{artifact_id:'a',output:{storage_key:'output.pdf',name:'译文.pdf'}},{artifact_id:'escape',output:{storage_key:'../../outside.pdf'}}]}));
  await apply(ctx);
  const call=async body=>{const response=await route(new Request('http://localhost/api/sacc-translator',body?{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}:{}));return {status:response.status,body:await response.json()};};
  assert.equal((await call({action:'start',target:'中文',text:'Hello',sessionId:'foreign'})).status,400);
  assert.equal((await call({action:'session'})).status,200);
  assert.equal((await call({action:'selectModel',sessionId:'foreign',selection:{provider,model}})).status,400);
  assert.equal((await call({action:'selectModel',sessionId:'test-session',selection:{provider,model,reasoningEffort:expectedEffort}})).status,200);
  assert.equal(ctx.agentDefaultModel.currentSelection().reasoningEffort,defaultEffort);
  // Reload before a job exists: only the durable registry can retain this session.
  await dispose();await apply(ctx);
  const original=await call({action:'preview',sessionId:'test-session',id:'preview-fixture',original:true});
  assert.equal(original.status,200);assert.equal(await readFile(original.body.path,'utf8'),'original');
  assert.ok(original.body.path.endsWith('中文 #1.pdf'));
  const output=await call({action:'preview',sessionId:'test-session',id:'preview-fixture',artifact:'a'});
  assert.equal(output.status,200);assert.equal(await readFile(output.body.path,'utf8'),'translated');
  assert.equal((await call({action:'preview',sessionId:'test-session',id:'preview-fixture',artifact:'escape'})).status,400);
  assert.equal((await call({action:'settings',concurrency:0})).status,400);
  assert.equal((await call({action:'settings',concurrency:1.5})).status,400);
  assert.equal((await call({action:'settings',concurrency:9})).status,400);
  const settingsResult=await call({action:'settings',concurrency:3});
  assert.equal(settingsResult.status,200,JSON.stringify(settingsResult.body));
  assert.equal((await call()).body.settings.ocrQuality,'medium');
  for (const patch of [{ocrQuality:'bad'},{ocrWorkers:4},{preferGpu:'yes'},{gpuMemory:'bad'}])
    assert.equal((await call({action:'settings',concurrency:3,...patch})).status,400);
  const ocrWorkers=(await call()).body.capabilities.maxOcrWorkers;
  assert.equal((await call({action:'settings',concurrency:3,ocrQuality:'high',ocrWorkers,preferGpu:false,gpuMemory:'low_memory'})).status,200);
  assert.equal((await call({action:'preview',sessionId:'foreign',id:'bad'})).status,400);
  assert.equal((await call({action:'preview',sessionId:'test-session',id:'bad'})).status,404);
  const started=await call({action:'start',target:'中文',text:'Hello',sessionId:'test-session'});
  assert.equal(started.status,200,JSON.stringify(started.body));
  let state;
  for(let i=0;i<100;i++){state=(await call()).body.jobs[0];if(state.status!=='running')break;await new Promise(r=>setTimeout(r,10));}
  assert.equal(state.status,'completed');assert.equal(state.result,'你好');
  assert.ok(Date.parse(state.completedAt) >= Date.parse(state.createdAt));
  const saved=JSON.parse(await readFile(join(home,'sacc-translator',state.id,'job.json'),'utf8'));
  assert.equal(saved.target,'zh');
  assert.equal(saved.selection.model,model);assert.equal(saved.selection.reasoningEffort,expectedEffort);assert.equal(state.text,'Hello');
  const jobFolder=join(home,'sacc-translator',state.id);
  const auditName=(await readdir(jobFolder)).find(name=>name.startsWith('request-')&&name.endsWith('.json')&&!name.endsWith('-response.json'));
  const audit=JSON.parse(await readFile(join(jobFolder,auditName),'utf8'));
  assert.equal(audit.promptVersion,3);assert.equal(audit.units[0].id,'text');
  assert.match(audit.system,/Simplified Chinese/);
  assert.deepEqual(JSON.parse(await readFile(join(jobFolder,auditName.replace('.json','-raw.txt')),'utf8')),{'0':'你好'});
  assert.deepEqual(JSON.parse(await readFile(join(jobFolder,auditName.replace('.json','-response.json')),'utf8')),{text:'你好'});
  assert.equal(saved.concurrency,3);assert.equal(saved.ocrQuality,'high');assert.equal(saved.ocrWorkers,ocrWorkers);assert.equal(saved.preferGpu,false);assert.equal(saved.gpuMemory,'low_memory');
  assert.equal((await call({action:'speech',audioBase64:''})).status,409);
  chosen={provider,model:'selected-model',reasoningEffort:'high'};fail=true;
  await call({action:'start',target:'中文',text:'Hello',sessionId:'test-session'});
  for(let i=0;i<100;i++){state=(await call()).body.jobs[0];if(state.status!=='running')break;await new Promise(r=>setTimeout(r,10));}
  assert.equal(state.status,'failed');
  await dispose();
  // Upgrade from an older plugin with history but no session registry.
  await unlink(join(home,'sacc-translator','translation-sessions.json'));
  await apply(ctx);assert.equal((await call()).body.jobs.length,3);
  assert.equal((await call({action:'preview',sessionId:'test-session',id:'preview-fixture',original:true})).status,200);
  assert.equal((await call({action:'preview',sessionId:'foreign',id:'preview-fixture',original:true})).status,400);
  assert.equal((await call()).body.settings.concurrency,3);
  const cache=await call({action:'cache'});
  assert.equal(cache.status,200);assert.ok(cache.body.bytes>0);assert.equal(cache.body.historyCount,3);
  assert.equal((await call({action:'clearCache'})).status,200);
  assert.equal((await call()).body.jobs.length,0);
  assert.equal((await call({action:'cache'})).body.bytes,0);
  await dispose();await apply(ctx);
  assert.equal((await call()).body.jobs.length,0);
  assert.equal((await call()).body.settings.concurrency,3);await dispose();
  assert.deepEqual(JSON.parse(await readFile(join(home,'sacc-translator','translation-sessions.json'),'utf8')),['test-session']);
});

test('provider normalization preserves defaults, other models, and the original selection', () => {
  for (const provider of ['deepseek-official','department-gateway-member','other']) {
    const selection={provider,model:'another-model',reasoningEffort:'none'};
    assert.deepEqual(translatorModelSelection(selection),selection);
    assert.deepEqual(translatorModelSelection({provider,model:'deepseek-flash'}),{provider,model:'deepseek-flash'});
  }
  const original={provider:'deepseek-official',model:'deepseek-flash',reasoningEffort:'none'};
  assert.equal(translatorModelSelection(original).reasoningEffort,'off');
  assert.equal(original.reasoningEffort,'none');
});
