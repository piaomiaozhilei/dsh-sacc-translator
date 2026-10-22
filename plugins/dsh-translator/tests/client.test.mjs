import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

test('view switch preserves draft and attachment; native model component receives host session', async () => {
  const values=[];let cursor=0, plugin, Panel;
  const React={Component:class {},createElement:(type,props,...children)=>({type,props:props||{},children}),
    useState:initial=>{const i=cursor++;if(!(i in values))values[i]=initial;return [values[i],v=>{values[i]=typeof v==='function'?v(values[i]):v;}];},
    useEffect:()=>{},useRef:initial=>({current:initial}),useMemo:fn=>fn(),Fragment:'fragment'};
  const source=await readFile(new URL('../src/client.js',import.meta.url),'utf8');
  vm.runInNewContext(source,{window:{__ModuleLoader__:{load:({factory})=>{plugin=factory(()=>React);}}},__STYLES__:'',AbortController});
  const native=()=>null;let injectedSession;
  const ctx={slots:{inject:(_name,fn)=>fn(),register:(options,component)=>{if(options.name==='main')Panel=component;},
    entriesOfSlot:()=>[{component:native,locale:'model',inject:session=>{injectedSession=session;return {directory:'host-directory'};}}]},locale:{bind:()=>()=>''}};
  plugin.apply(ctx);
  const render=()=>{cursor=0;return Panel({ctx});};
  const all=node=>!node||typeof node!=='object'?[]:[node,...(node.children||[]).flat(Infinity).flatMap(all)];
  const find=(tree,predicate)=>all(tree).find(predicate);
  let tree=render();
  find(tree,n=>n.type==='textarea').props.onChange({target:{value:'Preserve this draft'}});
  find(tree,n=>n.type==='input'&&n.props.type==='file').props.onChange({target:{files:[{name:'sample.docx',size:2048}],value:'sample.docx'}});
  tree=render();
  find(tree,n=>n.props.id==='sacc-tab-chat').props.onClick();
  tree=render();
  assert.equal(find(tree,n=>n.props.id==='sacc-view').props['aria-labelledby'],'sacc-tab-chat');
  assert.ok(all(tree).some(n=>n.children.includes('sample.docx')));
  find(tree,n=>n.props['aria-label']==='移除文件').props.onClick();
  tree=render();assert.equal(find(tree,n=>n.type==='textarea').props.value,'Preserve this draft');
  find(tree,n=>n.props.id==='sacc-tab-workspace').props.onClick();
  tree=render();assert.equal(find(tree,n=>n.type==='textarea').props.value,'Preserve this draft');
  // Session readiness comes from the retained host SessionReference.
  values[3]='translation-session';values[12]={};tree=render();
  const nativeSeat=find(tree,n=>typeof n.type==='function'&&n.type.name==='NativeModel');
  const rendered=nativeSeat.type(nativeSeat.props);
  assert.equal(rendered.type,native);assert.equal(injectedSession,'translation-session');
  assert.equal(rendered.props.directory,'host-directory');assert.equal(rendered.props.locked,false);
  values[5]=[{id:'document-job',status:'completed',createdAt:new Date().toISOString(),artifacts:[{artifact_id:'a',name:'sample.pdf',downloadable:true,warnings:['可能缺少参数: AB12','OCR 请复核']}]}];
  values[17]={name:'sample.pdf',files:[{path:'original.pdf'},{path:'translated.pdf'}],signal:new AbortController().signal};
  values[18]='both';tree=render();
  assert.equal(all(tree).filter(n=>n.props['aria-label']==='预览文档').length,1);
  const warnings=find(tree,n=>n.type==='details'&&n.props.className==='validation-warnings');
  assert.ok(warnings);assert.ok(!warnings.props.open);
  assert.ok(find(warnings,n=>n.type==='summary').children.includes('校验提示 · 2 项'));
  const actions=find(tree,n=>n.props.className==='artifact-actions');
  assert.ok(find(actions,n=>n.props['aria-label']==='预览文档'));
  assert.ok(find(actions,n=>n.props['aria-label']==='下载译文'));
  assert.equal(all(tree).filter(n=>['预览原文','预览译文'].includes(n.props['aria-label'])).length,0);
  assert.equal(find(tree,n=>n.props.className==='preview-columns').props['data-mode'],'both');
  find(tree,n=>n.props['aria-label']==='仅译文').props.onClick();tree=render();
  assert.equal(find(tree,n=>n.props['aria-label']==='原文预览').props.hidden,true);
  assert.equal(find(tree,n=>n.props['aria-label']==='译文预览').props.hidden,false);
  find(tree,n=>n.props['aria-label']==='仅原文').props.onClick();tree=render();
  assert.equal(find(tree,n=>n.props['aria-label']==='译文预览').props.hidden,true);
  values[21]={error:'缓存接口尚未加载，请重启 DeepSeek Harness 后重试。'};tree=render();
  assert.ok(all(tree).some(n=>n.children.includes('统计失败')));
  assert.ok(!all(tree).some(n=>n.children.includes('统计中…')));
  assert.equal(find(tree,n=>n.props['aria-label']==='一键清除翻译缓存').props.disabled,true);
  values.length=0;tree=render();
  assert.equal(find(tree,n=>n.type==='textarea').props.value,'Preserve this draft');
});
