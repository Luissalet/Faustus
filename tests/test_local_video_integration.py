"""Real local codecs and installed voices. Never calls a provider or downloads weights."""
import json
import os
import shutil
import subprocess
import pytest
from services.local_video import command,dub,video_duration


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
