const $ = id => document.getElementById(id);
let activeJob = null;
const storageKey = 'autoClipperJob';
function remember(id) { try { localStorage.setItem(storageKey, id); } catch (_) {} }
function forget() { try { localStorage.removeItem(storageKey); } catch (_) {} }
function setBusy(busy) {
  $('generateButton').disabled = busy;
  $('youtubeUrl').disabled = busy;
  $('generateButton').textContent = busy ? 'Processing…' : 'Generate Shorts';
}
function showError(message) { $('status').classList.add('error'); $('status').textContent = message; }
function showResults(data) {
  $('clips').replaceChildren();
  $('videoTitle').textContent = data.title || 'Your Shorts';
  data.clips.forEach((clip, index) => {
    const card = document.createElement('article'); card.className = 'clip-card';
    const video = document.createElement('video');
    video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.src = clip.url;
    video.setAttribute('aria-label', clip.title || `Short ${index + 1}`);
    const info = document.createElement('div'); info.className = 'clip-info';
    const title = document.createElement('h3'); title.textContent = clip.title;
    const duration = document.createElement('p'); duration.className = 'duration'; duration.textContent = `${clip.duration} seconds`;
    const reason = document.createElement('p'); reason.className = 'reason'; reason.textContent = `Why AI selected this: ${clip.reason}`;
    const download = document.createElement('a'); download.className = 'download';
    download.href = `${clip.url}?download=1`; download.download = clip.filename; download.textContent = 'Download Short';
    info.append(title, duration, reason, download);
    if (window.addPublishControls) window.addPublishControls(info,clip,index,data.job_id);
    card.append(video, info); $('clips').append(card);
  });
}
async function requestJSON(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {...options, signal: controller.signal, cache: 'no-store'});
    const data = await response.json(); return {response, data};
  } finally { clearTimeout(timer); }
}
async function poll(jobId) {
  if (activeJob !== jobId) return;
  try {
    const {response, data} = await requestJSON(`/status/${encodeURIComponent(jobId)}`);
    if (!response.ok) {
      if (response.status === 404) { activeJob = null; forget(); setBusy(false); }
      throw new Error(data.error || 'Could not read job status.');
    }
    $('status').classList.remove('error'); $('progressArea').hidden = false;
    $('stage').textContent = data.stage.replaceAll('_', ' ');
    $('progress').value = data.progress; $('percentage').textContent = `${data.progress}%`;
    $('status').textContent = data.message;
    if (data.status === 'complete' && data.publishing_pending) {
      setTimeout(() => poll(jobId), 1500); return;
    }
    if (data.status === 'complete' || data.status === 'failed') {
      activeJob = null; setBusy(false);
      if (data.status === 'complete') { showResults(data); remember(jobId);
        if (window.showAutomaticUploads) window.showAutomaticUploads(data); }
      else { showError(data.error); forget(); }
      return;
    }
  } catch (error) {
    showError(activeJob ? 'Connection interrupted. Reconnecting to your job…' : error.message);
  }
  if (activeJob === jobId) setTimeout(() => poll(jobId), 1500);
}
$('generateForm').addEventListener('submit', async event => {
  event.preventDefault(); if (activeJob) return;
  setBusy(true); $('status').classList.remove('error'); $('status').textContent = 'Preparing job…';
  $('clips').replaceChildren(); $('videoTitle').textContent = ''; $('progress').value = 0;
  try {
    const publish = window.getAutoPublishOptions ? window.getAutoPublishOptions() : null;
    const {response, data} = await requestJSON('/generate', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url: $('youtubeUrl').value.trim(), ...(publish ? {publish} : {})})});
    if (!response.ok && !(response.status === 409 && data.job_id)) throw new Error(data.error || 'Could not start generation.');
    activeJob = data.job_id; remember(activeJob); poll(activeJob);
  } catch (error) { setBusy(false); showError(error.name === 'AbortError' ? 'Could not connect to the server. Try again; any running job will be recovered.' : error.message); }
});
try { activeJob = localStorage.getItem(storageKey); } catch (_) {}
if (activeJob) { setBusy(true); poll(activeJob); }
