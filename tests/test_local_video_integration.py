"""Real local codecs and installed voices. Never calls a provider or downloads weights."""
import json
import os
import shutil
import subprocess
import pytest
from services.local_video import command,dub,video_duration,transcribe


@pytest.mark.skipif(os.name!='nt' or not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='Requires local FFmpeg and Windows voices')
@pytest.mark.parametrize('language,text',[('es','Hola, prueba local.'),('en','Hello, local test.')])
def test_real_offline_dubbing(tmp_path,language,text):
    source=tmp_path/'input.mp4'
    command([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=black:s=320x180:r=10:d=5','-c:v','libx264',str(source)])
    assert video_duration(source)==5
    output=dub(source,[{'start':0,'end':5,'text':text}],language)
    assert output.stat().st_size>1000
    result=json.loads(command([shutil.which('ffprobe'),'-v','error','-show_entries','stream=codec_type:format=duration','-of','json',str(output)]))
    assert {row['codec_type'] for row in result['streams']}=={'audio','video'}
    assert abs(float(result['format']['duration'])-5)<.1


@pytest.mark.skipif(os.environ.get('FAUSTUS_TEST_WHISPER')!='1',reason='Opt-in real transcription with already-cached local weights')
@pytest.mark.parametrize('language,text',[('es','Hola, esta es una prueba de vídeo local.'),('en','Hello, this is a test of local video.')])
def test_real_offline_transcription(tmp_path,monkeypatch,language,text):
    monkeypatch.setenv('HF_HUB_OFFLINE','1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE','1')
    source=tmp_path/'input.mp4'
    command([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=black:s=320x180:r=10:d=6','-c:v','libx264',str(source)])
    narrated=dub(source,[{'start':0,'end':6,'text':text}],language)
    result=transcribe(narrated,language,'base')
    assert result['language']==language
    assert result['segments'] and all(row['text'].strip() for row in result['segments'])
    assert 0<result['duration']<=6.1
