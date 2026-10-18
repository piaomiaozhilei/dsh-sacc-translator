import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, rename, readdir, open, copyFile } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { Readable } from 'node:stream';
import { join, dirname, resolve, basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir, cpus } from 'node:os';
import { createInterface } from 'node:readline';
import { translationCache } from './cache.js';
import { translationPrompt, decodeTranslations } from './prompt.js';

export const inject = ['connection', 'sessionController', 'sessionProjections', 'agents', 'fileUploads', 'attachments', 'llm', 'agentDefaultModel'];
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const edition = JSON.parse(await readFile(join(root, 'package.json'), 'utf8')).translatorEdition;
const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {'content-type': 'application/json', 'cache-control': 'no-store'}});
const terminal = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
const languages = {'中文':'zh','English':'en','日本語':'ja','한국어':'ko','Français':'fr','Deutsch':'de','Español':'es','العربية':'ar'};

export async function apply(ctx) {
  // One stable data root survives edition changes and uninstall/reinstall.
  const data = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'sacc-translator');
  await mkdir(data, {recursive: true});
  const settingsPath = join(data, 'settings.json');
  const defaults = {concurrency: 1, ocrQuality: 'medium', ocrWorkers: 1, preferGpu: true, gpuMemory: 'performance'};
  const maxOcrWorkers = cpus().length >= 12 ? 3 : cpus().length >= 6 ? 2 : 1;
  const validSetting = (key, value) => ({concurrency: () => Number.isInteger(value) && value >= 1 && value <= 8,
    ocrQuality: () => ['ultra_low','low','medium','high'].includes(value),
    ocrWorkers: () => Number.isInteger(value) && value >= 1 && value <= 3,
    preferGpu: () => typeof value === 'boolean', gpuMemory: () => ['performance','low_memory'].includes(value)})[key]?.();
  let settings = {...defaults};
  try {
    const saved = JSON.parse(await readFile(settingsPath, 'utf8'));
    for (const key of Object.keys(defaults)) if (validSetting(key, saved[key])) settings[key] = saved[key];
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  let settingsWrite = Promise.resolve();
  const jobs = new Map();
  const live = new Map();
  const previewStops = new Set();
  const sessions = new Set();
  let disposed = false;
  let admitting = false;
  let clearing = false, activeRequests = 0;
  const persist = async job => {
    const dir = join(data, job.id); await mkdir(dir, {recursive: true});
    // Serialize writes for each job: progress can arrive while a model request settles.
    const previous = jobWrites.get(job.id) || Promise.resolve();
    const snapshot = JSON.stringify(job, null, 2);
    const task = previous.catch(() => {}).then(async () => {
      const path = join(dir, 'job.json');
      await writeFile(path + '.tmp', snapshot); await rename(path + '.tmp', path);
    });
    jobWrites.set(job.id, task); await task;
  };
  const jobWrites = new Map();
  for (const dir of await readdir(data, {withFileTypes: true})) {
    if (!dir.isDirectory()) continue;
    try {
      const job = JSON.parse(await readFile(join(data, dir.name, 'job.json'), 'utf8'));
      if (job.id !== dir.name) continue;
      if (!terminal.has(job.status)) { job.status = 'interrupted'; job.message = '上次运行已中断，请重新提交'; await persist(job); }
      jobs.set(job.id, job);
    } catch { /* Incomplete first-write directories are not published jobs. */ }
  }
  const publicJob = job => ({id: job.id, status: job.status, message: job.message, progress: job.progress || 0,
    createdAt: job.createdAt, edition: job.edition, text: job.text, source: job.source, target: job.target, selection: job.selection, runtime: job.runtime, result: job.result, error: job.error,
    artifacts: (job.artifacts || []).map(a => ({artifact_id: a.artifact_id, status: a.status, progress: a.progress,
      message: a.message, warnings: a.warnings, name: a.output?.name || a.source?.name, downloadable: Boolean(a.output?.storage_key)}))});
  const stop = (id, status = 'cancelled') => {
    const running = live.get(id); const job = jobs.get(id);
    if (!job || terminal.has(job.status)) return;
    job.status = status; job.message = status === 'cancelled' ? '已取消' : '插件已停止，任务中断';
    running?.controller.abort();
    if (running?.child?.pid) {
      if (process.platform === 'win32') spawn('taskkill', ['/PID', String(running.child.pid), '/T', '/F'], {windowsHide:true, stdio:'ignore'}).on('error', () => running.child.kill());
      else running.child.kill();
    }
    return persist(job);
  };
  ctx.on('dispose', async () => {
    disposed = true;
    for(const stopPreview of previewStops)stopPreview();
    await Promise.all([...live.keys()].map(id => stop(id, 'interrupted')));
    await Promise.allSettled([...jobWrites.values()]);
    await settingsWrite.catch(() => {});
  });

  async function modelBatch(job, units, signal) {
    signal = AbortSignal.any([signal, AbortSignal.timeout(600000)]);
    signal.throwIfAborted();
    const selection = job.selection;
    const {system, input} = translationPrompt(job.source, job.target, units);
    // Persist the exact model input before sending; no credentials enter the audit.
    const audit = join(data, job.id, `request-${randomUUID()}.json`);
    const messages = [{id: randomUUID(), role: 'user', source: {kind: 'user'}, content: [{type: 'text', text: input}]}];
    await writeFile(audit, JSON.stringify({selection, system, messages, units, promptVersion: 2}));
    let text = ''; let finish;
    for await (const chunk of ctx.llm.stream({...selection, system, messages, signal})) {
      signal.throwIfAborted();
      if (chunk.type === 'text-delta') text += chunk.text;
      if (chunk.type === 'finish') finish = chunk;
    }
    if (!finish || !['stop', 'end_turn'].includes(typeof finish.reason === 'string' ? finish.reason : finish.reason?.kind)) {
      throw new Error(`模型未正常完成: ${JSON.stringify(finish?.reason || 'stream ended')}`);
    }
    // Preserve rejected output too, so protocol problems can be diagnosed offline.
    await writeFile(audit.replace('.json', '-raw.txt'), text);
    const result = decodeTranslations(text, units);
    await writeFile(audit.replace('.json', '-response.json'), JSON.stringify(result));
    return result;
  }

  async function spreadsheetPreview(path, signal) {
    if(!/\.xlsx?$/i.test(path))return path;
    const folder=join(data,'previews',randomUUID());await mkdir(folder,{recursive:true});
    const destination=join(folder,'preview.xlsx');
    const worker=spawn(join(root,'runtime','translator-worker.exe'),[],{windowsHide:true,cwd:join(root,'runtime'),stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONUTF8:'1'}});
    return await new Promise((resolveResult,reject)=>{
      let result, failure, stderr='';
      const abort=()=>{failure=new Error('表格预览已取消或处理超时');if(process.platform==='win32'&&worker.pid)spawn('taskkill',['/PID',String(worker.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'}).on('error',()=>worker.kill());else worker.kill();};
      previewStops.add(abort);
      const timer=setTimeout(abort,190000);
      signal.addEventListener('abort',abort,{once:true});
      worker.stdin.on('error',()=>{});
      worker.stderr.on('data',chunk=>{stderr=(stderr+chunk).slice(-2000);});
      createInterface({input:worker.stdout}).on('line',line=>{try{const event=JSON.parse(line);if(event.type==='complete')result=event.path;if(event.type==='error')failure=new Error(event.error);}catch{failure=new Error('无效的表格预览响应');}});
      const cleanup=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);previewStops.delete(abort);};
      worker.on('error',error=>{cleanup();reject(error);});
      worker.on('close',code=>{cleanup();if(failure||code||!result)reject(failure||new Error(stderr||'表格预览准备失败'));else resolveResult(destination);});
      if(signal.aborted)abort();else worker.stdin.end(JSON.stringify({type:'spreadsheet-preview',source:path,destination})+'\n');
    });
  }

  async function run(job, controller) {
    try {
      if (job.text) {
        job.result = (await modelBatch(job, [{id: 'text', text: job.text}], controller.signal)).text;
      } else {
        const worker = join(root, 'runtime', 'translator-worker.exe');
        const child = spawn(worker, [], {windowsHide: true, cwd: join(root, 'runtime'), stdio: ['pipe','pipe','pipe'],
          env: {...process.env, PYTHONUTF8: '1', TRANSLATOR_EDITION: edition}});
        live.get(job.id).child = child;
        child.stdin.on('error', () => {}); // Exit/cancellation owns the task failure.
        const send = message => { if (!child.stdin.destroyed) child.stdin.write(JSON.stringify(message) + '\n'); };
        let stderr = '';
        child.stderr.on('data', bytes => { stderr = (stderr + bytes).slice(-4000); });
        await new Promise((done, reject) => {
          let completed = false;
          const lines = createInterface({input: child.stdout});
          lines.on('line', line => {
            void (async () => {
              const event = JSON.parse(line);
              if (event.type === 'translate') {
                // The worker schedules document batches; do not multiply its
                // concurrency again by splitting each request in the host.
                try { send({type: 'translation', id: event.id, result: await modelBatch(job, event.units, controller.signal)}); }
                catch (error) { send({type: 'translation', id: event.id, error: String(error.message || error)}); }
              } else if (event.type === 'progress' && !terminal.has(job.status)) {
                job.artifacts = event.artifacts; job.message = event.message || event.artifacts?.[0]?.message;
                job.progress = event.artifacts?.[0]?.progress || 0; await persist(job);
              } else if (event.type === 'runtime') { job.runtime = event; await persist(job); }
              else if (event.type === 'complete') { job.artifacts = event.artifacts; completed = true; }
              else if (event.type === 'error') { reject(new Error(event.error)); child.kill(); }
            })().catch(error => { reject(error); child.kill(); });
          });
          child.on('error', reject);
          child.on('exit', code => {
            lines.close();
            if (controller.signal.aborted) reject(new Error('已取消'));
            else if (code !== 0 || !completed) reject(new Error(stderr || `翻译进程退出 (${code})`));
            else done();
          });
          send({type: 'start', ...job, upload_folder: join(data, job.id, 'files')});
        });
        if (job.artifacts.some(a => !['completed','completed_with_warnings'].includes(a.status))) throw new Error(job.artifacts.map(a => a.error || a.message).join('\n'));
      }
      if (!controller.signal.aborted) { job.status = 'completed'; job.progress = 100; job.message = '翻译完成'; }
    } catch (error) {
      if (!terminal.has(job.status)) { job.status = 'failed'; job.error = String(error.message || error); job.message = '翻译失败，可重新提交'; }
    } finally { live.delete(job.id); await persist(job); }
  }

  async function handle(request) {
    try {
      if (disposed) return json({error: '插件已停止'}, 503);
      const url = new URL(request.url);
      if (request.method === 'GET' && url.searchParams.has('download')) {
        const job = jobs.get(url.searchParams.get('download'));
        const artifact = job?.artifacts?.find(a => a.artifact_id === url.searchParams.get('artifact'));
        if (!artifact?.output?.storage_key) return json({error: '找不到产物'}, 404);
        const folder = resolve(data, job.id, 'files'); const path = resolve(folder, artifact.output.storage_key);
        if (!path.startsWith(folder + '\\') && !path.startsWith(folder + '/')) return json({error: '无效路径'}, 400);
        const handle = await open(path, 'r');
        return new Response(Readable.toWeb(handle.createReadStream()), {headers: {'content-type': 'application/octet-stream',
          'content-disposition': `attachment; filename*=UTF-8''${encodeURIComponent(artifact.output.name)}`}});
      }
      if (request.method === 'GET') return json({edition, settings, capabilities: {maxOcrWorkers, logicalCpus: cpus().length}, jobs: [...jobs.values()].sort((a,b) => b.createdAt.localeCompare(a.createdAt)).map(publicJob)});
      const body = await request.json();
      if (body.action === 'cache') return json({...await translationCache(data),historyCount:jobs.size});
      if (body.action === 'clearCache') {
        if(live.size || admitting || activeRequests>1) return json({error:'请等待翻译、上传或预览加载结束后清理'},409);
        clearing=true;
        try {
          await Promise.allSettled([...jobWrites.values()]);
          const removed=await translationCache(data,true);
          jobs.clear(); jobWrites.clear();
          return json({removed,...await translationCache(data),historyCount:0});
        } finally {clearing=false;}
      }
      if (body.action === 'settings') {
        if (!Number.isInteger(body.concurrency) || body.concurrency < 1 || body.concurrency > 8) return json({error: '并发数量须为 1–8 的整数'}, 400);
        for (const key of Object.keys(defaults)) if (key in body && !validSetting(key, body[key])) return json({error: `无效设置：${key}`}, 400);
        const next = {...settings, ...Object.fromEntries(Object.keys(defaults).filter(key => key in body).map(key => [key, body[key]]))};
        settingsWrite = settingsWrite.catch(() => {}).then(async () => {
          await writeFile(settingsPath + '.tmp', JSON.stringify(next));
          await rename(settingsPath + '.tmp', settingsPath); settings = next;
        });
        await settingsWrite;
        return json({settings: next});
      }
      if (body.action === 'speech') {
        const speech = ctx.get('speechController');
        if (!speech) return json({error: '请先在插件中启用“语音输入”，并完成模型准备'}, 409);
        return json(await speech.transcribe({audioBase64: body.audioBase64}, request.signal));
      }
      if (body.action === 'session') {
        const created = await ctx.sessionController.create({cwd: data}); sessions.add(created.sessionId);
        // Initialize only this translation session; selectModel also changes the host-wide default.
        const selection = await ctx.llm.resolveCallConfig({...ctx.agentDefaultModel.currentSelection(), reasoningEffort: 'off'});
        ctx.agents.get(created.sessionId).session.append('model/selection', {
          provider: selection.provider, model: selection.model,
          ...(selection.reasoningEffort === undefined ? {} : {reasoningEffort: selection.reasoningEffort}),
        });
        return json(created);
      }
      if (body.action === 'preview') {
        if (!sessions.has(body.sessionId)) return json({error: '翻译会话已失效，请刷新页面'}, 400);
        let path;
        if (body.receiptId) {
          const ref = ctx.fileUploads.resolve(ctx.agents.get(body.sessionId), body.receiptId);
          if (!ref) return json({error: '上传凭据无效'}, 400);
          const folder = join(data, 'previews', randomUUID()); await mkdir(folder, {recursive:true});
          path = join(folder, basename(ref.name || 'document'));
          const file = await open(path, 'wx');
          try { for await (const bytes of ctx.attachments.readFileStream(ref, request.signal)) await file.write(bytes); }
          finally { await file.close(); }
        } else {
          const job = jobs.get(body.id);
          if (!job) return json({error:'找不到任务'},404);
          const folder = resolve(data, job.id, 'files');
          if (body.original) {
            const source = job.files?.[0];
            if (!source) return json({error:'找不到原文件'},404);
            const original = resolve(folder, source.id);
            if (!original.startsWith(folder + '/') && !original.startsWith(folder + '\\')) throw new Error('无效路径');
            const previewFolder = join(data, job.id, 'preview'); await mkdir(previewFolder,{recursive:true});
            path = join(previewFolder, basename(source.name)); await copyFile(original,path);
          } else {
            const artifact = job.artifacts?.find(a => a.artifact_id === body.artifact);
            if (!artifact?.output?.storage_key) return json({error:'找不到译文'},404);
            path = resolve(folder, artifact.output.storage_key);
            if (!path.startsWith(folder + '/') && !path.startsWith(folder + '\\')) throw new Error('无效路径');
          }
        }
        path=await spreadsheetPreview(path,request.signal);
        return json({path, cwd:data});
      }
      if (body.action === 'cancel') { await stop(body.id); return json({ok: true}); }
      if (body.action !== 'start') return json({error: '未知操作'}, 400);
      if (live.size || admitting) return json({error: '请等待当前任务完成或取消后重试'}, 409);
      admitting = true;
      try {
      if (typeof body.target !== 'string' || !body.target.trim() || body.target.length > 80) return json({error: '请选择目标语言'}, 400);
      if (!sessions.has(body.sessionId)) return json({error: '翻译会话已失效，请刷新页面'}, 400);
      if (!(body.text?.trim()) && !body.receiptId) return json({error: '请输入文本或选择文件'}, 400);
      const job = {id: randomUUID(), sessionId: body.sessionId, source: languages[body.source] || String(body.source || 'auto').slice(0,80), target: languages[body.target] || body.target,
        text: typeof body.text === 'string' ? body.text.trim() : '', status: 'running', edition, createdAt: new Date().toISOString(), message: '准备翻译', files: []};
      const sessionAgent = ctx.agents.get(body.sessionId);
      if (!sessionAgent) return json({error: '翻译会话已失效，请刷新页面'}, 400);
      const selected = ctx.sessionProjections.stateOf(sessionAgent.session, 'modelSelection');
      // Snapshot the host selector's session choice for every chunk in this task.
      job.selection = {...(selected?.pending ?? selected?.lastUsed ?? ctx.agentDefaultModel.currentSelection())};
      Object.assign(job, settings);
      if (job.text.length > 100000) return json({error: '文本超过 100000 字符，请改用文件'}, 400);
      const folder = join(data, job.id, 'files'); await mkdir(folder, {recursive: true});
      if (body.receiptId) {
        const agent = ctx.agents.get(body.sessionId);
        const ref = agent && ctx.fileUploads.resolve(agent, body.receiptId);
        if (!ref) return json({error: '上传凭据无效或不属于此会话'}, 400);
        const name = basename(ref.name || 'document');
        const id = randomUUID(); const file = await open(join(folder, id), 'wx');
        try { for await (const bytes of ctx.attachments.readFileStream(ref, request.signal)) await file.write(bytes); }
        finally { await file.close(); }
        job.files.push({id, name}); job.text = '';
      }
      jobs.set(job.id, job); await persist(job);
      const controller = new AbortController(); live.set(job.id, {controller}); void run(job, controller);
      return json(publicJob(job));
      } finally { admitting = false; }
    } catch (error) { return json({error: String(error.message || error)}, 400); }
  }
  ctx.effect(() => ctx.connection.fetch.register({path: '/api/sacc-translator', methods: ['GET','POST'], requestBody: 'buffered', fetch: async request=>{
    if(clearing)return json({error:'正在清理翻译缓存，请稍后重试'},409);
    activeRequests++;
    try{return await handle(request);}finally{activeRequests--;}
  }}));
}
