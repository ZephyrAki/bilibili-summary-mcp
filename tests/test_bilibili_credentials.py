import json

import httpx
import pytest
from urllib.parse import parse_qs

from bilibili_summary_mcp.bilibili_credentials import BilibiliCredentials
from bilibili_summary_mcp.bilibili import BilibiliClient
from bilibili_summary_mcp.errors import MCPError


def credentials(tmp_path):
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({'cookies': {'SESSDATA': 'old-session', 'bili_jct': 'old-csrf',
                                          'DedeUserID': '123'}, 'refresh_token': 'old-refresh'}))
    path.chmod(0o600)
    return path


async def test_check_keeps_credentials_when_platform_does_not_require_refresh(tmp_path):
    path = credentials(tmp_path)
    before = path.read_bytes()

    def upstream(request):
        assert request.method == 'GET'
        assert request.url.host in ('api.bilibili.com', 'passport.bilibili.com')
        assert 'SESSDATA=old-session' in request.headers['cookie']
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        return httpx.Response(200, json={'code': 0, 'data': {'refresh': False, 'timestamp': 1700000000000}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        assert await BilibiliCredentials(client, path).check() == 'valid'
    assert path.read_bytes() == before


async def test_logged_out_credentials_do_not_trigger_refresh_posts(tmp_path):
    path = credentials(tmp_path)
    before = path.read_bytes()

    def upstream(request):
        assert request.method == 'GET'
        return httpx.Response(200, json={'code': -101, 'message': 'not logged in'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(ValueError, match='BILIBILI_LOGIN_REQUIRED'):
            await BilibiliCredentials(client, path).check()
    assert path.read_bytes() == before


async def test_refresh_saves_new_credentials_before_invalidating_old_session(tmp_path):
    path = credentials(tmp_path)

    def upstream(request):
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        if request.url.path.endswith('/cookie/info'):
            return httpx.Response(200, json={'code': 0, 'data': {'refresh': True, 'timestamp': 1700000000000}})
        if '/correspond/1/' in request.url.path:
            assert len(bytes.fromhex(request.url.path.rsplit('/', 1)[1])) == 128
            return httpx.Response(200, text='<div id="1-name">refresh-csrf</div>')
        if request.url.path.endswith('/cookie/refresh'):
            form = parse_qs(request.content.decode())
            assert form == {'csrf': ['old-csrf'], 'refresh_csrf': ['refresh-csrf'],
                            'refresh_token': ['old-refresh'], 'source': ['main_web']}
            return httpx.Response(200, json={'code': 0, 'data': {'refresh_token': 'new-refresh'}},
                headers=[('set-cookie', 'SESSDATA=new-session; Path=/; Domain=.bilibili.com'),
                         ('set-cookie', 'bili_jct=new-csrf; Path=/; Domain=.bilibili.com'),
                         ('set-cookie', 'DedeUserID=123; Path=/; Domain=.bilibili.com')])
        assert request.url.path.endswith('/confirm/refresh')
        saved = json.loads(path.read_text())
        assert saved['refresh_token'] == 'new-refresh'
        assert saved['pending_confirmation'] == 'old-refresh'
        assert path.stat().st_mode & 0o777 == 0o600
        assert 'SESSDATA=new-session' in request.headers['cookie']
        assert parse_qs(request.content.decode()) == {'csrf': ['new-csrf'], 'refresh_token': ['old-refresh']}
        return httpx.Response(200, json={'code': 0, 'data': {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        assert await BilibiliCredentials(client, path).check() == 'refreshed'
    saved = json.loads(path.read_text())
    assert saved['cookies']['SESSDATA'] == 'new-session'
    assert saved['refresh_token'] == 'new-refresh'
    assert 'pending_confirmation' not in saved


async def test_uncertain_refresh_is_not_blindly_submitted_again(tmp_path):
    path = credentials(tmp_path)
    data = json.loads(path.read_text()); data['refresh_started'] = True
    path.write_text(json.dumps(data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('No repeat request'))) as client:
        with pytest.raises(ValueError, match='BILIBILI_REFRESH_UNCERTAIN'):
            await BilibiliCredentials(client, path).check()


async def test_lost_refresh_response_records_uncertainty_before_post(tmp_path):
    path = credentials(tmp_path)
    def upstream(request):
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        if request.url.path.endswith('/cookie/info'):
            return httpx.Response(200, json={'code': 0, 'data': {'refresh': True, 'timestamp': 1700000000000}})
        if '/correspond/1/' in request.url.path:
            return httpx.Response(200, text='<div id="1-name">refresh-csrf</div>')
        assert request.url.path.endswith('/cookie/refresh')
        assert json.loads(path.read_text())['refresh_started'] is True
        raise httpx.ReadTimeout('response lost', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        login = BilibiliCredentials(client, path)
        with pytest.raises(httpx.ReadTimeout):
            await login.check()
        with pytest.raises(ValueError, match='BILIBILI_REFRESH_UNCERTAIN'):
            await login.check()
    assert json.loads(path.read_text())['refresh_token'] == 'old-refresh'


async def test_pending_confirmation_recovers_without_another_refresh(tmp_path):
    path = credentials(tmp_path)
    data = json.loads(path.read_text()); data['pending_confirmation'] = 'previous-refresh'
    path.write_text(json.dumps(data))

    def upstream(request):
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        assert request.url.path.endswith('/confirm/refresh')
        assert parse_qs(request.content.decode())['refresh_token'] == ['previous-refresh']
        return httpx.Response(200, json={'code': 0, 'message': '0', 'ttl': 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        assert await BilibiliCredentials(client, path).check() == 'confirmed'
    assert 'pending_confirmation' not in json.loads(path.read_text())


@pytest.mark.parametrize('info_code', [0, -101])
async def test_rejected_confirmation_requires_current_passport_login(tmp_path, info_code):
    path = credentials(tmp_path)
    data = json.loads(path.read_text()); data['pending_confirmation'] = 'previous-refresh'
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    def upstream(request):
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        if request.url.path.endswith('/confirm/refresh'):
            return httpx.Response(200, json={'code': -101})
        assert request.url.path.endswith('/cookie/info')
        return httpx.Response(200, json={'code': info_code, 'data': {'refresh': False}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        login = BilibiliCredentials(client, path)
        if info_code:
            with pytest.raises(ValueError, match='BILIBILI_LOGIN_REQUIRED'):
                await login.check()
            assert path.read_bytes() == before
        else:
            assert await login.check() == 'valid_confirmation_unavailable'
            saved = json.loads(path.read_text())
            assert saved['refresh_token'] == 'old-refresh'
            assert saved['confirmation_unavailable'] is True
            assert 'pending_confirmation' not in saved


async def test_confirmation_warning_does_not_block_next_needed_refresh(tmp_path):
    path = credentials(tmp_path)
    data = json.loads(path.read_text()); data['confirmation_unavailable'] = True
    path.write_text(json.dumps(data))
    paths = []
    def upstream(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        if request.url.path.endswith('/cookie/info'):
            return httpx.Response(200, json={'code': 0, 'data': {'refresh': True, 'timestamp': 1700000000000}})
        if '/correspond/1/' in request.url.path:
            return httpx.Response(200, text='<div id="1-name">refresh-csrf</div>')
        if request.url.path.endswith('/cookie/refresh'):
            return httpx.Response(200, json={'code': 0, 'data': {'refresh_token': 'next-refresh'}},
                headers=[('set-cookie', 'SESSDATA=next-session; Path=/; Domain=.bilibili.com'),
                         ('set-cookie', 'bili_jct=next-csrf; Path=/; Domain=.bilibili.com'),
                         ('set-cookie', 'DedeUserID=123; Path=/; Domain=.bilibili.com')])
        assert request.url.path.endswith('/confirm/refresh')
        return httpx.Response(200, json={'code': 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        assert await BilibiliCredentials(client, path).check() == 'refreshed'
    assert paths.count('/x/passport-login/web/cookie/refresh') == 1
    saved = json.loads(path.read_text())
    assert saved['refresh_token'] == 'next-refresh'
    assert 'confirmation_unavailable' not in saved


@pytest.mark.parametrize('code', [-400, -500])
async def test_other_confirmation_errors_keep_recovery_token(tmp_path, code):
    path = credentials(tmp_path)
    data = json.loads(path.read_text()); data['pending_confirmation'] = 'previous-refresh'
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    def upstream(request):
        if request.url.path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        assert request.url.path.endswith('/confirm/refresh')
        return httpx.Response(200, json={'code': code})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(ValueError, match='BILIBILI_AUTH_API_ERROR'):
            await BilibiliCredentials(client, path).check()
    assert path.read_bytes() == before


async def test_qr_pending_does_not_replace_existing_login_and_success_saves_refresh_token(tmp_path):
    path = credentials(tmp_path)
    before = path.read_bytes()
    responses = iter([
        httpx.Response(200, json={'code': 0, 'data': {'code': 86090, 'refresh_token': ''}}),
        httpx.Response(200, json={'code': 0, 'data': {'code': 0, 'refresh_token': 'qr-refresh'}},
            headers=[('set-cookie', 'SESSDATA=qr-session; Path=/; Domain=.bilibili.com'),
                     ('set-cookie', 'bili_jct=qr-csrf; Path=/; Domain=.bilibili.com'),
                     ('set-cookie', 'DedeUserID=123; Path=/; Domain=.bilibili.com')])])

    def upstream(request):
        if request.url.path.endswith('/nav'):
            assert 'SESSDATA=qr-session' in request.headers['cookie']
            return httpx.Response(200, json={'code': 0, 'data': {'isLogin': True, 'mid': 123}})
        assert request.url.params['qrcode_key'] == 'private-key'
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        login = BilibiliCredentials(client, path)
        assert await login.poll_qr('private-key') == 'scanned'
        assert path.read_bytes() == before
        assert await login.poll_qr('private-key') == 'logged_in'
    saved = json.loads(path.read_text())
    assert saved['cookies']['SESSDATA'] == 'qr-session'
    assert saved['refresh_token'] == 'qr-refresh'
    assert path.stat().st_mode & 0o777 == 0o600


async def test_bilibili_requests_use_rotated_file_without_restart_and_strip_cdn_cookie(tmp_path):
    path = credentials(tmp_path)
    seen = []
    client = BilibiliClient('SESSDATA=everyday-browser', credentials_path=path)
    hooks, headers = client.client.event_hooks, client.client.headers
    await client.client.aclose()
    def upstream(request):
        seen.append(request.headers.get('cookie'))
        return httpx.Response(200, json={'code': 0, 'data': {'cid': 1}})
    client.client = httpx.AsyncClient(headers=headers, event_hooks=hooks, transport=httpx.MockTransport(upstream))
    try:
        await client.player_info('BV1uJ411k7wy', 1)
        data = json.loads(path.read_text()); data['cookies']['SESSDATA'] = 'rotated-session'
        path.write_text(json.dumps(data))
        await client.player_info('BV1uJ411k7wy', 1)
        await client.client.get('https://upos-sz-mirrorcos.bilivideo.com/subtitle')
    finally:
        await client.close()
    assert 'SESSDATA=old-session' in seen[0]
    assert 'SESSDATA=rotated-session' in seen[1]
    assert seen[2] is None


async def test_missing_configured_credentials_never_fall_back_to_everyday_cookie(tmp_path):
    client = BilibiliClient('SESSDATA=everyday-browser', credentials_path=tmp_path/'missing.json')
    try:
        with pytest.raises(MCPError, match='专用登录'):
            await client.player_info('BV1uJ411k7wy', 1)
    finally:
        await client.close()


@pytest.mark.parametrize('url', ['http://api.bilibili.com/nav', 'https://api.bilibili.com:8443/nav'])
async def test_private_cookie_is_only_sent_to_official_https_port(tmp_path, url):
    path = credentials(tmp_path)
    client = BilibiliClient(None, credentials_path=path)
    request = httpx.Request('GET', url, headers={'Cookie': 'SESSDATA=stale'})
    try:
        await client._apply_credentials(request)
        assert 'cookie' not in request.headers
    finally:
        await client.close()


async def test_qr_generation_rejects_off_platform_url(tmp_path):
    response = httpx.Response(200, json={'code': 0, 'data': {'url': 'https://evil.example/scan', 'qrcode_key': 'key'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(ValueError, match='BILIBILI_QR_URL_REJECTED'):
            await BilibiliCredentials(client, tmp_path/'credentials.json').generate_qr()


async def test_qr_accepts_current_official_account_host(tmp_path):
    response = httpx.Response(200, json={'code': 0, 'data': {
        'url': 'https://account.bilibili.com/h5/account-h5/auth/scan-web?ticket=private-ticket', 'qrcode_key': 'key'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        qr = await BilibiliCredentials(client, tmp_path/'credentials.json').generate_qr()
    assert qr['qrcode_key'] == 'key'


def test_cli_errors_never_print_private_http_details(tmp_path, monkeypatch, capsys):
    import bilibili_summary_mcp.bilibili_credentials as module
    def fail(coroutine):
        coroutine.close()
        raise ValueError('Cookie=private-secret; refresh_token=private-secret')
    monkeypatch.setattr(module.asyncio, 'run', fail)
    monkeypatch.setattr('sys.argv', ['credentials', 'check', '--file', str(tmp_path/'credentials.json')])
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert exited.value.code == 1
    output = capsys.readouterr()
    assert 'private-secret' not in output.out + output.err
    assert 'failed' in output.out


async def test_private_file_with_public_permissions_is_rejected(tmp_path):
    path = credentials(tmp_path); path.chmod(0o644)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('Must not send credentials'))) as client:
        with pytest.raises(ValueError, match='BILIBILI_UNSAFE_CREDENTIAL_FILE'):
            await BilibiliCredentials(client, path).check()
