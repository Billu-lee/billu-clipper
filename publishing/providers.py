"""Official HTTP APIs. OAuth tokens never enter browser JavaScript."""
import hashlib
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlparse
import requests

SCOPES = 'https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly'


class PublishError(Exception):
    """Safe user-facing API error."""


class UncertainUpload(PublishError):
    """A remote write may have succeeded; never automatically submit it again."""


def request(method, url, **kwargs):
    try:
        response = requests.request(method, url, timeout=(15, 180), allow_redirects=False, **kwargs)
    except requests.RequestException:
        # HTTP exceptions may embed secrets in URLs. Do not display/log them.
        raise UncertainUpload('Connection interrupted. Check the destination account before trying another upload.') from None
    if response.status_code in (401, 403):
        raise PublishError('Platform access was denied. Reconnect the account and check its app permissions or quota.')
    if response.status_code >= 500:
        raise UncertainUpload('The platform returned a server error. Check your account before trying another upload.')
    if response.status_code >= 400:
        raise PublishError('The platform rejected this request. Check your account permissions, upload limits, and video settings.')
    if response.status_code not in (200, 201, 202, 204, 308):
        raise PublishError('The platform returned an unexpected response.')
    return response


def json_response(response):
    try:
        result = response.json()
        if not isinstance(result, dict) or 'error' in result:
            raise ValueError()
        return result
    except ValueError:
        raise UncertainUpload('The platform returned an unreadable response. Check your account before trying again.') from None


def safe_upload_url(url, host, prefix):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != host or parsed.port not in (None, 443) or parsed.username or parsed.password or not parsed.path.startswith(prefix):
        raise PublishError('The platform returned an invalid upload address.')
    return url


