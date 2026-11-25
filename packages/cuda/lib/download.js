import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';

export function createDownloads(launch=spawn) {
  const states=new Map();let active;
  return {
    get busy(){return Boolean(active);},
    start(source,name){
      if(active)return {id:active};
      // This method passes a pathname only. The native helper opens it after confirmation.
      const id=randomUUID();const state={status:'choosing'};states.set(id,state);active=id;
      const child=launch(fileURLToPath(new URL('./save-translation.exe',import.meta.url)),[source,name],{windowsHide:true,stdio:['ignore','pipe','pipe']});
      const lines=createInterface({input:child.stdout});
      lines.on('line',line=>{try{const update=JSON.parse(line);if(['choosing','saving','saved','cancelled','failed'].includes(update.status))Object.assign(state,update);}catch{}});
      child.stderr.resume();
      const finish=()=>{if(active===id)active=undefined;const timer=setTimeout(()=>states.delete(id),3600000);timer.unref();};
      child.once('error',error=>{Object.assign(state,{status:'failed',error:'保存组件无法启动：'+error.message});finish();});
      child.once('close',()=>{if(!['saved','cancelled','failed'].includes(state.status))Object.assign(state,{status:'failed',error:'保存窗口意外关闭，请重试'});finish();});
      return {id};
    },
    get(id){const state=states.get(id);if(!state)throw Error('保存操作不存在，请重新点击下载');return {...state};}
  };
}
