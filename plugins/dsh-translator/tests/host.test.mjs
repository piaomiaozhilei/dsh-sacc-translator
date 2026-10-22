import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, mkdir, writeFile, readdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from '../src/host.js';

test('host text translation, request auditing, validation and reload', async () => {
  const home=await mkdtemp(join(tmpdir(),'translator-test-'));process.env.DSH_HOME=home;
  let route, dispose; let fail=false; let chosen;
  const ctx={connection:{fetch:{register:r=>{route=r.fetch;return ()=>{};}}},
    sessionController:{create:async()=>({sessionId:'test-session'})},
    agentDefaultModel:{currentSelection:()=>({provider:'fake',model:'fixture'})},
    agents:{get:()=>({session:{append:(type,selection)=>{assert.equal(type,'model/selection');assert.equal(selection.reasoningEffort,'off');chosen=selection;}}})},
    sessionProjections:{stateOf:()=>({pending:chosen})},
    llm:{resolveCallConfig:async selection=>{assert.equal(selection.reasoningEffort,'off');return selection;},async *stream(request){assert.equal(request.provider,'fake');
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
  await call({action:'session'});
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
  assert.equal(saved.selection.model,'fixture');assert.equal(saved.selection.reasoningEffort,'off');assert.equal(state.text,'Hello');
  const jobFolder=join(home,'sacc-translator',state.id);
  const auditName=(await readdir(jobFolder)).find(name=>name.startsWith('request-')&&name.endsWith('.json')&&!name.endsWith('-response.json'));
  const audit=JSON.parse(await readFile(join(jobFolder,auditName),'utf8'));
  assert.equal(audit.promptVersion,2);assert.equal(audit.units[0].id,'text');
  assert.match(audit.system,/Simplified Chinese/);
  assert.deepEqual(JSON.parse(await readFile(join(jobFolder,auditName.replace('.json','-raw.txt')),'utf8')),{'0':'你好'});
  assert.deepEqual(JSON.parse(await readFile(join(jobFolder,auditName.replace('.json','-response.json')),'utf8')),{text:'你好'});
  assert.equal(saved.concurrency,3);assert.equal(saved.ocrQuality,'high');assert.equal(saved.ocrWorkers,ocrWorkers);assert.equal(saved.preferGpu,false);assert.equal(saved.gpuMemory,'low_memory');
  assert.equal((await call({action:'speech',audioBase64:''})).status,409);
  chosen={provider:'fake',model:'selected-model',reasoningEffort:'high'};fail=true;
  await call({action:'start',target:'中文',text:'Hello',sessionId:'test-session'});
  for(let i=0;i<100;i++){state=(await call()).body.jobs[0];if(state.status!=='running')break;await new Promise(r=>setTimeout(r,10));}
  assert.equal(state.status,'failed');
  await dispose();await apply(ctx);assert.equal((await call()).body.jobs.length,3);
  assert.equal((await call()).body.settings.concurrency,3);
  const cache=await call({action:'cache'});
  assert.equal(cache.status,200);assert.ok(cache.body.bytes>0);assert.equal(cache.body.historyCount,3);
  assert.equal((await call({action:'clearCache'})).status,200);
  assert.equal((await call()).body.jobs.length,0);
  assert.equal((await call({action:'cache'})).body.bytes,0);
  await dispose();await apply(ctx);
  assert.equal((await call()).body.jobs.length,0);
  assert.equal((await call()).body.settings.concurrency,3);await dispose();
});
