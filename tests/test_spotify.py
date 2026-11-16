"""src/spotify.py: PKCE login against the owner's app, token refresh and the tools."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

from src import spotify


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.content = b"" if data is None else json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = {"spotify_client_id": "cid123"}
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: store.get(k, d))
    path = str(tmp_path / "auth.json")
    monkeypatch.setattr(spotify, "auth_path", lambda: path)
    return store


def test_status_without_client_id_says_how_to_set_up(env):
    env["spotify_client_id"] = ""
    st = spotify.status()
    assert st["configured"] is False and "developer.spotify.com" in st["setup"]
    assert st["redirect_uri"].endswith("/api/spotify/callback") and st["redirect_uri"].startswith("http://127.0.0.1:")
    with pytest.raises(spotify.SpotifyError, match="Client ID"):
        spotify.begin_login()


def test_pkce_login_round_trip_and_encrypted_store(env):
    url = spotify.begin_login()
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["client_id"] == "cid123" and q["code_challenge_method"] == "S256" and "user-library-read" in q["scope"]
    sent = {}

    def post(url, data, headers, timeout):
        sent.update(data)
        return Resp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": "s"})

    spotify.finish_login("CODE", q["state"], post=post)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"] and sent["grant_type"] == "authorization_code"
    raw = open(spotify.auth_path(), encoding="utf-8").read()
    assert "AT" not in raw and "RT" not in raw            # encrypted at rest
    assert spotify.load_tokens()["refresh_token"] == "RT"
    assert spotify.status()["logged_in"] is True
    with pytest.raises(spotify.SpotifyError, match="state"):
        spotify.finish_login("CODE", q["state"], post=post)    # single use


def test_refresh_when_expiring(env):
    spotify.save_tokens({"access_token": "old", "refresh_token": "RT", "client_id": "cid123", "expires_at": 0})

    def post(url, data, headers, timeout):
        assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "RT"
        return Resp(200, {"access_token": "new", "expires_in": 3600})

    assert spotify.access_token(post=post) == "new"
    assert spotify.load_tokens()["refresh_token"] == "RT"


def test_request_retries_once_on_401_and_explains_errors(env):
    calls = []

    def send(method, url, params=None, json=None, headers=None, timeout=None):
        calls.append(headers["Authorization"])
        return Resp(401, {"error": {"message": "expired"}}) if len(calls) == 1 else Resp(200, {"ok": 1})

    tokens = iter(["t1", "t2"])
    assert spotify.request("GET", "/me/player", send=send, token=lambda force=False: next(tokens)) == {"ok": 1}
    assert calls == ["Bearer t1", "Bearer t2"]

    def forbidden(method, url, **kw):
        return Resp(403, {"error": {"message": "Premium required"}})
    with pytest.raises(spotify.SpotifyError, match="Premium"):
        spotify.request("PUT", "/me/player/pause", send=forbidden, token=lambda force=False: "t")


def test_ids_and_uris():
    assert spotify.spotify_id("https://open.spotify.com/intl-es/track/4uLU6hMCjMI75M1A2tKUQC?si=x", "track") == "4uLU6hMCjMI75M1A2tKUQC"
    assert spotify.spotify_uri("spotify:album:1DFixLWuPkv3KT3TnV35m3") == "spotify:album:1DFixLWuPkv3KT3TnV35m3"
    assert spotify.spotify_uri("4uLU6hMCjMI75M1A2tKUQC") == "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
    with pytest.raises(spotify.SpotifyError):
        spotify.spotify_id("not an id!", "track")


def test_tools_build_the_right_calls():
    seen = []

    def rq(method, path, params=None, body=None):
        seen.append((method, path, params, body))
        return {"ok": True}

    spotify.call("spotify_search", {"query": "Rosalía", "types": ["track", "artist", "bogus"]}, rq=rq)
    assert seen[-1][:2] == ("GET", "/search") and seen[-1][2]["type"] == "track,artist"
    spotify.call("spotify_playback", {"action": "play", "uris": ["4uLU6hMCjMI75M1A2tKUQC"]}, rq=rq)
    assert seen[-1][3] == {"uris": ["spotify:track:4uLU6hMCjMI75M1A2tKUQC"]}
    spotify.call("spotify_playback", {"action": "set_volume", "volume_percent": 150}, rq=rq)
    assert seen[-1][2]["volume_percent"] == 100
    spotify.call("spotify_playlists", {"action": "add_items", "playlist_id": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
                                       "uris": ["spotify:track:4uLU6hMCjMI75M1A2tKUQC"]}, rq=rq)
    assert seen[-1][:2] == ("POST", "/playlists/37i9dQZF1DXcBWIGoYBM5M/items")
    spotify.call("spotify_library", {"kind": "albums", "action": "save", "ids": ["1DFixLWuPkv3KT3TnV35m3"]}, rq=rq)
    assert seen[-1][2] == {"uris": "spotify:album:1DFixLWuPkv3KT3TnV35m3"}
    spotify.call("spotify_queue", {"action": "add", "uri": "4uLU6hMCjMI75M1A2tKUQC"}, rq=rq)
    assert seen[-1][:2] == ("POST", "/me/player/queue")
    with pytest.raises(spotify.SpotifyError):
        spotify.call("spotify_playback", {"action": "set_repeat", "state": "forever"}, rq=rq)


pytest.importorskip("mcp")


def test_mcp_server_lists_the_tools_and_answers_without_login(env):
    import asyncio
    import mcp_servers.spotify_server as srv
    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert names[0] == "spotify_status" and "spotify_library" in names and len(names) == 8
    res = asyncio.run(srv.call_tool("spotify_search", {"query": "x"}))
    assert res.isError and "not connected" in res.content[0].text
