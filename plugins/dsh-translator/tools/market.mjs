import {createServer} from 'node:http';
import {createReadStream} from 'node:fs';
import {stat,readdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
const root=fileURLToPath(new URL('../dist/',import.meta.url));
const server=createServer(async(req,res)=>{
  try {
    if(!['GET','HEAD'].includes(req.method)){res.writeHead(405);res.end();return;}
    const key=decodeURIComponent(new URL(req.url,'http://127.0.0.1').pathname).slice(1);
    if(key==='health'){res.writeHead(200,{'content-type':'application/json'});res.end(req.method==='HEAD'?'':JSON.stringify({service:'sacc-translator-market',version:1}));return;}
    const allowed=new Set((await readdir(root)).filter(n=>n.endsWith('.json')||n.endsWith('.tgz')));
    const name=['dsh-sacc-translator-cpu','dsh-sacc-translator-cuda'].includes(key)?key+'.json':key;
    if(!allowed.has(name)){res.writeHead(404);res.end();return;}
    const path=join(root,name);const info=await stat(path);
    res.writeHead(200,{'content-length':info.size,'content-type':name.endsWith('.json')?'application/json':'application/octet-stream'});
    if(req.method==='HEAD'){res.end();return;}
    const stream=createReadStream(path);stream.on('error',()=>res.destroy());res.on('close',()=>stream.destroy());stream.pipe(res);
  }catch{if(!res.headersSent)res.writeHead(500);res.end();}
});
server.listen(31989,'127.0.0.1',()=>console.log('SACC market: http://127.0.0.1:31989/index.json'));
