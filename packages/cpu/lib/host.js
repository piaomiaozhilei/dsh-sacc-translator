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
import { translationPrompt, decodeTranslations, singleTranslationPrompt, decodeSingleTranslation, recoverTranslations, TranslationFormatError } from './prompt.js';
import { prepareRuntime } from './runtime.js';
import { ScreenTranslator } from './screen.js';
import { createDownloads } from './download.js';

export const inject = ['connection', 'sessionController', 'sessionProjections', 'agents', 'fileUploads', 'attachments', 'llm', 'agentDefaultModel'];

export function translatorModelSelection(value) {
  const selection = {...value};
  // These are DSH provider vocabulary differences, not API endpoint changes.
  // Limit the adapter to the model verified on both configured providers.
  if (selection.model === 'deepseek-flash' && ['off', 'none'].includes(selection.reasoningEffort)) {
    if (selection.provider === 'deepseek-official') selection.reasoningEffort = 'off';
    else if (selection.provider === 'department-gateway-member') selection.reasoningEffort = 'none';
  }
  return selection;
}

export async function translationDefaultSelection(llm, current) {
  const selection = translatorModelSelection(current);
  if (typeof llm.resolveModelInfo !== 'function') return selection;
  const info = await llm.resolveModelInfo(selection.provider, selection.model);
  const efforts = info.reasoning?.efforts || [];
  const off = ['off', 'none'].find(id => efforts.some(effort => effort.id === id));
  if (off !== undefined) selection.reasoningEffort = off;
  else if (!info.reasoning) delete selection.reasoningEffort;
  return selection;
}

export function uploadedFileReference(staged) {
  if (!staged) return undefined;
  if (staged.type === 'image') throw new Error('此上传凭据是图片引用，请以文件方式重新上传');
  // Newer hosts wrap the durable file reference; older hosts return it directly.
  return staged.type === 'file' && 'attachment' in staged ? staged.attachment : staged;
}
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
const edition = manifest.translatorEdition;
const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {'content-type': 'application/json', 'cache-control': 'no-store'}});
const terminal = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
const languages = {'中文':'zh','English':'en','日本語':'ja','한국어':'ko','Français':'fr','Deutsch':'de','Español':'es','العربية':'ar'};

