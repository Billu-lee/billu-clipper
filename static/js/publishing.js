// Account controls and upload polling are separate from the generation pipeline.
const uploadTimers = new Map();
function selectedPublishingOptions() {
  const platforms = [];
  if (document.getElementById('publishYouTube').checked) platforms.push('youtube');
  if (document.getElementById('publishInstagram').checked) platforms.push('instagram');
  if (!platforms.length) throw new Error('Choose at least one publishing destination.');
  return {platforms, privacy:document.getElementById('youtubePrivacy').value,
          made_for_kids:document.getElementById('madeForKids').checked};
}
window.getAutoPublishOptions = () => document.getElementById('autoPublish').checked ? selectedPublishingOptions() : null;
async function publishingRequest(url, body) {
  const {response, data} = await requestJSON(url, body === undefined ? {} : {
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (!response.ok) throw new Error(data.error || 'Could not complete publishing request.');
  return data;
}
async function refreshAccounts() {
  const status = document.getElementById('accountsStatus');
  try {
    const data = await publishingRequest('/accounts');
    status.textContent = Object.entries(data.accounts).map(([platform,account]) =>
      `${platform === 'youtube' ? 'YouTube' : 'Instagram'}: ${account.connected ? account.label : account.configured ? 'ready to connect' : 'setup required'}`).join(' · ');
    document.getElementById('disconnectYouTube').hidden = !data.accounts.youtube.connected;
    document.getElementById('disconnectInstagram').hidden = !data.accounts.instagram.connected;
  } catch (error) {status.textContent = error.message;}
}
function trackUploads(ids) {
  const root = document.getElementById('uploadStatuses');
  let stored=[]; try {stored=JSON.parse(localStorage.getItem('autoClipperUploads')||'[]');} catch (_) {}
  try {localStorage.setItem('autoClipperUploads',JSON.stringify([...new Set([...stored,...ids])].slice(-50)));} catch (_) {}
  ids.forEach(id => {
    const previous=uploadTimers.get(id);
    if (previous?.active) return;
    const row=previous?.row || document.createElement('p');
    row.className='upload-status';row.classList.remove('error');
    if (!previous) root.append(row);
    uploadTimers.set(id,{row,active:true});
    async function update() {
      try {
        const upload = await publishingRequest(`/publish/status/${encodeURIComponent(id)}`);
        row.textContent = `${upload.platform === 'youtube' ? 'YouTube' : 'Instagram'} · ${upload.title || 'Short'} · ${upload.message} (${upload.progress}%)`;
        if (upload.status === 'complete') {
          uploadTimers.get(id).active=false;
          if (upload.visibility) row.append(document.createTextNode(` · ${upload.visibility}`));
          if (upload.url) { const link=document.createElement('a'); link.href=upload.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=' View post';row.append(link); }
          return;
        }
        if (upload.status === 'failed' || upload.status === 'uncertain') {
          uploadTimers.get(id).active=false;row.classList.add('error');
          if (upload.status === 'uncertain' && !upload.remote_id) {
            const reset=document.createElement('button');reset.type='button';reset.textContent='I checked: no post exists';
            reset.addEventListener('click',async()=>{
              reset.disabled=true;
              try {await publishingRequest(`/publish/resolve/${encodeURIComponent(id)}`,{not_posted:true});trackUploads([id]);}
              catch(error){row.textContent=error.message;}
            });row.append(reset);
          }
          return;
        }
      } catch (error) {
        row.textContent = error.message;
        if (error.message === 'Upload not found.') {uploadTimers.get(id).active=false;return;}
      }
      setTimeout(update,2000);
    }
    update();
  });
}
window.showAutomaticUploads = data => {
  if (data.uploads?.length) {document.querySelector('.publishing-settings').open=true;trackUploads(data.uploads);}
  if (data.publishing_error) {const p=document.createElement('p');p.className='error';p.textContent=data.publishing_error;document.getElementById('uploadStatuses').append(p);}
};
window.addPublishControls = (info,clip,index,jobId) => {
  const details=document.createElement('details');details.className='clip-publishing';
  const summary=document.createElement('summary');summary.textContent='Publish this Short';
  const titleLabel=document.createElement('label');titleLabel.textContent='Post title';
  const title=document.createElement('input');title.value=clip.title.slice(0,100);title.maxLength=100;title.setAttribute('aria-label','Post title');
  const captionLabel=document.createElement('label');captionLabel.textContent='Caption / description';
  const caption=document.createElement('textarea');caption.value=clip.title+' #Shorts';caption.maxLength=2200;caption.rows=3;caption.setAttribute('aria-label','Caption or description');
  const hint=document.createElement('p');hint.className='hint';hint.textContent='Uses the destinations, visibility and audience selected above.';
  const button=document.createElement('button');button.type='button';button.textContent='Publish';
  const status=document.createElement('p');status.setAttribute('role','status');
  button.addEventListener('click',async()=>{
    button.disabled=true;status.textContent='Preparing upload…';status.classList.remove('error');
    try {
      const options=selectedPublishingOptions();
      const data=await publishingRequest('/publish',{job_id:jobId,clip_index:index,title:title.value.trim(),caption:caption.value,retry:true,...options});
      document.querySelector('.publishing-settings').open=true;
      trackUploads(data.upload_ids);status.textContent='Follow upload progress above.';
    } catch(error){status.classList.add('error');status.textContent=error.message;}
    finally{button.disabled=false;}
  });
  details.append(summary,titleLabel,title,captionLabel,caption,hint,button,status);info.append(details);
};
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('connectInstagram').addEventListener('click',async()=>{
    try {await publishingRequest('/accounts/instagram/connect',{});await refreshAccounts();}
    catch(error){document.getElementById('accountsStatus').textContent=error.message;}
  });
  for (const [id,platform] of [['disconnectYouTube','youtube'],['disconnectInstagram','instagram']]) {
    document.getElementById(id).addEventListener('click',async()=>{
      try {await publishingRequest(`/accounts/${platform}/disconnect`,{});await refreshAccounts();}
      catch(error){document.getElementById('accountsStatus').textContent=error.message;}
    });
  }
  refreshAccounts();
  try {trackUploads(JSON.parse(localStorage.getItem('autoClipperUploads')||'[]'));} catch (_) {}
});
