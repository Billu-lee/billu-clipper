from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Lock
from .providers import Instagram, YouTube, PublishError, UncertainUpload
from .storage import Store


class Publisher:
    def __init__(self, directory, clips_directory):
        self.store = Store(directory)
        self.clips_directory = Path(clips_directory).resolve()
        self.providers = {'youtube':YouTube(self.store),'instagram':Instagram(self.store)}
        self.worker = ThreadPoolExecutor(max_workers=1,thread_name_prefix='publishing')
        self.lock = Lock()

    def accounts(self):
        result = {}
        for platform, provider in self.providers.items():
            account = self.store.account(platform)
            result[platform] = dict(configured=provider.configured(),connected=bool(account),
                                    label=account['label'] if account else None)
        return result

    def options(self, data):
        if not isinstance(data,dict):
            raise PublishError('Please select publishing options.')
        platforms=data.get('platforms',[])
        if not isinstance(platforms,list) or not platforms or any(not isinstance(p,str) or p not in self.providers for p in platforms):
            raise PublishError('Choose YouTube, Instagram, or both.')
        privacy=data.get('privacy','private')
        if privacy not in ('private','unlisted','public'):
            raise PublishError('Choose a valid YouTube visibility.')
        if not isinstance(data.get('made_for_kids',False),bool):
            raise PublishError('Choose a valid audience setting.')
        caption=data.get('caption','')
        if not isinstance(caption,str) or len(caption)>2200:
            raise PublishError('Caption must contain at most 2200 characters.')
        title=data.get('title')
        if title is not None and (not isinstance(title,str) or not title.strip() or len(title)>100 or '<' in title or '>' in title):
            raise PublishError('Title must be 1–100 characters and cannot contain angle brackets.')
        accounts = {p:self.store.account(p) for p in platforms}
        for platform, account in accounts.items():
            if not account:
                raise PublishError(f'Connect your {platform.title()} account first.')
        account_ids = {p:account['id'] for p,account in accounts.items()}
        return dict(platforms=list(dict.fromkeys(platforms)),account_ids=account_ids,privacy=privacy,
                    made_for_kids=data.get('made_for_kids',False),caption=caption,title=title,retry=data.get('retry') is True)

    def enqueue(self,clip,options):
        filename=clip.get('filename','')
        if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.mp4',filename):
            raise PublishError('Choose a completed Short from your results.')
        path=(self.clips_directory/filename).resolve()
        if path.parent != self.clips_directory or not path.is_file():
            raise PublishError('The completed Short could not be found.')
        with path.open('rb') as video:
            fingerprint=hashlib.file_digest(video,'sha256').hexdigest()
        ids=[]
        with self.lock:
            accounts = {p:self.store.account(p) for p in options['platforms']}
            for platform,account in accounts.items():
                if not account:
                    raise PublishError(f'Connect your {platform.title()} account first.')
                if account['id'] != options['account_ids'][platform]:
                    raise PublishError('The connected account changed. Select publishing settings again.')
            for platform,account in accounts.items():
                upload,created=self.store.create(platform,account['id'],fingerprint,filename,retry=options.get('retry',False))
                ids.append(upload['upload_id'])
                if created:
                    selected={**clip,'title':options.get('title') or clip['title']}
                    self.store.update(upload['upload_id'],title=selected['title'])
                    settings={**options,'caption':options['caption'] or selected['title']+' #Shorts'}
                    self.worker.submit(self.run,upload['upload_id'],platform,path,selected,settings,account)
        return ids

    def automatic(self,clips,options):
        # Revalidate connections at completion; they may have been disconnected.
        expected = options.get('account_ids')
        options=self.options(options)
        if expected != options['account_ids']:
            raise PublishError('A destination account changed during generation. Select the account and publish manually.')
        return [ident for clip in clips for ident in self.enqueue(clip,options)]

    def get(self,upload_id):
        data=self.store.get(upload_id)
        if data is None:
            return None
        # Session upload URLs and container identifiers are private implementation details.
        return {key:value for key,value in data.items() if key not in ('session_uri','container_id')}

    def run(self,upload_id,platform,path,clip,options,account):
        prepared_path = None
        try:
            current = self.store.account(platform)
            if not current or current['id'] != account['id']:
                raise PublishError('The destination account was disconnected or changed. Connect it again before publishing.')
            account = current
            self.store.update(upload_id,status='uploading',progress=1,message='Preparing upload...')
            if platform == 'instagram':
                # Keep the original render. Meta's sample specifies 128 kbps AAC
                # and no MP4 edit lists, so create a private upload-only copy.
                with tempfile.NamedTemporaryFile(dir=self.store.path.parent,suffix='.mp4',delete=False) as temp:
                    prepared_path = Path(temp.name)
                result = subprocess.run(['ffmpeg','-v','error','-y','-i',str(path),
                    '-map','0:v:0','-map','0:a:0?','-c:v','copy','-c:a','aac','-b:a','128k','-ar','48000',
                    '-ac','2','-use_editlist','0','-movflags','+faststart',str(prepared_path)],capture_output=True,text=True)
                if result.returncode != 0:
                    raise PublishError('Could not prepare the video for Instagram. Check FFmpeg and the source file.')
                path = prepared_path
            result=self.providers[platform].upload(path,clip,options,account,
                lambda progress,message:self.store.update(upload_id,progress=progress,message=message),
                lambda **changes:self.store.update(upload_id,**changes))
            self.store.update(upload_id,status='complete',progress=100,message='Upload complete.',**result)
        except PublishError as error:
            self.store.update(upload_id,status='uncertain' if isinstance(error,UncertainUpload) else 'failed',
                              message=str(error),error=str(error))
        except Exception:
            logging.error('Publisher %s encountered an unexpected error for upload %s',platform,upload_id)
            self.store.update(upload_id,status='uncertain',message='Upload outcome is unknown. Check the destination account.',
                              error='Publishing failed unexpectedly. Check your account before uploading again.')
        finally:
            if prepared_path is not None:
                prepared_path.unlink(missing_ok=True)