class YouTube:
    def __init__(self, store):
        self.store = store

    def configured(self):
        return all(os.getenv(key) for key in ('YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'YOUTUBE_REDIRECT_URI'))

    def authorize(self):
        if not self.configured():
            raise PublishError('Add your YouTube OAuth client settings to .env first. See README for setup.')
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(dict(
            client_id=os.environ['YOUTUBE_CLIENT_ID'], redirect_uri=os.environ['YOUTUBE_REDIRECT_URI'],
            response_type='code', scope=SCOPES, access_type='offline', prompt='consent select_account',
            state=state, code_challenge=challenge, code_challenge_method='S256'))
        return url, state, verifier

    def connect(self, code, verifier):
        data = json_response(request('POST', 'https://oauth2.googleapis.com/token', data=dict(
            code=code, code_verifier=verifier, client_id=os.environ['YOUTUBE_CLIENT_ID'],
            client_secret=os.environ['YOUTUBE_CLIENT_SECRET'], redirect_uri=os.environ['YOUTUBE_REDIRECT_URI'],
            grant_type='authorization_code')))
        if not data.get('access_token') or not data.get('refresh_token'):
            raise PublishError('Google did not grant offline access. Connect again and approve YouTube access.')
        channels = json_response(request('GET', 'https://www.googleapis.com/youtube/v3/channels',
            params={'part':'snippet', 'mine':'true'}, headers={'Authorization': 'Bearer ' + data['access_token']}))
        if not channels.get('items'):
            raise PublishError('This Google account has no accessible YouTube channel. Create a channel, then reconnect.')
        channel = channels['items'][0]
        data.update(id=channel['id'], label=channel['snippet']['title'], expires_at=time.time()+data.get('expires_in',3600))
        self.store.save_account('youtube', data)

    def token(self, account):
        if account.get('expires_at',0) <= time.time()+60:
            refreshed = json_response(request('POST','https://oauth2.googleapis.com/token',data=dict(
                client_id=os.environ['YOUTUBE_CLIENT_ID'],client_secret=os.environ['YOUTUBE_CLIENT_SECRET'],
                refresh_token=account['refresh_token'],grant_type='refresh_token')))
            if not refreshed.get('access_token'):
                raise PublishError('YouTube authorization expired. Reconnect your account.')
            account.update(access_token=refreshed['access_token'],expires_at=time.time()+refreshed.get('expires_in',3600))
            self.store.save_account('youtube',account)
        return account['access_token']

    def upload(self, path, clip, options, account, report, checkpoint):
        headers = {'Authorization':'Bearer '+self.token(account)}
        size = path.stat().st_size
        metadata = {'snippet':{'title':clip['title'].replace('<','').replace('>','')[:100] or 'Short', 'description': options['caption'][:5000], 'categoryId':'22'},
                    'status':{'privacyStatus':options['privacy'], 'selfDeclaredMadeForKids':options['made_for_kids']}}
        response = request('POST','https://www.googleapis.com/upload/youtube/v3/videos',
                           params={'uploadType':'resumable','part':'snippet,status'},json=metadata,
                           headers={**headers,'X-Upload-Content-Length':str(size),'X-Upload-Content-Type':'video/mp4'})
        uri = safe_upload_url(response.headers.get('Location',''),'www.googleapis.com','/upload/youtube/v3/videos')
        checkpoint(session_uri=uri)
        offset = 0
        report(5,'Uploading to YouTube...')
        # Fixed-size chunks are multiples of 256 KiB. API acknowledgements drive progress.
        with path.open('rb') as video:
            while offset < size:
                video.seek(offset)
                chunk = video.read(8*1024*1024)
                response = request('PUT',uri,data=chunk,headers={**headers,'Content-Type':'video/mp4',
                    'Content-Range':f'bytes {offset}-{offset+len(chunk)-1}/{size}'})
                if response.status_code in (200,201):
                    result = json_response(response)
                    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(result.get('id',''))):
                        raise UncertainUpload('YouTube accepted the upload but returned no video ID. Check YouTube Studio.')
                    # The API may force private visibility for an unaudited project.
                    return dict(remote_id=result['id'],url='https://youtu.be/'+result['id'],
                                visibility=result.get('status',{}).get('privacyStatus',options['privacy']))
                if response.status_code != 308:
                    raise UncertainUpload('YouTube did not confirm the upload. Check YouTube Studio.')
                match = re.fullmatch(r'bytes=0-(\d+)',response.headers.get('Range',''))
                confirmed = int(match.group(1))+1 if match else 0
                if not offset < confirmed <= offset+len(chunk):
                    raise UncertainUpload('YouTube did not confirm upload progress. Check YouTube Studio.')
                offset = confirmed
                report(5+85*offset/size,'Uploading to YouTube...')
        raise UncertainUpload('Upload transferred but YouTube did not confirm completion. Check YouTube Studio.')


class Instagram:
    """Facebook Login for Business token + Page-linked professional Instagram."""
    def __init__(self, store):
        self.store = store

    def configured(self):
        return bool(os.getenv('INSTAGRAM_ACCESS_TOKEN') and re.fullmatch(r'\d+',os.getenv('INSTAGRAM_ACCOUNT_ID','')))

    def connect(self):
        if not self.configured():
            raise PublishError('Add your Instagram account ID and Meta access token to .env. See README for setup.')
        account = dict(id=os.environ['INSTAGRAM_ACCOUNT_ID'],access_token=os.environ['INSTAGRAM_ACCESS_TOKEN'])
        result = json_response(request('GET',self.base+'/'+account['id'],params={'fields':'id,username'},
                                      headers={'Authorization':'Bearer '+account['access_token']}))
        if str(result.get('id')) != account['id'] or not result.get('username'):
            raise PublishError('Meta did not recognize this professional Instagram account.')
        account['label'] = '@'+result['username']
        self.store.save_account('instagram',account)

    @property
    def base(self):
        version = os.getenv('META_GRAPH_VERSION','v24.0')
        if not re.fullmatch(r'v\d+\.0',version):
            raise PublishError('META_GRAPH_VERSION must look like v24.0.')
        return 'https://graph.facebook.com/'+version

    def upload(self,path,clip,options,account,report,checkpoint):
        headers={'Authorization':'Bearer '+account['access_token']}
        container = json_response(request('POST',self.base+'/'+account['id']+'/media',headers=headers,
            data={'media_type':'REELS','upload_type':'resumable','caption':options['caption'][:2200], 'share_to_feed':'true'}))
        container_id = str(container.get('id',''))
        if not re.fullmatch(r'\d+',container_id):
            raise PublishError('Instagram did not create an upload container.')
        checkpoint(container_id=container_id)
        uri = safe_upload_url(container.get('uri',''),'rupload.facebook.com','/ig-api-upload/')
        report(5,'Uploading Reel to Instagram...')
        with path.open('rb') as video:
            result = json_response(request('POST',uri,data=video,headers={'Authorization':'OAuth '+account['access_token'],
                                          'offset':'0','file_size':str(path.stat().st_size),'Content-Type':'application/octet-stream'}))
        if not result.get('success'):
            raise PublishError('Instagram could not accept the video file.')
        report(85,'Instagram is processing the Reel...')
        for _ in range(60):
            status = json_response(request('GET',self.base+'/'+container_id,params={'fields':'status_code'},headers=headers))
            code = status.get('status_code')
            if code == 'FINISHED':
                break
            if code in ('ERROR','EXPIRED'):
                raise PublishError('Instagram could not process this Reel. Check the account permissions and media requirements.')
            if code == 'PUBLISHED':
                raise UncertainUpload('This Instagram container was already published. Check your account.')
            time.sleep(5)
        else:
            raise PublishError('Instagram processing took too long. This Reel has not been submitted for publishing.')
        report(95,'Publishing Reel on Instagram...')
        published = json_response(request('POST',self.base+'/'+account['id']+'/media_publish',
                                         data={'creation_id':container_id},headers=headers))
        media_id = str(published.get('id',''))
        if not re.fullmatch(r'\d+',media_id):
            raise UncertainUpload('Instagram may have published this Reel but returned no ID. Check your account.')
        checkpoint(remote_id=media_id)
        # Permalink lookup is optional: failure must never turn a published Reel into a retry.
        url = 'https://www.instagram.com/'+account['label'].lstrip('@')+'/'
        try:
            details=json_response(request('GET',self.base+'/'+media_id,params={'fields':'permalink'},headers=headers))
            candidate=details.get('permalink','')
            parsed=urlparse(candidate)
            if parsed.scheme=='https' and parsed.hostname in ('www.instagram.com','instagram.com'):
                url=candidate
        except PublishError:
            pass
        return dict(remote_id=media_id,url=url)
