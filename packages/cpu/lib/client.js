window.__ModuleLoader__.load({id: 'dsh-sacc-translator-cpu', factory: require => {
  const React = require('react');
  const {Tooltip, writeClipboard, IconCopyOutlineRegular, IconCheckOutlineRegular, IconClockOutlineRegular} = require('@deepseek-ai/dsh-client-ui-primitives');
  const {useState, useEffect, useRef} = React;
  const h = React.createElement;
  const route = '/api/sacc-translator';
  const send = (input, init) => (globalThis.__DSH_TRANSPORT__?.fetch || globalThis.fetch.bind(globalThis))(input, init);
  async function api(body, signal) {
    const response = await send(route, body ? {method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body), signal} : {signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  async function wave(blob) {
    const audio = new AudioContext();
    try {
      const decoded = await audio.decodeAudioData(await blob.arrayBuffer());
      if (decoded.duration > 120) throw new Error('单次录音请控制在两分钟以内');
      const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * 16000), 16000);
      const source = offline.createBufferSource(); source.buffer = decoded; source.connect(offline.destination); source.start();
      const samples = (await offline.startRendering()).getChannelData(0);
      const bytes = new Uint8Array(44 + samples.length * 2); const view = new DataView(bytes.buffer);
      const word = (offset, value) => [...value].forEach((c,i) => view.setUint8(offset+i,c.charCodeAt(0)));
      word(0,'RIFF'); view.setUint32(4,bytes.length-8,true); word(8,'WAVE'); word(12,'fmt ');
      view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true); view.setUint32(24,16000,true);
      view.setUint32(28,32000,true); view.setUint16(32,2,true); view.setUint16(34,16,true); word(36,'data'); view.setUint32(40,bytes.length-44,true);
      samples.forEach((x,i) => view.setInt16(44+i*2, Math.round(Math.max(-1,Math.min(1,x))*32767),true));
      let binary = ''; for (let i=0;i<bytes.length;i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192));
      return btoa(binary);
    } finally { await audio.close(); }
  }
  const css = ".sacc-translator{--st-bg:var(--dsw-alias-bg-base,#171719);--st-surface:var(--dsw-specific-input-major,#29292c);--st-soft:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.12));--st-line:var(--dsw-alias-border-l2,rgba(128,128,128,.2));--st-text:var(--dsw-alias-label-primary,#e9e9ec);--st-muted:var(--dsw-alias-label-secondary,#b1b1b8);--st-accent:var(--dsw-alias-state-business-primary,#8399ee);height:100%;overflow:auto;background:var(--st-bg);color:var(--st-text);font-family:var(--dsw-font-family,system-ui,sans-serif);font-size:14px;container-type:inline-size;container-name:st-page;line-height:1.6}\n.sacc-translator *{box-sizing:border-box}.sacc-translator .wrap{max-width:1120px;margin:auto;padding:36px 40px 24px}.sacc-translator .topbar{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:36px}.sacc-translator .brand{display:flex;gap:14px;align-items:center}.sacc-translator .brand-icon{width:44px;height:44px;border-radius:14px;background:var(--st-soft);display:grid;place-items:center;color:var(--st-accent)}.sacc-translator h1{font-size:22px;line-height:1.35;font-weight:600;margin:0}.sacc-translator .subtitle{color:var(--st-muted);font-size:12px;margin:3px 0 0}.sacc-translator .tabs{display:flex;position:relative;padding:4px;background:var(--st-soft);border-radius:12px;isolation:isolate}.sacc-translator .tab-indicator{position:absolute;left:4px;top:4px;bottom:4px;width:calc(50% - 4px);border-radius:9px;background:var(--st-surface);box-shadow:0 2px 5px #00000012;z-index:-1;transition:transform 280ms cubic-bezier(.22,1,.36,1)}.sacc-translator .tabs[data-mode=chat] .tab-indicator{transform:translateX(100%)}\n.sacc-translator .st-button{border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;border-radius:10px;min-height:40px;padding:8px 12px;display:inline-flex;align-items:center;justify-content:center;gap:7px;transition:background 150ms,transform 150ms,opacity 150ms}.sacc-translator .st-button:hover:not(:disabled){background:var(--st-soft)}.sacc-translator .st-button:active:not(:disabled){transform:scale(.97)}.sacc-translator .st-button:disabled{opacity:.4;cursor:default}.sacc-translator :focus-visible{outline:2px solid var(--st-accent);outline-offset:3px}.sacc-translator .tabs .st-button{min-width:90px;font-size:13px;color:var(--st-muted)}.sacc-translator .tabs [aria-selected=true]{color:var(--st-text)}.sacc-translator .icon-button{width:40px;padding:8px;flex-shrink:0}.sacc-translator svg{flex-shrink:0}.sacc-translator .language-bar{display:flex;align-items:center;gap:14px;margin-bottom:20px}.sacc-translator .language{display:flex;gap:12px;align-items:center;color:var(--st-muted);font-size:12px}.sacc-translator select{font:inherit;font-size:14px;color:var(--st-text);background:var(--st-surface);border:1px solid var(--st-line);border-radius:10px;padding:10px 30px 10px 12px;cursor:pointer;max-width:180px}.sacc-translator .workspace-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:stretch}.sacc-translator .editor,.sacc-translator .output{min-width:0;border:1px solid var(--st-line);border-radius:22px;background:var(--st-surface);display:flex;flex-direction:column;overflow:visible}.sacc-translator .card-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 22px 0;color:var(--st-muted);font-size:12px;letter-spacing:.03em}.sacc-translator textarea{width:100%;border:0;background:transparent;color:var(--st-text);font:inherit;font-size:16px;line-height:1.8;min-height:245px;padding:16px 22px;resize:vertical;outline-offset:-4px}.sacc-translator textarea::placeholder{color:var(--st-muted);opacity:.75}.sacc-translator .editor.is-dragging{outline:2px dashed var(--st-accent);outline-offset:4px}.sacc-translator .editor-footer{display:flex;align-items:center;gap:6px;padding:10px 12px;flex-wrap:wrap;margin-top:auto;container-type:inline-size}.sacc-translator .model-seat{margin-left:auto;max-width:100%;min-width:0}.sacc-translator .send-button{background:var(--dsw-alias-button-info-fill,#647cd1);color:white;border-radius:50%;width:40px;padding:9px}.sacc-translator .send-button:hover:not(:disabled){background:var(--dsw-alias-button-info-hover,#738be0)}.sacc-translator .attachment{display:flex;align-items:center;gap:12px;padding:12px;margin:16px 20px;border:1px solid var(--st-line);border-radius:12px;background:var(--st-soft);min-width:0}.sacc-translator .attachment-copy{min-width:0;flex:1}.sacc-translator .attachment strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:500}.sacc-translator small{font-size:12px;color:var(--st-muted)}.sacc-translator .empty-output{flex:1;min-height:260px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:32px;color:var(--st-muted);gap:12px}.sacc-translator .empty-output svg{opacity:.5}.sacc-translator .empty-output p{margin:0;font-size:13px}.sacc-translator .result{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;font-size:16px;line-height:1.85;margin:0;padding:20px 22px;max-height:540px;overflow:auto}.sacc-translator .result-body{padding:16px 22px}.sacc-translator .result-body .result{padding:8px 0}.sacc-translator .result-actions{display:flex;justify-content:flex-end;padding:8px 12px;margin-top:auto}.sacc-translator .statusline{min-height:32px;padding:8px 2px;color:var(--st-muted);font-size:12px;display:flex;gap:10px;align-items:center}.sacc-translator .error{color:var(--dsw-alias-state-error-primary,#ef9999);background:var(--st-soft);border-radius:12px;padding:12px 16px;margin:12px 0;white-space:pre-wrap}.sacc-translator .progress-track{height:4px;border-radius:4px;background:var(--st-soft);overflow:hidden;margin:14px 0}.sacc-translator .progress-fill{height:100%;background:var(--st-accent);transform-origin:left;transition:transform 400ms ease}.sacc-translator .pulse{width:7px;height:7px;border-radius:50%;background:var(--st-accent);animation:st-pulse 1.4s ease-in-out infinite}.sacc-translator .history{margin-top:32px;border-top:1px solid var(--st-line);padding-top:20px}.sacc-translator .section-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.sacc-translator h2{font-size:14px;font-weight:500;margin:0}.sacc-translator .history-row{width:100%;text-align:left;justify-content:flex-start;padding:12px 10px;gap:14px}.sacc-translator .history-row[aria-pressed=true]{background:var(--st-soft)}.sacc-translator .history-copy{flex:1;min-width:0}.sacc-translator .history-title{display:block;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;max-width:65cqw}.sacc-translator .badge{font-size:11px;color:var(--st-muted);padding:2px 8px;border:1px solid var(--st-line);border-radius:20px;white-space:nowrap}.sacc-translator .view{animation:st-enter 260ms cubic-bezier(.22,1,.36,1)}.sacc-translator .chat-view{max-width:800px;margin:auto}.sacc-translator .chat-feed{display:flex;flex-direction:column;gap:28px;min-height:190px;margin:24px 0 32px}.sacc-translator .chat-pair{animation:st-enter 220ms ease-out}.sacc-translator .user-message{margin-left:auto;max-width:85%;width:fit-content;border-radius:18px 18px 5px 18px;background:var(--st-soft);padding:14px 20px;white-space:pre-wrap;overflow-wrap:anywhere}.sacc-translator .assistant-message{margin-top:18px;display:flex;gap:14px}.sacc-translator .assistant-message>svg{color:var(--st-accent);margin-top:6px}.sacc-translator .assistant-content{flex:1;min-width:0}.sacc-translator .assistant-content .result-body{padding:0}.sacc-translator .chat-view .editor{position:sticky;bottom:12px;box-shadow:0 8px 28px #00000015}.sacc-translator .chat-view textarea{min-height:100px;max-height:260px}.sacc-translator .chat-empty{text-align:center;padding:28px 16px;color:var(--st-muted)}.sacc-translator .chat-empty h2{font-size:24px;color:var(--st-text);margin:12px}.sacc-translator .recording{color:var(--dsw-alias-state-error-primary,#ef9999)}.sacc-translator details{font-size:12px;color:var(--st-muted);margin-top:12px}.sacc-translator summary{cursor:pointer}.sacc-translator .hint{font-size:12px;color:var(--st-muted);text-align:center;margin:12px 0}\n@keyframes st-enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}@keyframes st-pulse{50%{opacity:.3;transform:scale(.8)}}\n@container st-page (max-width:780px){.sacc-translator .wrap{padding:24px 20px}.sacc-translator .workspace-grid{grid-template-columns:1fr}.sacc-translator .topbar{margin-bottom:24px}.sacc-translator .language{gap:6px}.sacc-translator .language-bar{gap:6px}.sacc-translator .empty-output{min-height:160px}.sacc-translator textarea{min-height:200px}}\n@container st-page (max-width:480px){.sacc-translator .topbar{align-items:flex-start;flex-direction:column;gap:16px}.sacc-translator .language{flex-direction:column;align-items:flex-start}.sacc-translator select{max-width:135px}.sacc-translator .brand-icon{display:none}.sacc-translator .wrap{padding:20px 14px}.sacc-translator .model-seat{order:3;width:100%}}\n@media(prefers-reduced-motion:reduce){.sacc-translator *{animation:none!important;transition:none!important;scroll-behavior:auto!important}}\n\n.sacc-translator .model-seat{flex:1}.sacc-translator .model-seat button[aria-haspopup]{max-width:100%}\n.sacc-translator .view-toolbar{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:20px;flex-wrap:wrap}.sacc-translator .view-toolbar .language-bar{margin:0}.sacc-translator .view-toolbar .tabs{margin-left:auto}.sacc-translator .settings-dialog{width:min(420px,calc(100vw - 32px));max-height:calc(100vh - 40px);overflow:auto;border:1px solid var(--st-line);border-radius:20px;padding:22px;background:var(--st-surface);color:var(--st-text);box-shadow:0 20px 80px #0005}.sacc-translator .settings-dialog[open]{animation:st-enter 200ms ease-out}.sacc-translator .settings-dialog::backdrop{background:#0006;backdrop-filter:blur(3px)}.sacc-translator .settings-dialog h2{font-size:17px;font-weight:600}.sacc-translator .settings-field{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-top:22px}.sacc-translator .settings-field input{width:90px;border:1px solid var(--st-line);background:var(--st-bg);color:var(--st-text);border-radius:9px;padding:10px 12px;font:inherit}.sacc-translator .settings-help{color:var(--st-muted);font-size:12px;line-height:1.8;margin:14px 0 24px}.sacc-translator .settings-actions{display:flex;justify-content:flex-end;gap:8px}.sacc-translator .settings-save{background:var(--dsw-alias-button-info-fill,#647cd1);color:white;padding-inline:24px}.sacc-translator .settings-save:hover:not(:disabled){background:var(--dsw-alias-button-info-hover,#738be0)}\n@container st-page (max-width:480px){.sacc-translator .topbar{flex-direction:row;align-items:center}.sacc-translator .view-toolbar{gap:16px}.sacc-translator .view-toolbar .tabs{margin-left:auto}}\n\n.sacc-translator .settings-dialog{width:min(560px,calc(100vw - 32px))}.sacc-translator .settings-field select{max-width:260px;border:1px solid var(--st-line);border-radius:9px;padding:9px;background:var(--st-bg);color:inherit;font:inherit}.sacc-translator .settings-field input[role=switch]{width:38px;height:22px;accent-color:var(--st-accent);cursor:pointer}.sacc-translator .settings-divider{border-top:1px solid var(--st-line);margin:22px 0}.sacc-translator .settings-field :disabled{opacity:.5}\n\n.sacc-translator .result-body .attachment{display:grid;grid-template-columns:20px minmax(0,1fr);gap:10px;margin-inline:0}.sacc-translator .result-body .attachment>.st-button{grid-column:2;justify-self:start;min-height:32px;padding:5px 9px}.sacc-translator .attachment-copy{overflow-wrap:anywhere}.sacc-translator .settings-field input[role=switch]{padding:0}\n\n.sacc-translator .preview-dialog{width:min(calc(100vw - 48px),calc((100dvh - 88px) * 1.41));height:auto;aspect-ratio:1.41 / 1;max-width:none;max-height:calc(100dvh - 88px);padding:0;border:1px solid var(--st-line);border-radius:18px;background:var(--st-bg);color:var(--st-text);overflow:hidden;box-shadow:0 24px 90px #0007}\n.sacc-translator .preview-dialog[open]{display:flex;flex-direction:column;animation:st-enter 200ms ease-out}.sacc-translator .preview-dialog::backdrop{background:#0008;backdrop-filter:blur(4px)}\n.sacc-translator .preview-header{display:flex;align-items:center;gap:20px;padding:14px 20px;border-bottom:1px solid var(--st-line);flex:none}.sacc-translator .preview-title{min-width:0;flex:1}.sacc-translator .preview-title h2{margin:0 0 4px;font-size:16px}.sacc-translator .preview-title small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sacc-translator .preview-modes{display:flex;background:var(--st-soft);border-radius:10px;padding:3px}.sacc-translator .preview-modes .st-button{min-height:32px;font-size:12px}.sacc-translator .preview-modes [aria-pressed=true]{background:var(--st-surface);box-shadow:0 1px 4px #0003;color:var(--st-accent)}\n.sacc-translator .preview-columns{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.sacc-translator .preview-columns:not([data-mode=both]){grid-template-columns:minmax(0,1fr)}.sacc-translator .preview-pane{min-width:0;min-height:0;display:flex;flex-direction:column}.sacc-translator .preview-pane[hidden]{display:none}.sacc-translator .preview-pane+.preview-pane{border-left:1px solid var(--st-line)}.sacc-translator .preview-pane-label{padding:10px 16px;font-size:12px;font-weight:600;color:var(--st-muted);border-bottom:1px solid var(--st-line);background:var(--st-surface)}.sacc-translator .preview-native,.sacc-translator .native-document{flex:1;min-width:0;min-height:0;height:100%;display:flex;flex-direction:column;position:relative;overflow:auto}.sacc-translator .preview-loading{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:12px;color:var(--st-muted)}\n@media(max-width:700px){.sacc-translator .preview-dialog{width:min(calc(100vw - 16px),calc((100dvh - 64px) * 1.41));height:auto}.sacc-translator .preview-header{padding:10px;gap:8px;flex-wrap:wrap}.sacc-translator .preview-title{min-width:140px}.sacc-translator .preview-columns[data-mode=both]{overflow-x:auto;grid-template-columns: minmax(300px,1fr) minmax(300px,1fr)}}\n\n.sacc-translator .result-body .attachment{grid-template-columns:20px minmax(0,1fr) auto;align-items:center}.sacc-translator .artifact-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;align-self:center}.sacc-translator .artifact-actions .icon-button{width:40px;height:40px}.sacc-translator .attachment-copy p{margin-block:10px}\n@container st-page (max-width:480px){.sacc-translator .result-body .attachment{grid-template-columns:20px minmax(0,1fr)}.sacc-translator .artifact-actions{grid-column:2;justify-self:end}}\n\n.sacc-translator .cache-heading{display:flex;align-items:center;gap:12px}.sacc-translator .cache-heading strong{margin-left:auto;font-variant-numeric:tabular-nums}.sacc-translator .cache-meta{font-size:12px;color:var(--st-muted)}.sacc-translator .cache-clear{border:1px solid var(--st-line);margin-bottom:20px}.sacc-translator .cache-clear:hover:not(:disabled){color:#df7777}\n\n.sacc-translator .validation-warnings{margin-top:10px;color:var(--st-muted);font-size:12px}.sacc-translator .validation-warnings summary{cursor:pointer;padding-block:6px}.sacc-translator .validation-warnings[open] summary{color:var(--st-text)}.sacc-translator .validation-warnings p{line-height:1.6;overflow-wrap:anywhere}\n.sacc-translator #sacc-source:focus,.sacc-translator #sacc-source:focus-visible{outline:none;box-shadow:none}\n.sacc-translator .message-actions{display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--dsw-alias-label-tertiary,var(--st-muted))}.sacc-translator .message-action{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-width:28px;height:28px;padding:6px;border:0;border-radius:var(--dsw-radius-sm,6px);background:transparent;color:inherit;cursor:pointer}.sacc-translator .message-action svg{width:17px;height:17px}.sacc-translator .message-action:hover{background:var(--dsw-alias-interactive-bg-hover,var(--st-soft));color:var(--st-text)}.sacc-translator .message-action:focus-visible{outline:2px solid var(--st-accent);outline-offset:2px}.sacc-translator .message-action:disabled{opacity:.4;cursor:default}.sacc-translator .message-time{font-size:12px;font-variant-numeric:tabular-nums}.sacc-translator .message-copy-error{font-size:12px;color:var(--dsw-alias-state-error-primary,#df7777)}.sacc-translator .message-action-status{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}\n";
  let draft = {};
  function Panel({ctx}) {
    const [text,setText] = useState(draft.text||''), [source,setSource] = useState(draft.source||'auto'), [target,setTarget] = useState(draft.target||'中文');
    const [session,setSession] = useState(''), [file,setFile] = useState(draft.file||null), [jobs,setJobs] = useState([]);
    const [error,setError] = useState(''), [busy,setBusy] = useState(false), [phase,setPhase] = useState('');
    const [mode,setMode] = useState(draft.mode||'workspace'), [selected,setSelected] = useState(draft.selected||null), [dragging,setDragging] = useState(false);
    const [reference,setReference] = useState(null); const fileInput = useRef(null);
    const [recording,setRecording] = useState(false); const owner = useRef({});
    const [concurrency,setConcurrency] = useState(5), [saving,setSaving] = useState(false), [settingsError,setSettingsError] = useState('');
    draft = {text,source,target,file,mode,selected};
    const settingsDialog = useRef(null), previewDialog = useRef(null);
    const [previewData,setPreviewData] = useState(null), [previewMode,setPreviewMode] = useState('both');
    const [ocrSettings,setOcrSettings] = useState({ocrQuality:'medium',ocrWorkers:1,preferGpu:true,gpuMemory:'performance'});
    const [capabilities,setCapabilities] = useState({maxOcrWorkers:1,logicalCpus:1});
    const [cache,setCache] = useState(null), [clearingCache,setClearingCache] = useState(false);
    const cacheSize = bytes=>bytes<1024?`${bytes} B`:bytes<1048576?`${(bytes/1024).toFixed(1)} KB`:bytes<1073741824?`${(bytes/1048576).toFixed(1)} MB`:`${(bytes/1073741824).toFixed(2)} GB`;
    async function refreshCache() {
      setCache(null);
      try {setCache(await api({action:'cache'},AbortSignal.any([owner.current.page.signal,AbortSignal.timeout(15000)])));}
      catch(e){setCache({error:e.message==='未知操作'?'缓存接口尚未加载，请重启 DeepSeek Harness 后重试。':e.name==='TimeoutError'?'统计超时，请点击刷新重试。':e.message});}
    }
    async function clearCache() {
      setClearingCache(true);setSettingsError('');closePreview();
      try {
        const result=await api({action:'clearCache'},owner.current.page.signal);
        setCache(result);setJobs([]);setSelected(null);setFile(null);
        setPhase(`翻译缓存已清除，释放 ${cacheSize(result.removed.bytes)}`);
      } catch(e){setSettingsError(e.message);await refreshCache();}
      finally {setClearingCache(false);}
    }
    const updateOcr = (key,value) => setOcrSettings(previous=>({...previous,[key]:value}));
    useEffect(() => {
      const controller = new AbortController(); owner.current.page = controller; let timer;
      const refresh = async () => {
        try { const result = await api(null,controller.signal); setJobs(result.jobs); }
        catch(e) { if (!controller.signal.aborted) setError(e.message); }
        if (!controller.signal.aborted) timer = setTimeout(refresh,1200);
      };
      api({action:'session'},controller.signal).then(async x=>{
        if(controller.signal.aborted)return;
        const ref=ctx.sessions.retain(x.sessionId,{source:'saccTranslator',signal:controller.signal}); owner.current.reference=ref;
        await ref.ready; if(controller.signal.aborted)return; setReference(ref);setSession(x.sessionId);
      }).catch(e=>{if(!controller.signal.aborted)setError(e.message);});
      refresh();
      return () => {controller.abort(); owner.current.preview?.abort(); owner.current.reference?.release(); clearTimeout(timer); owner.current.operation?.abort(); clearTimeout(owner.current.recordTimer);
        owner.current.discard = true; if (owner.current.recorder?.state === 'recording') owner.current.recorder.stop();
        owner.current.stream?.getTracks().forEach(track=>track.stop());};
    },[]);
    const safe = task => {setError(''); Promise.resolve().then(task).catch(e=>{if(!owner.current.page.signal.aborted)setError(e.message);});};
    async function submit() {
      setBusy(true); const controller = new AbortController(); owner.current.operation = controller;
      try {
        let receiptId;
        if (file) {
          setPhase('正在上传文件');
          const result = await ctx.fileUpload.upload(session,file,file.name,controller.signal,progress=>setPhase(`正在上传 · ${Math.round(progress.loaded/1024)} KB`));
          if(!result.ok) throw result.error; receiptId = result.value.receiptId;
        }
        setPhase('正在创建翻译任务');
        const started=await api({action:'start',sessionId:session,source,target,text,...receiptId?{receiptId}:{}},controller.signal);
        setSelected(started.id || started.job?.id || null); const fresh=await api(null,controller.signal);setJobs(fresh.jobs);
        if(mode==='chat'){setText('');setFile(null);}
        setPhase('任务已提交');
      } finally {setBusy(false);}
    }
    async function record() {
      if(recording){owner.current.recorder.stop();return;}
      const stream = await navigator.mediaDevices.getUserMedia({audio:true}); owner.current.stream=stream;
      owner.current.discard=false;
      const recorder = new MediaRecorder(stream); owner.current.recorder=recorder; const chunks=[];
      recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
      recorder.onstop=()=>{
        stream.getTracks().forEach(track=>track.stop()); clearTimeout(owner.current.recordTimer); setRecording(false);
        if(owner.current.discard)return;
        safe(async()=>{
          setBusy(true);setPhase('正在识别语音'); const controller=new AbortController();owner.current.operation=controller;
          try {const result=await api({action:'speech',audioBase64:await wave(new Blob(chunks,{type:recorder.mimeType}))},controller.signal);
            setText(previous=>previous+(previous?'\n':'')+result.text);setPhase('语音已转为文字，请检查后提交');}
          finally {setBusy(false);}
        });
      };
      recorder.start();setRecording(true);owner.current.recordTimer=setTimeout(()=>{if(recorder.state==='recording')recorder.stop();},115000);
    }
    function closePreview() { owner.current.preview?.abort(); previewDialog.current?.close(); setPreviewData(null); }
    async function preview(job, artifact) {
      owner.current.preview?.abort();
      const controller = new AbortController(); owner.current.preview=controller;
      setPreviewMode(artifact?.downloadable?'both':'original');
      setPreviewData({name:artifact?.name||file?.name||'文档',loading:true}); previewDialog.current.showModal();
      try {
        const requests = job ? [{id:job.id,original:true}, ...(artifact.downloadable?[{id:job.id,artifact:artifact.artifact_id}]:[])] : [artifact];
        const files = await Promise.all(requests.map(async payload=>{
          try {return await api({action:'preview',sessionId:session,...payload},controller.signal);}
          catch(e){if(controller.signal.aborted)throw e;return {error:e.message};}
        }));
        if(!controller.signal.aborted)setPreviewData({name:artifact?.name||file?.name||'文档',files,signal:controller.signal});
      } catch(e) {if(!controller.signal.aborted)setPreviewData({error:e.message});}
    }
    async function previewFile() {
      setBusy(true); const controller = new AbortController(); owner.current.operation = controller;
      try {
        setPhase('正在准备文档预览');
        const uploaded = await ctx.fileUpload.upload(session,file,file.name,controller.signal,()=>{});
        if (!uploaded.ok) throw uploaded.error;
        await preview(null,{receiptId:uploaded.value.receiptId}); setPhase('预览已打开');
      } finally {setBusy(false);}
    }
    async function download(job,artifact){
      const response=await send(`${route}?download=${encodeURIComponent(job.id)}&artifact=${encodeURIComponent(artifact.artifact_id)}`);
      if(!response.ok)throw new Error('下载失败，请重试');
      const url=URL.createObjectURL(await response.blob());const anchor=document.createElement('a');anchor.href=url;anchor.download=artifact.name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
    }
    const language=(label,value,set,auto)=>h('label',{className:'language'},h('span',null,label),h('select',{value,onChange:e=>set(e.target.value)},...(auto?['auto']:[]).concat(['中文','English','日本語','한국어','Français','Deutsch','Español','العربية']).map(x=>h('option',{key:x,value:x},x==='auto'?'自动识别':x))));
    const active=jobs.some(job=>job.status==='running');
    const current=jobs.find(job=>job.id===selected)||jobs[0];
    const status={running:'翻译中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
    const button=(label,icon,onClick,{iconOnly,children,...extra}={})=>h('button',{type:'button',className:'st-button',onClick,'aria-label':label,title:label,...extra},...(children || [icon&&h(Glyph,{name:icon}),iconOnly?null:label]));
    const title=job=>job.text || job.artifacts[0]?.name || '文档翻译';
    const result=job=>h('div',{className:'result-body'},
      job.status==='running'&&h(React.Fragment,null,h('div',{className:'statusline',role:'status'},h('span',{className:'pulse'}),job.message),h('div',{className:'progress-track',role:'progressbar','aria-label':'翻译进度','aria-valuemin':0,'aria-valuemax':100,'aria-valuenow':job.progress},h('div',{className:'progress-fill',style:{transform:`scaleX(${Math.max(.02,Math.min(1,job.progress/100))})`}})),button('取消翻译','stop',()=>safe(()=>api({action:'cancel',id:job.id})))),
      job.error&&h('p',{className:'error',role:'alert'},job.error),
      job.result&&h('pre',{className:'result'},job.result),
      !job.result&&job.status!=='running'&&h('small',null,job.message),
      ...job.artifacts.map(artifact=>h('div',{key:artifact.artifact_id,className:'attachment'},h(Glyph,{name:'file'}),h('div',{className:'attachment-copy'},h('strong',null,artifact.name),h('small',null,artifact.message),artifact.warnings?.length>0&&h('details',{className:'validation-warnings'},h('summary',null,`校验提示 · ${artifact.warnings.length} 项`),...artifact.warnings.map((w,i)=>h('p',{key:i},w)))),h('div',{className:'artifact-actions'},button('预览文档','eye',()=>safe(()=>preview(job,artifact)),{iconOnly:true,className:'st-button icon-button'}),artifact.downloadable&&button('下载译文','download',()=>safe(()=>download(job,artifact)),{iconOnly:true,className:'st-button icon-button'})))),
      job.runtime&&h('details',null,h('summary',null,'处理详情'),h('p',null,`OCR · ${job.runtime.actual.toUpperCase()}`),job.runtime.warning&&h('p',null,job.runtime.warning)));
    const chooseFile=next=>{if(!busy&&!active&&!recording){setFile(next);setError('');}};
    const composer=h('div',{className:`editor${dragging?' is-dragging':''}`,onDragOver:e=>{e.preventDefault();setDragging(true);},onDragLeave:e=>{if(!e.currentTarget.contains(e.relatedTarget))setDragging(false);},onDrop:e=>{e.preventDefault();setDragging(false);chooseFile(e.dataTransfer.files[0]||null);}},
      h('div',{className:'card-heading'},h('label',{htmlFor:'sacc-source'},file?'原格式文档':'原文'),h('span',null,file?'保留排版':`${text.length.toLocaleString()} / 100,000`)),
      file?h('div',{className:'attachment'},h(Glyph,{name:'file'}),h('div',{className:'attachment-copy'},h('strong',null,file.name),h('small',null,`${(file.size/1024).toFixed(1)} KB · 译文将单独保存`)),button('预览文档','eye',()=>safe(previewFile),{disabled:busy||!session,iconOnly:true,className:'st-button icon-button'}),button('移除文件','close',()=>setFile(null),{disabled:busy||active,iconOnly:true,className:'st-button icon-button'})):
      h('textarea',{id:'sacc-source',value:text,maxLength:100000,onChange:e=>setText(e.target.value),placeholder:mode==='chat'?'发送文字，开始翻译…':'输入或粘贴要翻译的文字，也可以拖入文档',onPaste:e=>{if(e.clipboardData.files.length){e.preventDefault();chooseFile(e.clipboardData.files[0]);}},onKeyDown:e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&!e.nativeEvent.isComposing&&!busy&&!active&&!recording&&session&&text.trim()){e.preventDefault();safe(submit);}}}),
      h('input',{type:'file',ref:fileInput,hidden:true,onChange:e=>{chooseFile(e.target.files[0]||null);e.target.value='';}}),
      h('div',{className:'editor-footer'},button('添加文件','plus',()=>fileInput.current?.click(),{disabled:busy||active||recording,iconOnly:true,className:'st-button icon-button'}),
        button(recording?'停止并识别':'语音输入','mic',()=>safe(record),{disabled:busy||!!file,iconOnly:true,className:`st-button icon-button${recording?' recording':''}`}),
        recording&&button('取消录音','close',()=>{owner.current.discard=true;owner.current.recorder.stop();},{iconOnly:true,className:'st-button icon-button'}),
        button('语音设置','settings',()=>ctx.get('pluginNavigation')?.openBundle('@deepseek-ai/dsh-experimental-voice-input-bundle'),{iconOnly:true,className:'st-button icon-button'}),
        h('div',{className:'model-seat'},reference?h(NativeModel,{ctx,session,locked:busy||active}):h('small',null,'正在连接模型…')),
        busy?button('取消当前操作','stop',()=>owner.current.operation?.abort(),{iconOnly:true,className:'st-button icon-button'}):button('开始翻译 · Ctrl+Enter','send',()=>safe(submit),{disabled:active||recording||!session||(!text.trim()&&!file),iconOnly:true,className:'st-button send-button'})));
    const languageBar=h('div',{className:'language-bar'},language('从',source,setSource,true),button('交换语言','swap',()=>{setSource(target);setTarget(source);},{disabled:source==='auto',iconOnly:true,className:'st-button icon-button'}),language('译为',target,setTarget,false));
    const tabs=['workspace','chat'];
    const viewTabs=h('div',{className:'tabs',role:'tablist','aria-label':'翻译视图','data-mode':mode},h('span',{className:'tab-indicator','aria-hidden':true}),...tabs.map((tab,i)=>button(i?'聊天':'工作台',i?'chat':'grid',()=>setMode(tab),{key:tab,role:'tab',id:`sacc-tab-${tab}`,'aria-controls':'sacc-view','aria-selected':mode===tab,tabIndex:mode===tab?0:-1,onKeyDown:e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'workspace':e.key==='End'?'chat':tabs[1-i];setMode(next);document.getElementById(`sacc-tab-${next}`)?.focus();}}})));
    return h('section',{className:'sacc-translator','aria-label':'翻译助手'},h('style',null,css),h('div',{className:'wrap'},
      h('header',{className:'topbar'},h('div',{className:'brand'},h('div',{className:'brand-icon'},h(Icon)),h('div',null,h('h1',null,'翻译助手'),h('p',{className:'subtitle'},'文本与文档，自然流转'))),
        button('设置','settings',()=>safe(async()=>{const result=await api(null,owner.current.page.signal);setConcurrency(result.settings.concurrency);setOcrSettings(result.settings);setCapabilities(result.capabilities);setSettingsError('');setCache(null);settingsDialog.current.showModal();await refreshCache();}),{'aria-haspopup':'dialog'})),
      h('div',{className:'view-toolbar'},languageBar,viewTabs),
      h('dialog',{ref:previewDialog,className:'preview-dialog','aria-labelledby':'sacc-preview-title',onCancel:closePreview,onClose:()=>{owner.current.preview?.abort();setPreviewData(null);}},
        h('header',{className:'preview-header'},h('div',{className:'preview-title'},h('h2',{id:'sacc-preview-title'},'文档预览'),h('small',null,previewData?.name)),
          h('div',{className:'preview-modes','aria-label':'预览布局'},...Object.entries({both:'对照',original:'仅原文',translated:'仅译文'}).map(([value,label])=>button(label,null,()=>setPreviewMode(value),{key:value,'aria-pressed':previewMode===value,disabled:previewData?.loading||(value!=='original'&&!previewData?.files?.[1])}))),
          button('关闭预览','close',closePreview,{iconOnly:true,className:'st-button icon-button'})),
        previewData?.loading?h('div',{className:'preview-loading',role:'status'},'正在准备文档…'):previewData?.error?h('p',{className:'error',role:'alert'},previewData.error):previewData&&h('div',{className:'preview-columns','data-mode':previewMode},
          ...['original','translated'].map((side,i)=>h('section',{key:side,className:'preview-pane',hidden:previewMode!=='both'&&previewMode!==side,'aria-label':i?'译文预览':'原文预览'},h('div',{className:'preview-pane-label'},i?'译文':'原文'),h('div',{className:'preview-native'},previewData.files?.[i]?h(PreviewBoundary,{key:previewData.files[i].path||side},h(NativeDocument,{ctx,session,file:previewData.files[i],signal:previewData.signal})):h('p',null,'译文尚未生成')))))),
      h('dialog',{ref:settingsDialog,className:'settings-dialog','aria-labelledby':'sacc-settings-title',onClick:e=>{if(e.target===e.currentTarget){const rect=e.currentTarget.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)e.currentTarget.close();}}},
        h('form',{onSubmit:e=>{e.preventDefault();setSaving(true);setSettingsError('');api({...ocrSettings,action:'settings',concurrency:Number(concurrency)},owner.current.page.signal).then(()=>{settingsDialog.current.close();setPhase('设置已保存，从下一个任务生效');}).catch(e=>setSettingsError(e.message)).finally(()=>setSaving(false));}},
          h('div',{className:'section-heading'},h('h2',{id:'sacc-settings-title'},'翻译设置'),button('关闭设置','close',()=>settingsDialog.current.close(),{iconOnly:true,className:'st-button icon-button'})),
          h('label',{className:'settings-field',htmlFor:'sacc-concurrency'},h('span',null,'并发数量'),h('input',{id:'sacc-concurrency',type:'number',min:1,max:8,step:1,required:true,value:concurrency,disabled:saving,'aria-describedby':'sacc-concurrency-help',onChange:e=>setConcurrency(e.target.value)})),
          h('p',{id:'sacc-concurrency-help',className:'settings-help'},'文档分块同时请求模型的数量，范围 1–8。默认 5；修改后从下一个任务生效。'),settingsError&&h('p',{className:'error',role:'alert'},settingsError),
          h('div',{className:'settings-divider'}),
          h('label',{className:'settings-field'},h('span',null,'文件 OCR 精度'),h('select',{value:ocrSettings.ocrQuality,disabled:saving,onChange:e=>updateOcr('ocrQuality',e.target.value)},...Object.entries({ultra_low:'1200px · 超低',low:'1400px · 低',medium:'1600px · 中（默认）',high:'2200px · 高'}).map(([value,label])=>h('option',{key:value,value},label)))),
          h('p',{className:'settings-help'},'用于图片和扫描 PDF 的文字检测。低档节省内存，高档适合小字、低对比度和倾斜扫描件；译文仍以高清分辨率回填。'),
          h('label',{className:'settings-field'},h('span',null,'PDF OCR 线程'),h('select',{value:Math.min(ocrSettings.ocrWorkers,capabilities.maxOcrWorkers),disabled:saving,onChange:e=>updateOcr('ocrWorkers',Number(e.target.value))},...Array.from({length:capabilities.maxOcrWorkers},(_,i)=>h('option',{key:i,value:i+1},`${i+1} 路 · 约 ${i===0?1:(i+1)*2} 个计算线程`)))),
          h('p',{className:'settings-help'},`本机最多 ${capabilities.maxOcrWorkers} 路；计算预算不超过逻辑核心的 50%，为文档渲染与系统保留余量。实际页数不足时自动减少。`),
          h('label',{className:'settings-field'},h('span',null,'文件 OCR GPU 加速'),h('input',{type:'checkbox',role:'switch',checked:'CPU'==='CUDA'&&ocrSettings.preferGpu,disabled:saving||'CPU'!=='CUDA',onChange:e=>updateOcr('preferGpu',e.target.checked)})),
          h('p',{className:'settings-help'},'CPU'==='CUDA'?'优先使用 GPU；不可用时自动回退 CPU，任务详情显示实际引擎与原因。':'当前为 CPU 版。安装并启用 CUDA 版后可使用 GPU；两版共用设置和翻译记录。'),
          h('label',{className:'settings-field'},h('span',null,'GPU 内存模式'),h('select',{value:ocrSettings.gpuMemory,disabled:saving||'CPU'!=='CUDA'||!ocrSettings.preferGpu,onChange:e=>updateOcr('gpuMemory',e.target.value)},h('option',{value:'performance'},'性能优先（默认）'),h('option',{value:'low_memory'},'低内存'))),
          h('p',{className:'settings-help'},'低内存模式按需申请显存并使用启发式算法，适合显存较小的设备。设置从下一个任务生效。'),
          h('div',{className:'settings-divider'}),
          h('div',{className:'cache-heading'},h('span',null,'翻译缓存'),h('strong',{'aria-live':'polite'},cache?.error?'统计失败':cache?cacheSize(cache.bytes):'统计中…'),button('刷新缓存大小',null,refreshCache,{disabled:clearingCache})),
          cache?.error&&h('p',{className:'error',role:'alert'},cache.error),
          cache&&!cache.error&&h('p',{className:'cache-meta'},`${cache.historyCount} 条历史 · ${cache.files} 个文件 · ${new Date(cache.measuredAt).toLocaleTimeString()} 更新`),
          h('p',{className:'settings-help'},'清除翻译历史、上传副本、译文与预览缓存，保留设置。删除后无法恢复，请先下载需要的译文。DSH 共享附件库由宿主管理，不计入此处，也不会被删除。'),
          button(clearingCache?'正在清除…':'一键清除翻译缓存',null,clearCache,{className:'st-button cache-clear',disabled:!cache||Boolean(cache.error)||clearingCache||busy||recording||saving||jobs.some(job=>['running','queued'].includes(job.status))}),
          h('div',{className:'settings-actions'},button('取消',null,()=>settingsDialog.current.close()),h('button',{type:'submit',className:'st-button settings-save',disabled:saving},saving?'正在保存…':'保存')))),
      h('div',{key:mode,id:'sacc-view',role:'tabpanel','aria-labelledby':`sacc-tab-${mode}`,className:`view ${mode==='chat'?'chat-view':''}`},
        mode==='workspace'?h(React.Fragment,null,h('div',{className:'workspace-grid'},composer,h('section',{className:'output','aria-label':'译文'},h('div',{className:'card-heading'},h('span',null,'译文'),current&&h('span',{className:'badge'},status[current.status])),current?result(current):h('div',{className:'empty-output'},h(Icon),h('p',null,'让文字跨越语言'),h('small',null,'译文与下载文件会显示在这里')),current?.result&&h('div',{className:'result-actions'},button('复制译文','copy',()=>safe(async()=>{await navigator.clipboard.writeText(current.result);setPhase('译文已复制');})))))):
        h(React.Fragment,null,h('div',{className:'chat-feed'},jobs.length?[...jobs].reverse().map(job=>h('article',{className:'chat-pair',key:job.id},h('div',{className:'user-message'},title(job)),h('div',{className:'assistant-message'},h(Icon),h('div',{className:'assistant-content'},h('small',null,`翻译助手 · ${status[job.status]}`),result(job),job.status!=='running'&&h(MessageActions,{job}))))):h('div',{className:'chat-empty'},h(Icon),h('h2',null,'换一种语言，继续表达'),h('p',null,'输入文字、上传文档，或用语音开始。'))),composer),
        h('div',{className:'statusline',role:'status','aria-live':'polite'},(busy||recording)&&h('span',{className:'pulse'}),recording?'正在录音 · 点击麦克风结束':phase),error&&h('div',{role:'alert',className:'error'},error),
        mode==='workspace'&&h('section',{className:'history'},h('div',{className:'section-heading'},h('h2',null,'最近翻译'),h('small',null,`${jobs.length} 条记录`)),jobs.length?jobs.slice(0,20).map(job=>button('',null,()=>setSelected(job.id),{key:job.id,className:'st-button history-row','aria-label':`查看翻译：${title(job).slice(0,60)}`,'aria-pressed':current?.id===job.id,children:[h(Glyph,{key:'icon',name:job.text?'chat':'file'}),h('span',{key:'title',className:'history-copy'},h('span',{className:'history-title'},title(job)),h('small',null,new Date(job.createdAt).toLocaleString())),h('span',{key:'status',className:'badge'},status[job.status])]})):h('small',null,'完成一段翻译，从这里继续。'))),
      h('p',{className:'hint'},'支持 Word、Excel、PDF 与图片 · CPU')));
  }
  class PreviewBoundary extends React.Component {
    constructor(props){super(props);this.state={error:null};}
    static getDerivedStateFromError(error){return {error:String(error.message||error)};}
    render(){return this.state.error?h('div',{className:'preview-loading',role:'alert'},'预览加载失败：'+this.state.error):this.props.children;}
  }
  // Embed the host's registered document bodies; no sidebar navigation or copied renderer.
  function NativeRenderer({ctx,session,entry,tab,ownerProps}) {
    const binding=React.useMemo(()=>{
      const store=entry.store?.create(session);
      const injected=entry.inject?.(session,store?.actions)||{};
      const props={...injected,actions:store?.actions,t:ctx.locale.bind(entry.locale)};
      if(store)props.useStore=selector=>selector(React.useSyncExternalStore(fn=>store.subscribe(fn),()=>store.getSnapshot()));
      for(const [name,source] of Object.entries(injected.hooks||{})) {
        props['use'+name[0].toUpperCase()+name.slice(1)]=selector=>selector(React.useSyncExternalStore(fn=>source.subscribe(fn),()=>source.getSnapshot()));
      }
      return props;
    },[entry,session]);
    const useTabInfo=React.useCallback(()=>({tab}),[tab]);
    const renderSlot=(name,props,options)=>{
      const child=ctx.slots.entriesOfSlot(name).find(item=>item.options.key===options?.entryKey);
      return child?h(NativeRenderer,{ctx,session,entry:child,tab,ownerProps:props}):null;
    };
    return h(entry.component,{...binding,...ownerProps,useTabInfo,renderSlot});
  }
  function NativeDocument({ctx,session,file,signal}) {
    const [state,setState]=useState(null), [attempt,setAttempt]=useState(0);
    const scrollportRef=useRef(null);
    const bindScrollport=React.useCallback(node=>{scrollportRef.current=node;},[]);
    const life=React.useMemo(()=>new AbortController(),[file.path,attempt]);
    const tab=React.useMemo(()=>{
      const encode=part=>encodeURIComponent(part).replace(/%3A/gi,':');
      const path=(file.path||'').replace(/\\/g,'/');
      return {id:crypto.randomUUID(),contentId:`dsh-resource://file/session/${encode(session)}/${path.split('/').map(encode).join('/')}`,
        signal:AbortSignal.any([signal,life.signal]),navigation:{revision:0,params:{}},actions:{bindCommands:()=>()=>{}}};
    },[session,file.path,signal,life]);
    useEffect(()=>{
      setState(null);
      if(file.error){setState({error:file.error});return;}
      const suffix=file.path.split('.').pop().toLowerCase();
      const definitions=ctx.get('documentPreviews')?.getSnapshot()||[];
      const definition=definitions.find(item=>item.extensions?.includes(suffix))||definitions.find(item=>item.id.endsWith('/text'));
      const entry=definition&&ctx.slots.entriesOfSlot('sidebar.right.tab.document').find(item=>item.options.key===definition.id);
      if(!entry){setState({error:'宿主未启用此文档的预览组件'});return;}
      const reload=()=>setAttempt(value=>value+1);
      ctx.get('resources')?.pin(tab.contentId,tab.signal);
      if(definition.loading==='renderer') {
        setState({entry,content:{kind:'renderer',revision:attempt,reload,loaded:()=>{},failed:()=>{}}});
      } else {
        ctx.get('remote').workspaceFiles.readBytes(session,file.path,{},tab.signal).then(result=>{
          if(tab.signal.aborted)return;
          if(!result.ok)throw new Error(result.error.message||result.error.code||'无法读取文档');
          const text=definition.loading==='text-pages'?new TextDecoder().decode(result.value.data):null;
          const content=text===null?{kind:'bytes',data:result.value.data}:{kind:'text',text,eof:true,pages:[{offset:1,text,lines:text.split('\n').length}]};
          setState({entry,content});
        }).catch(error=>{if(!tab.signal.aborted)setState({error:error.message});});
      }
      return ()=>life.abort();
    },[file.path,session,tab]);
    if(state?.error)return h('div',{className:'preview-loading',role:'alert'},h('p',null,state.error),h('button',{className:'st-button',onClick:()=>setAttempt(value=>value+1)},'重试'));
    if(!state)return h('div',{className:'preview-loading',role:'status'},'正在加载…');
    return h('div',{ref:scrollportRef,className:'native-document'},h(NativeRenderer,{key:tab.id,ctx,session,entry:state.entry,tab,
      ownerProps:{content:state.content,resourceAddress:tab.contentId,wrap:true,scrollportRef:bindScrollport,addResource:address=>ctx.get('resources')?.pin(address,tab.signal),setResources:()=>{}}}));
  }
  // DSH 0.2.1-rc.2 owns this slot's declaration. Reuse its registered component
  // and injection face without declaring the same slot under a second parent.
  function NativeModel({ctx,session,locked}) {
    const entry=ctx.slots.entriesOfSlot('conversation.input.model')[0];
    const injected=React.useMemo(()=>entry?.inject?.(session)||{},[entry,session]);
    if(!entry)return h('small',null,'宿主模型选择器未启用');
    const renderLabel=(name,owner,options)=>{
      const label=ctx.slots.entriesOfSlot(name)[0];
      return label?h(label.component,{...label.inject?.(session),...owner,...label.locale?{t:ctx.locale.bind(label.locale)}:{}}):options?.fallback;
    };
    return h(entry.component,{...injected,locked,t:ctx.locale.bind(entry.locale),renderSlot:renderLabel});
  }
  function MessageActions({job}) {
    const [copied,setCopied] = useState(false), [copyError,setCopyError] = useState('');
    const pending = useRef(false), lifetime = useRef(null);
    useEffect(()=>{
      const life = {active:true,timer:null}; lifetime.current = life;
      return ()=>{life.active=false;clearTimeout(life.timer);};
    },[]);
    const copyText = job.result || [job.error || job.message,
      ...(job.artifacts||[]).map(a=>[a.name,a.message,...(a.warnings||[])].filter(Boolean).join('\n'))].filter(Boolean).join('\n\n');
    const copy = async()=>{
      if(pending.current||copied)return;
      pending.current=true;setCopyError('');
      const life=lifetime.current;
      try {
        if(!await writeClipboard(copyText))throw new Error('复制失败，请重试');
        if(!life?.active)return;
        setCopied(true);life.timer=setTimeout(()=>{if(life.active)setCopied(false);},1000);
      } catch {if(life?.active)setCopyError('复制失败，请重试');}
      finally {pending.current=false;}
    };
    const date = new Date(job.completedAt||job.createdAt), validTime = Number.isFinite(date.getTime());
    const timeLabel = `${job.completedAt?'结束时间':'创建时间'}：${validTime?date.toLocaleString():''}`;
    return h('div',{className:'message-actions','aria-label':'译文消息操作'},
      h(Tooltip,{label:copied?'已复制':'复制',side:'bottom'},h('button',{type:'button',className:'message-action','aria-label':copied?'已复制':'复制',onClick:copy,disabled:!copyText},h(copied?IconCheckOutlineRegular:IconCopyOutlineRegular))),
      validTime&&h(Tooltip,{label:timeLabel,side:'bottom'},h('button',{type:'button',className:'message-action message-time','aria-label':timeLabel},h(IconClockOutlineRegular),h('time',{dateTime:date.toISOString()},date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false})))),
      h('span',{className:copyError?'message-copy-error':'message-action-status',role:'status'},copyError||(copied?'已复制':'')));
  }
  function Glyph({name}) {
    const paths={eye:'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',plus:'M12 5v14M5 12h14',mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0zM5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8',send:'M12 19V5M5 12l7-7 7 7',swap:'M4 8h15l-4-4M20 16H5l4 4',file:'M14 3H5v18h14V8zM14 3v6h5M8 13h8M8 17h5',download:'M12 3v12M7 10l5 5 5-5M4 17v4h16v-4',close:'M6 6l12 12M6 18L18 6',stop:'M6 6h12v12H6z',copy:'M8 8h12v13H8zM16 8V3H3v13h5',grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',chat:'M21 11a8 8 0 0 1-8 8H7l-5 3 2-6a8 8 0 1 1 17-5z',settings:'M4 7h16M4 17h16M8 4v6M16 14v6'};
    return h('svg',{width:18,height:18,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',strokeWidth:1.6,strokeLinecap:'round',strokeLinejoin:'round','aria-hidden':true},h('path',{d:paths[name]}));
  }

  function Icon(){return h('svg',{viewBox:'0 0 24 24',width:20,height:20,fill:'none',stroke:'currentColor',strokeWidth:1.7,'aria-hidden':true},h('path',{d:'M3 5h12M9 3v2M5 5c0 5 4 8 9 10M13 5c-1 5-4 8-10 11M13 21l4-10 4 10M15 17h4'}));}
  return {inject:['slots','fileUpload','sessions','locale','remote','remote.workspaceFiles','resources','documentPreviews'],apply(ctx){
    ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'sacc-translator',inject:()=>({ctx})},Panel));
    ctx.slots.inject('sidebar.panellist',()=>ctx.slots.register({name:'sidebar.panellist',id:'sacc-translator',order:5,label:'翻译'},Icon));
  }};
}});

