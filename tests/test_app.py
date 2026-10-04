"""Offline regression tests; no private cookies, network, or paid API calls."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import app
import pipeline
from jobs import JobStore

TRANSCRIPT = [{"start": 88.96, "end": 92.0, "text": "One two three",
               "words": [{"start":90.5,"end":90.7,"word":"One"},
                         {"start":90.9,"end":91.0,"word":"two"},
                         {"start":91.1,"end":91.4,"word":"three"}]}]
CLIPS = [{"start": x, "end": x+25, "title":"A hook", "reason":"Complete story"} for x in (0,30,60)]


class PipelineTests(unittest.TestCase):
    def test_url_validation(self):
        for url in ('https://youtu.be/mw3kSNIxjqo', 'https://www.youtube.com/watch?v=mw3kSNIxjqo&list=123', 'https://youtube.com/shorts/mw3kSNIxjqo'):
            self.assertEqual(pipeline.validate_url(url)[0], 'mw3kSNIxjqo')
        for url in ('https://youtube.com.evil.test/watch?v=mw3kSNIxjqo', 'file:///tmp/test', 'https://youtube.com/playlist?list=x', None, 42, 'https://user@youtube.com/watch?v=mw3kSNIxjqo'):
            with self.assertRaises(ValueError): pipeline.validate_url(url)

    def test_clip_validation(self):
        self.assertEqual(len(pipeline.validate_clips(CLIPS,100)),3)
        for change in ({'start':-1}, {'end':float('nan')}, {'end':200}, {'end':5}, {'start':True}, {'start':30,'end':55}):
            bad = [dict(c) for c in CLIPS]; bad[0].update(change)
            with self.assertRaises(pipeline.PipelineError): pipeline.validate_clips(bad,100)

    def test_caption_local_timing(self):
        words=pipeline.get_clip_words(TRANSCRIPT,88.96,92)
        self.assertAlmostEqual(words[0]['start'],1.54)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'captions.ass'
            self.assertTrue(pipeline.create_karaoke_subtitles(TRANSCRIPT,88.96,92,str(path)))
            text=path.read_text()
            self.assertIn('0:00:01.54,0:00:01.94',text)
            self.assertIn(r'{\c&H0000FFFF&}ONE',text)
            self.assertIn('trim=start=88.96',pipeline.build_video_filter(str(path),3.04,True,88.96))

    def test_transcript_cache_and_fresh_transcription(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pipeline,'DOWNLOAD_FOLDER',directory):
            path=Path(directory)/'id_words.json';path.write_text(json.dumps(TRANSCRIPT))
            with patch.object(pipeline,'WhisperModel') as model:
                self.assertEqual(pipeline.transcribe_video('source','id'),TRANSCRIPT)
                model.assert_not_called()
            # Malformed cache triggers a fresh transcription with word timestamps.
            path.write_text('{broken')
            segment=SimpleNamespace(start=0,end=1,text='hello',words=[SimpleNamespace(start=0,end=1,word='hello')])
            model=SimpleNamespace(transcribe=lambda *a, **kw: (iter([segment]),SimpleNamespace(duration=1,language='en')))
            with patch.object(pipeline,'whisper_model',model):
                result=pipeline.transcribe_video('source','id')
                self.assertEqual(result[0]['words'][0]['word'],'hello')
                self.assertTrue(pipeline.valid_transcript(json.loads(path.read_text())))

    def test_zero_duration_whisper_word_is_valid_and_reuses_cache(self):
        # Real music transcription returned a word at 194.30–194.30.
        # Whisper alignment can produce this; it must not invalidate all speech.
        transcript = [{"start": 194.0, "end": 195.0, "text": "a smile",
                       "words": [{"start": 194.3, "end": 194.3, "word": "a"},
                                 {"start": 194.5, "end": 195.0, "word": "smile"}]}]
        self.assertTrue(pipeline.valid_transcript(transcript))
        with tempfile.TemporaryDirectory() as directory, patch.object(pipeline, 'DOWNLOAD_FOLDER', directory):
            Path(directory, 'id_words.json').write_text(json.dumps(transcript))
            with patch.object(pipeline, 'WhisperModel') as model:
                self.assertEqual(pipeline.transcribe_video('source', 'id'), transcript)
                model.assert_not_called()
        words = pipeline.get_clip_words(transcript, 194.0, 195.0)
        self.assertEqual(len(words), 2)
        self.assertGreater(words[0]['end'], words[0]['start'])
        transcript[0]['words'][0]['end'] = 194.2
        self.assertFalse(pipeline.valid_transcript(transcript))
        transcript[0]['words'][0]['end'] = float('nan')
        self.assertFalse(pipeline.valid_transcript(transcript))

    def test_gemini_retry_and_permanent_error(self):
        create=SimpleNamespace()
        with patch.object(pipeline,'gemini',SimpleNamespace(interactions=create)), patch.object(pipeline.time,'sleep') as sleep:
            with patch.object(create,'create',side_effect=[Exception('503 service_unavailable'),SimpleNamespace(output_text=json.dumps(CLIPS))],create=True) as call:
                self.assertEqual(len(pipeline.find_best_clips(TRANSCRIPT,100)),3)
                self.assertEqual(call.call_count,2);sleep.assert_called_once()
            with patch.object(create,'create',side_effect=Exception('401 API key invalid'),create=True) as call:
                with self.assertRaises(pipeline.PipelineError):pipeline.find_best_clips(TRANSCRIPT,100)
                self.assertEqual(call.call_count,1)

    def test_ffmpeg_failure_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            out=str(Path(directory)/'short.mp4')
            with patch.object(pipeline.subprocess,'run',return_value=SimpleNamespace(returncode=1,stderr='encoder failed')):
                with self.assertRaises(pipeline.PipelineError):pipeline.render_short('source',out,'subs',0,25,False)
            self.assertFalse(Path(out).exists())


class JobTests(unittest.TestCase):
    def test_background_progress_responsiveness_refresh_and_failure(self):
        store=JobStore();gate=threading.Event()
        def download(url,report):
            report('download',7,'Downloading...');gate.wait(5)
            return {'title':'Video','duration':100},'mw3kSNIxjqo','source'
        generated=[dict(c,filename=f'clip{i}.mp4',url=f'/clips/clip{i}.mp4') for i,c in enumerate(CLIPS)]
        with patch.object(app,'jobs',store), patch.object(pipeline,'check_dependencies'), patch.object(pipeline,'download_video',side_effect=download), patch.object(pipeline,'transcribe_video',return_value=TRANSCRIPT), patch.object(pipeline,'find_best_clips',return_value=CLIPS), patch.object(pipeline,'create_clips',return_value=generated):
            client=app.app.test_client()
            start=time.monotonic();response=client.post('/generate',json={'url':'https://youtu.be/mw3kSNIxjqo'})
            self.assertEqual(response.status_code,202);self.assertLess(time.monotonic()-start,1)
            job=response.json['job_id']
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/status/'+job).status_code,200)
            self.assertEqual(client.post('/generate',json={'url':'https://youtu.be/mw3kSNIxjqo'}).status_code,409)
            gate.set()
            for _ in range(100):
                state=client.get('/status/'+job).json
                if state['status']=='complete':break
                time.sleep(.01)
            self.assertEqual(state['progress'],100);self.assertEqual(len(state['clips']),3)
            self.assertEqual(client.get('/status/'+job).headers['Cache-Control'],'no-store')
        with patch.object(pipeline,'check_dependencies',side_effect=ValueError('/private/secret API key')):
            job,_=store.create('https://youtu.be/mw3kSNIxjqo')
            for _ in range(100):
                state=store.get(job)
                if state['status']=='failed':break
                time.sleep(.01)
            self.assertEqual(state['status'],'failed');self.assertNotIn('secret',state['error'])
        store.worker.shutdown()

    def test_http_validation_and_safe_media(self):
        client=app.app.test_client()
        for data in ({'url':'bad'}, {'url':42},[],None):
            self.assertEqual(client.post('/generate',json=data).status_code,400)
        self.assertEqual(client.get('/status/missing').status_code,404)
        self.assertEqual(client.get('/clips/../.env').status_code,404)
        self.assertEqual(client.get('/clips/foo.ass').status_code,404)
        with tempfile.TemporaryDirectory() as directory,patch.object(pipeline,'CLIPS_FOLDER',directory):
            Path(directory,'clip.mp4').write_bytes(b'video')
            with client.get('/clips/clip.mp4') as response:
                self.assertEqual(response.status_code,200)
            with client.get('/clips/clip.mp4?download=1') as response:
                self.assertIn('attachment',response.headers['Content-Disposition'])


if __name__=='__main__':unittest.main()
