import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

test('chat actions use native clipboard, show stable time and report copy failures', async () => {
  let plugin, Panel, cursor=0, values=[], effects=[], refs=[], refCursor=0, copiedText, clipboardOK=true;
  const React={Component:class {},Fragment:'fragment',
    createElement:(type,props,...children)=>({type,props:props||{},children}),
    useState:initial=>{const i=cursor++;if(!(i in values))values[i]=initial;return [values[i],v=>{values[i]=v;}];},
    useRef:initial=>refs[refCursor++]|| (refs[refCursor-1]={current:initial}),
    useEffect:fn=>effects.push(fn),useMemo:fn=>fn()};
  const native={Tooltip:'native-tooltip',IconCopyOutlineRegular:'native-copy',IconCheckOutlineRegular:'native-check',IconClockOutlineRegular:'native-clock',
    writeClipboard:async text=>{copiedText=text;return clipboardOK;}};
  vm.runInNewContext(await readFile(new URL('../src/client.js',import.meta.url),'utf8'),{
    window:{__ModuleLoader__:{load:({factory})=>{plugin=factory(name=>name==='react'?React:native);}}},
    __STYLES__:'',AbortController,setTimeout,clearTimeout});
  const ctx={slots:{inject:(_name,fn)=>fn(),register:(options,component)=>{if(options.name==='main')Panel=component;}},locale:{}};
  plugin.apply(ctx);
  const all=node=>!node||typeof node!=='object'?[]:[node,...(node.children||[]).flat(Infinity).flatMap(all)];
  const job={id:'a',status:'completed',result:'译文\n第二行',createdAt:'2026-10-22T10:00:00Z',completedAt:'2026-10-22T10:01:00Z',artifacts:[]};
  const panel=()=>{cursor=0;refCursor=0;return Panel({ctx});};
  panel();values[9]='chat';values[5]=[job];
  const seat=all(panel()).find(n=>n.type?.name==='MessageActions');assert.ok(seat);
  values[5]=[{...job,status:'running'}];assert.ok(!all(panel()).some(n=>n.type?.name==='MessageActions'));
  values=[];refs=[];effects=[];
  const render=props=>{cursor=0;refCursor=0;return seat.type(props||seat.props);};
  let tree=render();const cleanup=effects[0]();
  assert.equal(all(tree).find(n=>n.type==='time').props.dateTime,job.completedAt.replace('Z','.000Z'));
  await all(tree).find(n=>n.props['aria-label']==='复制').props.onClick();
  assert.equal(copiedText,job.result);
  tree=render();assert.ok(all(tree).some(n=>n.type==='native-check'));
  cleanup();values=[];refs=[];effects=[];clipboardOK=false;
  const documentJob={...job,result:undefined,completedAt:undefined,message:'翻译完成',artifacts:[{name:'译文.pdf',message:'已保存',warnings:['请复核']}]};
  tree=render({job:documentJob});const cleanup2=effects[0]();
  assert.ok(all(tree).some(n=>n.props['aria-label']?.startsWith('创建时间：')));
  await all(tree).find(n=>n.props['aria-label']==='复制').props.onClick();
  assert.equal(copiedText,'翻译完成\n\n译文.pdf\n已保存\n请复核');
  tree=render({job:documentJob});assert.ok(all(tree).some(n=>n.props.role==='status'&&n.children.includes('复制失败，请重试')));
  cleanup2();
});
