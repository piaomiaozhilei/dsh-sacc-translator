// Exercise the exact DSH parser worker with the prepared preview file.
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source=await readFile(new URL('../.reference/dsh-client-ui-sidebar-documentpreview/lib/client.excel.js',import.meta.url),'utf8');
const marker='var _dsh_excel_worker_source_default = ';
const start=source.indexOf(marker)+marker.length;
assert.ok(start>=marker.length);
const literal=source.slice(start,source.indexOf(';\n',start)).trim();
const workerSource=vm.runInNewContext(literal);
const bytes=new Uint8Array(await readFile(process.argv[2]));
const parsed=await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(new Error('native parser timed out')),20000);
  const context={TextDecoder,TextEncoder,Uint8Array,ArrayBuffer,DataView,setTimeout,clearTimeout,setImmediate,clearImmediate,console,atob,btoa,
    postMessage:value=>{if(typeof value?.ok==='boolean'){clearTimeout(timer);resolve(value);}}};
  context.self=context;
  try {
    vm.runInNewContext(workerSource,context,{timeout:15000});
    context.onmessage({data:{bytes,format:'xlsx',limits:{maxBytes:16*1024*1024,maxCells:250000,timeoutMs:15000}}});
  } catch(error){clearTimeout(timer);reject(error);}
});
assert.equal(parsed.ok,true,JSON.stringify(parsed));
console.log('DSH native parser PASS',parsed.value.sheets.map(s=>({name:s.name,rows:s.row,columns:s.column})));
