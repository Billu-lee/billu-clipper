import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import app
from jobs import JobStore
from publishing.manager import Publisher
from publishing.providers import Instagram, YouTube, PublishError, UncertainUpload, safe_upload_url
from publishing.storage import Store


def response(data=None, status=200, headers=None):
    return SimpleNamespace(status_code=status,headers=headers or {},json=lambda:data or {})


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.publisher=Publisher(self.root/'private',self.root/'clips')
        (self.root/'clips').mkdir()
        self.store=self.publisher.store
        self.store.save_account('youtube',dict(id='channel',label='My channel',access_token='private-token',refresh_token='private-refresh',expires_at=99999999999))

    def tearDown(self):
        self.publisher.worker.shutdown()
        self.directory.cleanup()

    def test_tokens_never_in_account_or_upload_json(self):
        data=self.publisher.accounts()
        self.assertNotIn('private-token',json.dumps(data))
        upload,_=self.store.create('youtube','channel','hash','clip.mp4')
        self.store.update(upload['upload_id'],session_uri='https://private-upload-url',container_id='private-container')
        result=self.publisher.get(upload['upload_id'])
        self.assertNotIn('session_uri',result);self.assertNotIn('container_id',result)
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)

    def test_durable_deduplication_and_restart_uncertainty(self):
        upload,created=self.store.create('youtube','channel','hash','clip.mp4')
        duplicate,created2=self.store.create('youtube','channel','hash','clip.mp4')
        self.assertTrue(created);self.assertFalse(created2);self.assertEqual(upload['upload_id'],duplicate['upload_id'])
        restarted=Store(self.root/'private')
        self.assertEqual(restarted.get(upload['upload_id'])['status'],'uncertain')
        self.assertFalse(restarted.create('youtube','channel','hash','clip.mp4',retry=True)[1])
        restarted.update(upload['upload_id'],status='failed')
        self.assertTrue(restarted.create('youtube','channel','hash','clip.mp4',retry=True)[1])
        self.assertEqual(restarted.get(upload['upload_id'])['progress'],0)

    def test_youtube_oauth_pkce_and_refresh(self):
        provider=self.publisher.providers['youtube']
        env=dict(YOUTUBE_CLIENT_ID='client',YOUTUBE_CLIENT_SECRET='secret',YOUTUBE_REDIRECT_URI='http://127.0.0.1:5001/accounts/youtube/callback')
        with patch.dict(os.environ,env):
            url,state,verifier=provider.authorize()
            self.assertIn('code_challenge_method=S256',url);self.assertIn('access_type=offline',url)
            self.assertNotIn('secret',url);self.assertGreater(len(verifier),40)
            with patch('publishing.providers.request',side_effect=[response({'access_token':'token','refresh_token':'refresh','expires_in':3600}),response({'items':[{'id':'channel2','snippet':{'title':'Channel 2'}}]})]):
                provider.connect('code',verifier)
            account=self.store.account('youtube');self.assertEqual(account['id'],'channel2')
            account['expires_at']=0
            with patch('publishing.providers.request',return_value=response({'access_token':'renewed','expires_in':3600})):
                self.assertEqual(provider.token(account),'renewed')
            self.assertEqual(self.store.account('youtube')['refresh_token'],'refresh')

    def test_youtube_resumable_upload(self):
        path=self.root/'video.mp4';path.write_bytes(b'abcdef')
        calls=[response(headers={'Location':'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test'}),
               response(status=308,headers={'Range':'bytes=0-2'}),response({'id':'abcdefghijk','status':{'privacyStatus':'private'}},status=201)]
        with patch('publishing.providers.request',side_effect=calls) as request:
            result=self.publisher.providers['youtube'].upload(path,{'title':'Hook'},
                {'caption':'Caption','privacy':'public','made_for_kids':False},self.store.account('youtube'),lambda *args:None,lambda **kw:None)
        self.assertEqual(result['visibility'],'private');self.assertEqual(result['url'],'https://youtu.be/abcdefghijk')
        self.assertEqual(request.call_args_list[-1].kwargs['data'],b'def')
        self.assertEqual(request.call_args_list[-1].kwargs['headers']['Content-Range'],'bytes 3-5/6')

    def test_instagram_binary_upload_and_processing_before_publish(self):
        path=self.root/'video.mp4';path.write_bytes(b'video')
        account=dict(id='123',label='@creator',access_token='private-token')
        calls=[response({'id':'456','uri':'https://rupload.facebook.com/ig-api-upload/v24.0/456'}),response({'success':True}),
               response({'status_code':'IN_PROGRESS'}),response({'status_code':'FINISHED'}),response({'id':'789'}),
               response({'permalink':'https://www.instagram.com/reel/abc/'})]
        with patch('publishing.providers.request',side_effect=calls) as request,patch('publishing.providers.time.sleep'):
            result=Instagram(self.store).upload(path,{},dict(caption='Caption'),account,lambda *args:None,lambda **kw:None)
        self.assertEqual(result['remote_id'],'789');self.assertEqual(result['url'],'https://www.instagram.com/reel/abc/')
        self.assertEqual(request.call_args_list[1].kwargs['headers']['file_size'],'5')
        self.assertIn('/media_publish',request.call_args_list[4].args[1])

    def test_upload_host_validation(self):
        for url in ('http://www.googleapis.com/upload/youtube/v3/videos','https://evil.example/upload/youtube/v3/videos','https://www.googleapis.com@evil.example/upload/youtube/v3/videos'):
            with self.assertRaises(PublishError):safe_upload_url(url,'www.googleapis.com','/upload/youtube/v3/videos')

    def test_account_change_prevents_automatic_publishing(self):
        options=self.publisher.options(dict(platforms=['youtube']))
        self.store.save_account('youtube',dict(id='another-channel',label='Another channel'))
        with self.assertRaises(PublishError):self.publisher.automatic([],options)

    def test_network_failure_is_uncertain_and_does_not_repeat(self):
        path=self.root/'clips'/'clip.mp4';path.write_bytes(b'video')
        clip=dict(filename='clip.mp4',title='Hook')
        options=self.publisher.options(dict(platforms=['youtube']))
        with patch.object(self.publisher.providers['youtube'],'upload',side_effect=UncertainUpload('Check your account.')) as upload:
            ids=self.publisher.enqueue(clip,options)
            self.publisher.worker.shutdown()
            self.assertEqual(self.publisher.get(ids[0])['status'],'uncertain')
            self.assertEqual(self.publisher.enqueue(clip,{**options,'retry':True}),ids)
            self.assertEqual(upload.call_count,1)

    def test_http_accounts_state_validation_and_publish_validation(self):
        with patch.object(app,'publisher',self.publisher):
            client=app.app.test_client()
            self.assertNotIn('private-token',client.get('/accounts').get_data(as_text=True))
            self.assertEqual(client.get('/accounts/youtube/callback?state=forged&code=code').status_code,400)
            self.assertEqual(client.post('/publish',json={'job_id':'missing','clip_index':0}).status_code,400)
            self.assertEqual(client.post('/publish',data='x',content_type='text/plain').status_code,403)
            self.assertEqual(client.post('/publish',json={},headers={'Origin':'https://evil.example'}).status_code,403)
            self.assertEqual(client.get('/publish/status/missing').status_code,404)
            with patch.object(self.publisher.providers['youtube'],'configured',return_value=False):
                self.assertEqual(client.get('/accounts/youtube/connect').status_code,400)

    def test_publish_route_returns_job_and_status_without_waiting_for_upload(self):
        import threading
        gate=threading.Event()
        path=self.root/'clips'/'clip.mp4';path.write_bytes(b'video')
        completed=dict(status='complete',clips=[dict(filename='clip.mp4',title='Hook')])
        def upload(*args):
            gate.wait(5)
            return dict(remote_id='abcdefghijk',url='https://youtu.be/abcdefghijk')
        with patch.object(app,'publisher',self.publisher),patch.object(app.jobs,'get',return_value=completed),patch.object(self.publisher.providers['youtube'],'upload',side_effect=upload):
            client=app.app.test_client()
            result=client.post('/publish',json=dict(job_id='job',clip_index=0,platforms=['youtube']))
            self.assertEqual(result.status_code,202)
            ident=result.json['upload_ids'][0]
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/publish/status/'+ident).status_code,200)
            self.assertEqual(client.post('/publish',json=dict(job_id='job',clip_index=0,platforms=['youtube'])).json['upload_ids'],[ident])
            gate.set();self.publisher.worker.shutdown()
            status=client.get('/publish/status/'+ident).json
            self.assertEqual(status['status'],'complete');self.assertEqual(status['progress'],100)
            self.assertEqual(client.get('/accounts',headers={'Host':'evil.example'}).status_code,400)

    def test_unknown_outcome_requires_explicit_confirmation(self):
        upload,_=self.store.create('youtube','channel','hash','clip.mp4')
        self.store.update(upload['upload_id'],status='uncertain')
        with patch.object(app,'publisher',self.publisher):
            client=app.app.test_client();url='/publish/resolve/'+upload['upload_id']
            self.assertEqual(client.post(url,json={}).status_code,400)
            self.assertEqual(client.post(url,json={'not_posted':True}).status_code,200)
            self.assertEqual(self.store.get(upload['upload_id'])['status'],'failed')
            self.store.update(upload['upload_id'],status='uncertain',remote_id='abcdefghijk')
            self.assertEqual(client.post(url,json={'not_posted':True}).status_code,400)

    def test_automatic_publish_failure_keeps_generation_results(self):
        import pipeline
        store=JobStore();store.publish_callback=lambda *args: (_ for _ in ()).throw(PublishError('Disconnected'))
        with (patch.object(pipeline,'check_dependencies'),patch.object(pipeline,'download_video',return_value=({'duration':100},'id','video')),
             patch.object(pipeline,'transcribe_video',return_value=[]),patch.object(pipeline,'find_best_clips',return_value=[]),patch.object(pipeline,'create_clips',return_value=[{'title':'Done'}])):
            store.jobs['test']=dict(job_id='test',status='queued',progress=0,publish_options={'platforms':['youtube']})
            store.run('test','url')
        job=store.get('test')
        self.assertEqual(job['status'],'complete');self.assertEqual(job['progress'],100)
        self.assertTrue(job['publishing_error']);self.assertFalse(job['publishing_pending'])
        store.worker.shutdown()


if __name__=='__main__':unittest.main()
