// Exercise the actual browser script with a minimal DOM and controlled HTTP responses.
// Node is only used for this optional test; the app runs with vanilla browser JS.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children=[]; this.classList={add(){},remove(){}}; this.value='https://youtu.be/mw3kSNIxjqo'; }
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children=items;}
  setAttribute() {}
  addEventListener(name,callback) {this[name]=callback;}
}
async function tick() { for(let i=0;i<5;i++) await new Promise(resolve=>setImmediate(resolve)); }
async function scenario(restored=false) {
  const elements = new Map();const timers=[];const values=new Map();
  if(restored)values.set('autoClipperJob','job1');
  const job={success:true,job_id:'job1',stage:'render',progress:75,message:'Rendering Short 2 of 3...',status:'running'};
  const clips=[1,2,3].map(i=>({title:`Hook ${i}`,duration:30,reason:'Complete story',url:`/clips/short${i}.mp4`,filename:`short${i}.mp4`}));
  const requests=[];let failNext=false;
  const context={window:{},document:{getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();}},
    localStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
    AbortController,fetch:async (url,options)=>{requests.push(url);if(failNext){failNext=false;throw Error('offline');}return {ok:true,status: url==='/generate'?202:200,json:async()=>url==='/generate'?{success:true,job_id:'job1'}:{...job}};},
    setTimeout:(callback,ms)=>{if(ms===1500)timers.push(callback);return 1;},clearTimeout(){}};
  vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/app.js','utf8'),context);
  if(!restored)await elements.get('generateForm').submit({preventDefault(){}});
  await tick();assert.equal(elements.get('generateButton').disabled,true);
  assert.equal(elements.get('progress').value,75);assert.equal(values.get('autoClipperJob'),'job1');
  // A transient network failure keeps polling and keeps duplicate submission disabled.
  failNext=true;await timers.shift()();await tick();assert.equal(elements.get('generateButton').disabled,true);
  job.status='complete';job.progress=100;job.stage='complete';job.message='Shorts ready.';job.clips=clips;job.title='Source video';
  await timers.shift()();await tick();
  assert.equal(elements.get('progress').value,100);assert.equal(elements.get('generateButton').disabled,false);
  assert.equal(elements.get('clips').children.length,3);
  const card=elements.get('clips').children[0];assert.equal(card.children[0].controls,true);
  assert.equal(card.children[1].children[3].href,'/clips/short1.mp4?download=1');
  assert(requests.includes('/status/job1'));
  if(restored)assert(!requests.includes('/generate'));
}
(async()=>{await scenario();await scenario(true);console.log('Frontend submission, polling, reconnect, refresh recovery and three preview/download cards passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
