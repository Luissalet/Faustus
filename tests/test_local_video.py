import json
from pathlib import Path
import pytest
from services.local_video import validate_segments, tempo_filter, input_args, video_duration


@pytest.mark.parametrize("rows", [[],[{"start":False,"end":2,"text":"hi"}],
    [{"start":0,"end":float("inf"),"text":"hi"}], [{"start":0,"end":1e-300,"text":"hi"}],
    [{"start":0,"end":181,"text":"hi"}], [{"start":0,"end":1,"text":" "}],
    [{"start":0,"end":2,"text":"hi"},{"start":1,"end":3,"text":"overlap"}],
    [{"start":0,"end":2,"text":"hi","path":"escape"}]])
def test_rejects_invalid_segments(rows):
    with pytest.raises(ValueError): validate_segments(rows,180)


def test_valid_segments():
    assert validate_segments([{"start":0,"end":2,"text":" Hola "}],3)==[{"start":0.0,"end":2.0,"text":"Hola"}]


@pytest.mark.parametrize("ratio", [0,float("inf"),float("nan"),1e300,-1,5])
def test_invalid_tempo(ratio):
    with pytest.raises(ValueError): tempo_filter(ratio)


def test_tempo_chain():
    assert tempo_filter(4)=="atempo=2.00000000,atempo=2.00000000"
    assert tempo_filter(.25)=="atempo=0.50000000,atempo=0.50000000"


def test_no_playlist_or_network_input():
    with pytest.raises(ValueError):input_args(Path('playlist.m3u8'))
    assert input_args(Path('input.mp4'))[:4]==['-protocol_whitelist','file,pipe','-f','mov']


@pytest.mark.parametrize('duration,pixels', [(181,100),(float('nan'),100),(5,3000)])
def test_video_limits(monkeypatch,duration,pixels):
    monkeypatch.setattr('services.local_video.shutil.which',lambda _: 'ffprobe')
    monkeypatch.setattr('services.local_video.command',lambda *a,**k:json.dumps({'format':{'duration':duration},'streams':[{'codec_type':'video','width':pixels,'height':pixels}]}).encode())
    with pytest.raises(ValueError):video_duration(Path('input.mp4'))
