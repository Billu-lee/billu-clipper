const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.checked=false;this.value='';this.textContent='';this.classList={add(){},remove(){}};}
  append(...items){this.children.push(...items);}
  setAttribute(){}
  addEventListener(event,callback){this[event]=callback;}
}
const elements=new Map();const timers=[];const storage=new Map();const requests=[];
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
get('publishYouTube').checked=true;get('autoPublish').checked=true;get('youtubePrivacy').value='private';
let uploadStatus='uploading';
const context={window:{},document:{getElementById:get,createElement:()=>new Element(),createTextNode:text=>({textContent:text}),querySelector:()=>new Element(),addEventListener(){}},
  localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
  setTimeout:fn=>timers.push(fn),requestJSON:async(url,options={})=>{
    requests.push({url,options});
    let data;
    if(url==='/accounts')data={accounts:{youtube:{connected:true,label:'My channel'},instagram:{connected:false,configured:false}}};
    else if(url==='/publish')data={upload_ids:['upload1']};
    else if(url.startsWith('/publish/resolve/')){uploadStatus='failed';data={success:true};}
    else data={platform:'youtube',filename:'clip.mp4',status:uploadStatus,progress:uploadStatus==='complete'?100:30,message:uploadStatus==='complete'?'Upload complete.':'Uploading...',url:uploadStatus==='complete'?'https://youtu.be/abcdefghijk':null,visibility:'private'};
    return {response:{ok:true},data};
  }};
vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/publishing.js','utf8'),context);
const tick=async()=>{for(let i=0;i<4;i++)await new Promise(r=>setImmediate(r));};
(async()=>{
  const options=context.window.getAutoPublishOptions();
  assert.equal(options.platforms[0],'youtube');assert.equal(options.privacy,'private');assert.equal(options.made_for_kids,false);
  get('autoPublish').checked=false;assert.equal(context.window.getAutoPublishOptions(),null);
  const info=new Element();context.window.addPublishControls(info,{title:'Hook'},0,'job1');
  const details=info.children[0];const button=details.children[6];
  await button.click();await tick();
  const post=requests.find(r=>r.url==='/publish');const payload=JSON.parse(post.options.body);
  assert.equal(payload.job_id,'job1');assert.equal(payload.clip_index,0);assert.equal(payload.title,'Hook');assert.equal(payload.retry,true);
  assert.equal(get('uploadStatuses').children.length,1);
  uploadStatus='complete';await timers.shift()();await tick();
  assert(get('uploadStatuses').children[0].textContent.includes('Upload complete.'));
  // A terminal upload can be polled again after a manual retry without duplicate rows.
  uploadStatus='uncertain';context.window.showAutomaticUploads({uploads:['upload1']});await tick();
  const row=get('uploadStatuses').children[0];const reset=row.children.at(-1);
  assert.equal(reset.textContent,'I checked: no post exists');await reset.click();await tick();
  assert(requests.some(r=>r.url==='/publish/resolve/upload1'));
  await context.refreshAccounts();
  assert(get('accountsStatus').textContent.includes('My channel'));
  console.log('Publishing controls, payloads, upload polling, uncertainty handling and account labels passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
