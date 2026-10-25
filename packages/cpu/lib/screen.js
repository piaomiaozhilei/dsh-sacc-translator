import {spawn} from 'node:child_process';
import {access} from 'node:fs/promises';
import {join} from 'node:path';
import {createInterface} from 'node:readline';

// Native helper owns pixels/OCR/UI. Only bounded recognized text crosses IPC.
export class ScreenTranslator {
  constructor({root,prepare,translate,selection,spawnProcess=spawn,platform=process.platform}) {
    Object.assign(this,{root,prepare,translate,selection,spawnProcess,platform});
    this.state={enabled:false,running:false,message:'屏幕翻译未开启'};
    this.child=null;this.starting=null;this.controller=null;this.disposed=false;this.configuration=null;this.generation=0;
  }
  status(){return {...this.state};}
  configure(configuration){this.configuration={...configuration};}
  send(command){if(this.child?.stdin.writable)this.child.stdin.write(JSON.stringify(command)+'\n');}
  async enable(configuration){
    if(this.disposed)throw new Error('插件已停止');
    this.configure(configuration);
    if(this.state.enabled)return this.status();
    if(this.starting)return this.starting;
    this.starting=this.start().finally(()=>{this.starting=null;});return this.starting;
  }
  async start(){
    const generation=this.generation;
    if(this.platform!=='win32')throw new Error('屏幕翻译目前仅支持 Windows');
    const executable=join(this.root,'native','sacc-screen-translator.exe');
    try{await access(executable);}catch{throw new Error('缺少 Rust 屏幕翻译组件，请安装包含原生组件的新版插件');}
    const runtime=await this.prepare();
    if(this.disposed||generation!==this.generation)throw new Error('屏幕翻译启动已取消');
    return new Promise((resolve,reject)=>{
      const child=this.spawnProcess(executable,[runtime],{windowsHide:true,stdio:['pipe','pipe','pipe']});this.child=child;
      let ready=false,settled=false;
      const startup=setTimeout(()=>{reject(new Error('屏幕翻译启动超时'));child.kill();},15000);
      const lines=createInterface({input:child.stdout});let stderr='';
      child.stderr.on('data',chunk=>{stderr=(stderr+chunk).slice(-1500);});
      child.stdin.on('error',()=>{});
      lines.on('line',line=>{
        if(this.child!==child)return;
        try{
          if(line.length>2_000_000)throw new Error('屏幕翻译响应过大');
          const event=JSON.parse(line);
          if(event.type==='ready'){
            if(event.protocol!==1)throw new Error('屏幕翻译组件版本不兼容');
            clearTimeout(startup);ready=true;settled=true;this.state={enabled:true,running:false,message:'Ctrl+Shift+S 框选翻译'};resolve(this.status());
          }else if(event.type==='busy'){
            this.activeId=null;this.controller?.abort();this.state={...this.state,running:true,message:'正在识别选区文字…',error:null};
            clearTimeout(this.ocrTimeout);this.ocrTimeout=setTimeout(()=>{this.disable();this.state.error='屏幕识别超时，请重新开启';this.state.message=this.state.error;},180000);
          }else if(event.type==='recognized'){
            clearTimeout(this.ocrTimeout);void this.onRecognized(event).catch(error=>this.fail(event.id,error));
          }else if(event.type==='closed'){
            clearTimeout(this.ocrTimeout);this.activeId=null;this.controller?.abort();this.controller=null;this.state.running=false;this.state.message='Ctrl+Shift+S 框选翻译';
          }else if(event.type==='completed'){
            this.state.running=false;this.state.message='翻译完成，Esc 关闭浮层';
          }else if(event.type==='error'){
            clearTimeout(this.ocrTimeout);this.state.running=false;this.state.error=String(event.error||'屏幕翻译失败');this.state.message=this.state.error;
            if(!ready){clearTimeout(startup);settled=true;reject(new Error(this.state.error));child.kill();}
          }else if(event.type==='metrics'){this.state.ocrMs=event.ocrMs;}
        }catch(error){clearTimeout(startup);if(!settled){settled=true;reject(error);}this.state.error=error.message;child.kill();}
      });
      child.on('error',error=>{clearTimeout(startup);if(!settled){settled=true;reject(error);}this.state.error=error.message;});
      child.on('close',()=>{
        clearTimeout(startup);lines.close();
        if(this.child===child){clearTimeout(this.ocrTimeout);this.controller?.abort();this.child=null;this.state.enabled=false;this.state.running=false;}
        if(!settled){settled=true;reject(new Error(stderr||this.state.error||'屏幕翻译组件退出'));}
      });
    });
  }
  fail(id,error){
    if(error.name==='AbortError'||id!==this.activeId||this.controller?.signal.aborted)return;
    this.state.running=false;this.state.error=error.message;this.state.message=error.message;
    this.send({type:'error',id,error:error.message});
  }
  async onRecognized(event){
    this.activeId=event.id;
    this.controller?.abort();const controller=new AbortController();this.controller=controller;
    if(!Number.isSafeInteger(event.id)||!Array.isArray(event.segments)||!event.segments.length||event.segments.length>300)throw new Error('无效的屏幕识别结果');
    const ids=new Set();let length=0;
    const segments=event.segments.map(item=>{
      if(typeof item.id!=='string'||ids.has(item.id)||typeof item.text!=='string'||item.text.length>10000||!['left','top','width','height'].every(key=>Number.isFinite(item[key])))throw new Error('无效的屏幕文字区域');
      ids.add(item.id);length+=item.text.length;return {...item};
    });
    if(length>100000)throw new Error('选区文字过多，请缩小选区');
    this.state.running=true;this.state.message='正在翻译选区文字…';this.state.error=null;
    const configuration={...this.configuration};
    const selected=await this.selection(configuration.sessionId);
    const job={...configuration,selection:selected};
    let offset=0;
    while(offset<segments.length){
      controller.signal.throwIfAborted();let chars=0;const batch=[];
      while(offset<segments.length&&batch.length<24&&(chars<6000||!batch.length)){const item=segments[offset++];batch.push(item);chars+=item.text.length;}
      let missing=batch;const result={};
      for(let attempt=0;attempt<3&&missing.length;attempt++){
        const values=await this.translate(job,missing,controller.signal);Object.assign(result,values);
        missing=batch.filter(item=>typeof result[item.id]!=='string'||!result[item.id].trim());
      }
      if(missing.length)throw new Error('部分屏幕文字未返回译文，请重新翻译');
      for(const item of batch)item.translation=result[item.id];
    }
    controller.signal.throwIfAborted();this.send({type:'translated',id:event.id,segments});
  }
  select(){if(!this.state.enabled)throw new Error('请先开启屏幕翻译');this.send({type:'select'});return this.status();}
  disable(){
    this.generation++;this.activeId=null;
    clearTimeout(this.ocrTimeout);this.controller?.abort();this.controller=null;
    const child=this.child;this.child=null;this.state={enabled:false,running:false,message:'屏幕翻译已关闭'};
    if(child){child.stdin.end(JSON.stringify({type:'shutdown'})+'\n');const timer=setTimeout(()=>child.kill(),2000);timer.unref?.();child.once('close',()=>clearTimeout(timer));}
    return this.status();
  }
  dispose(){this.disposed=true;this.disable();}
}