export async function apply(ctx) {
  const downloads=createDownloads();
  const required = ['connection.fetch.register', 'sessionController.create', 'sessionProjections.stateOf',
    'agents.get', 'llm.resolveCallConfig', 'llm.stream', 'agentDefaultModel.currentSelection'];
  const missing = required.filter(path => typeof path.split('.').reduce((value, key) => value?.[key], ctx) !== 'function');
  if (missing.length) throw new Error(`翻译插件无法启动：当前 DSH 缺少必要接口 ${missing.join('、')}。请更新翻译插件以适配此版本 DSH。`);
  let runtimePreparation;
  const prepare = () => runtimePreparation ||= prepareRuntime(root,
    join(process.env.LOCALAPPDATA || join(homedir(), '.dsh'), 'dsh-translator', 'runtimes'),
    edition, manifest.translatorRuntimeBuild ? `${manifest.version}-${manifest.translatorRuntimeBuild}` : manifest.version).catch(error => { runtimePreparation = undefined; throw error; });
  if (edition === 'cuda') prepare().catch(() => {}); // Retry with a visible task error; never break host startup.
  // One stable data root survives edition changes and uninstall/reinstall.
  const data = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'sacc-translator');
  await mkdir(data, {recursive: true});
  const settingsPath = join(data, 'settings.json');
  const defaults = {concurrency: 5, ocrQuality: 'medium', ocrWorkers: 1, preferGpu: true, gpuMemory: 'performance'};
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
  const sessionsPath = join(data, 'translation-sessions.json');
  try {
    const saved = JSON.parse(await readFile(sessionsPath, 'utf8'));
    if (Array.isArray(saved)) for (const id of saved) if (typeof id === 'string') sessions.add(id);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  let sessionsWrite = Promise.resolve();
  const saveSessions = () => {
    const snapshot = JSON.stringify([...sessions]);
    sessionsWrite = sessionsWrite.catch(() => {}).then(async () => {
      await writeFile(sessionsPath + '.tmp', snapshot);
      await rename(sessionsPath + '.tmp', sessionsPath);
    });
    return sessionsWrite;
  };
  let disposed = false;
  let admitting = false;
  let clearing = false, activeRequests = 0;
  const persist = async job => {
    if (terminal.has(job.status) && !job.completedAt) job.completedAt = new Date().toISOString();
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
  const screen = new ScreenTranslator({root,prepare,
    translate:(job,units,signal)=>modelBatch(job,units,signal,false),
    selection:sessionId=>{
      if(!sessions.has(sessionId))throw new Error('翻译会话已失效，请重新开启屏幕翻译');
      const agent=ctx.agents.get(sessionId);if(!agent)throw new Error('翻译会话已失效');
      const selected=ctx.sessionProjections.stateOf(agent.session,'modelSelection');
      return translatorModelSelection(selected?.pending??selected?.lastUsed??ctx.agentDefaultModel.currentSelection());
    }});
  for (const dir of await readdir(data, {withFileTypes: true})) {
    if (!dir.isDirectory()) continue;
    try {
      const job = JSON.parse(await readFile(join(data, dir.name, 'job.json'), 'utf8'));
      if (job.id !== dir.name) continue;
      // Migrate sessions created before the durable registry existed.
      if (typeof job.sessionId === 'string') sessions.add(job.sessionId);
      if (!terminal.has(job.status)) { job.status = 'interrupted'; job.message = '上次运行已中断，请重新提交'; await persist(job); }
      jobs.set(job.id, job);
    } catch { /* Incomplete first-write directories are not published jobs. */ }
  }
  await saveSessions();
  const publicJob = job => ({id: job.id, status: job.status, message: job.message, progress: job.progress || 0,
    createdAt: job.createdAt, completedAt: job.completedAt, edition: job.edition, text: job.text, source: job.source, target: job.target, selection: job.selection, runtime: job.runtime, result: job.result, error: job.error,
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
    screen.dispose();
    disposed = true;
    for(const stopPreview of previewStops)stopPreview();
    await Promise.all([...live.keys()].map(id => stop(id, 'interrupted')));
    await Promise.allSettled([...jobWrites.values()]);
    await settingsWrite.catch(() => {});
    await sessionsWrite.catch(() => {});
  });

  async function modelBatch(job, units, signal, saveAudit = true) {
    signal = AbortSignal.any([signal, AbortSignal.timeout(600000)]);
    return recoverTranslations(units, (subset, single) => modelRequest(job, subset, signal, saveAudit, single), signal);
  }

  async function modelRequest(job, units, signal, saveAudit, single) {
    signal.throwIfAborted();
    const selection = translatorModelSelection(job.selection);
    const {system, input} = single ? singleTranslationPrompt(job.source, job.target, units[0]) : translationPrompt(job.source, job.target, units);
    // Persist the exact model input before sending; no credentials enter the audit.
    const audit = saveAudit ? join(data, job.id, `request-${randomUUID()}.json`) : null;
    const messages = [{id: randomUUID(), role: 'user', source: {kind: 'user'}, content: [{type: 'text', text: input}]}];
    if(audit)await writeFile(audit, JSON.stringify({selection, system, messages, units, promptVersion: 3, mode: single ? 'single-string' : 'batch'}));
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
    if(audit)await writeFile(audit.replace('.json', '-raw.txt'), text);
    let result;
    try { result = single ? decodeSingleTranslation(text, units[0]) : decodeTranslations(text, units); }
    catch (error) { throw new TranslationFormatError(error.message, {cause: error}); }
    if(audit)await writeFile(audit.replace('.json', '-response.json'), JSON.stringify(result));
    return result;
  }

  async function spreadsheetPreview(path, signal) {
    if(!/\.xlsx?$/i.test(path))return path;
    const folder=join(data,'previews',randomUUID());await mkdir(folder,{recursive:true});
    const destination=join(folder,'preview.xlsx');
    const runtime=await prepare();
    if(disposed||signal.aborted)throw new Error('预览已取消');
    const worker=spawn(join(runtime,'translator-worker.exe'),[],{windowsHide:true,cwd:runtime,stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONUTF8:'1'}});
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
        const runtime = await prepare();
        if (disposed || controller.signal.aborted) throw new Error('翻译已取消');
        const worker = join(runtime, 'translator-worker.exe');
        const child = spawn(worker, [], {windowsHide: true, cwd: runtime, stdio: ['pipe','pipe','pipe'],
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
      if (request.method === 'GET') return json({edition, settings, screen:screen.status(), capabilities: {maxOcrWorkers, logicalCpus: cpus().length}, jobs: [...jobs.values()].sort((a,b) => b.createdAt.localeCompare(a.createdAt)).map(publicJob)});
      const body = await request.json();
      if(body.action==='saveStatus')return json(downloads.get(body.id));
      if(body.action==='saveArtifact'){
        if(process.platform!=='win32')return json({error:'当前系统不支持 Windows 保存窗口'},400);
        const artifact=jobs.get(body.jobId)?.artifacts?.find(a=>a.artifact_id===body.artifactId);
        if(!artifact?.output?.storage_key)return json({error:'找不到译文文件'},404);
        const folder=resolve(data,body.jobId,'files');const path=resolve(folder,artifact.output.storage_key);
        if(!path.startsWith(folder+'\\'))return json({error:'无效路径'},400);
        return json(downloads.start(path,basename(artifact.output.name)));
      }
      if(body.action==='screen'){
        if(body.operation==='disable')return json(screen.disable());
        if(!sessions.has(body.sessionId))return json({error:'翻译会话已失效，请刷新页面'},400);
        const configuration={sessionId:body.sessionId,source:languages[body.source]||String(body.source||'auto').slice(0,80),target:languages[body.target]||String(body.target||'zh').slice(0,80)};
        if(body.operation==='enable')return json(await screen.enable(configuration));
        screen.configure(configuration);
        if(body.operation==='select')return json(screen.select());
        if(body.operation==='configure')return json(screen.status());
        return json({error:'未知屏幕翻译操作'},400);
      }
      if (body.action === 'cache') return json({...await translationCache(data),historyCount:jobs.size});
      if (body.action === 'clearCache') {
        if(live.size || admitting || downloads.busy || activeRequests>1) return json({error:'请等待翻译、上传、保存或预览加载结束后清理'},409);
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
        // Initialize only this translation session; selectModel also changes the host-wide default.
        const selection = await ctx.llm.resolveCallConfig(await translationDefaultSelection(ctx.llm, ctx.agentDefaultModel.currentSelection()));
        const created = await ctx.sessionController.create({cwd: data}); sessions.add(created.sessionId);
        await saveSessions();
        ctx.agents.get(created.sessionId).session.append('model/selection', {
          provider: selection.provider, model: selection.model,
          ...(selection.reasoningEffort === undefined ? {} : {reasoningEffort: selection.reasoningEffort}),
        });
        return json(created);
      }
      if (body.action === 'selectModel') {
        if (!sessions.has(body.sessionId)) return json({error:'翻译会话已失效，请刷新页面'},400);
        const agent = ctx.agents.get(body.sessionId);
        if (!agent) return json({error:'翻译会话已失效，请刷新页面'},400);
        const requested = {provider:body.selection?.provider, model:body.selection?.model,
          ...(body.selection?.reasoningEffort === undefined ? {} : {reasoningEffort:body.selection.reasoningEffort})};
        const selection = await ctx.llm.resolveCallConfig(translatorModelSelection(requested));
        agent.session.append('model/selection', {provider:selection.provider,model:selection.model,
          ...(selection.reasoningEffort === undefined ? {} : {reasoningEffort:selection.reasoningEffort})});
        return json({ok:true});
      }
      if (body.action === 'preview') {
        if (!sessions.has(body.sessionId)) return json({error: '翻译会话已失效，请刷新页面'}, 400);
        let path;
        if (body.receiptId) {
          const ref = uploadedFileReference(await ctx.fileUploads.resolve(ctx.agents.get(body.sessionId), body.receiptId));
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
      job.selection = translatorModelSelection(selected?.pending ?? selected?.lastUsed ?? ctx.agentDefaultModel.currentSelection());
      Object.assign(job, settings);
      if (job.text.length > 100000) return json({error: '文本超过 100000 字符，请改用文件'}, 400);
      const folder = join(data, job.id, 'files'); await mkdir(folder, {recursive: true});
      if (body.receiptId) {
        const agent = ctx.agents.get(body.sessionId);
        const ref = agent && uploadedFileReference(await ctx.fileUploads.resolve(agent, body.receiptId));
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
