"""Private Bilibili web credentials; no browser access or ASR side effects."""
from __future__ import annotations

import json
import asyncio
import argparse
import fcntl
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


PASSPORT = 'https://passport.bilibili.com/x/passport-login/web'
NAV = 'https://api.bilibili.com/x/web-interface/nav'
# Protocol reference: amtoaer/bili-sync c900777e78796653fdfb00421879b6a802d1eedd.
# Bilibili's public protocol key, not a private credential.
REFRESH_PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----'''


def validate_shape(data: dict) -> dict:
    cookies = data['cookies']
    if not all(cookies.get(name) for name in ('SESSDATA', 'bili_jct', 'DedeUserID')) or not data.get('refresh_token'):
        raise ValueError('BILIBILI_INVALID_CREDENTIALS')
    tokens = [('refresh_token', data['refresh_token'])]
    if data.get('pending_confirmation'):
        tokens.append(('pending_confirmation', data['pending_confirmation']))
    for name, value in [*cookies.items(), *tokens]:
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]+', name) or not isinstance(value, str) or any(c in value for c in '\r\n;'):
            raise ValueError('BILIBILI_INVALID_CREDENTIALS')
    return data


def load_credentials(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 16384:
        raise ValueError('BILIBILI_UNSAFE_CREDENTIAL_FILE')
    return validate_shape(json.loads(path.read_text()))


def save_credentials(path: Path, data: dict) -> None:
    validate_shape(data)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(path, data)


def atomic_json(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cookie_header(data: dict) -> str:
    return '; '.join(f'{name}={value}' for name, value in data['cookies'].items())


class BilibiliCredentials:
    def __init__(self, client: httpx.AsyncClient, path: Path):
        self.client, self.path = client, path

    async def request(self, method: str, url: str, credential: dict | None = None, **kwargs) -> httpx.Response:
        headers = {'Cookie': cookie_header(credential)} if credential else {}
        response = await self.client.request(method, url, headers=headers, follow_redirects=False, **kwargs)
        if response.is_redirect:
            raise ValueError('BILIBILI_LOGIN_REDIRECT_REJECTED')
        response.raise_for_status()
        return response

    @staticmethod
    def data(response: httpx.Response) -> dict:
        payload = response.json()
        if payload.get('code') != 0:
            raise ValueError('BILIBILI_LOGIN_REQUIRED' if payload.get('code') == -101 else 'BILIBILI_AUTH_API_ERROR')
        return payload.get('data') or {}

    async def validate(self, credential: dict) -> None:
        data = self.data(await self.request('GET', NAV, credential))
        if not data.get('isLogin') or str(data.get('mid')) != credential['cookies']['DedeUserID']:
            raise ValueError('BILIBILI_LOGIN_IDENTITY_MISMATCH')

    async def generate_qr(self) -> dict:
        data = self.data(await self.request('GET', PASSPORT + '/qrcode/generate'))
        url = httpx.URL(data['url'])
        if url.scheme != 'https' or url.host not in ('passport.bilibili.com', 'account.bilibili.com') or url.port not in (None, 443) or url.username or url.password:
            raise ValueError('BILIBILI_QR_URL_REJECTED')
        return {'url': data['url'], 'qrcode_key': data['qrcode_key']}

    async def poll_qr(self, key: str) -> str:
        response = await self.request('GET', PASSPORT + '/qrcode/poll', params={'qrcode_key': key})
        data = self.data(response)
        pending = {86101: 'pending', 86090: 'scanned', 86038: 'expired'}
        if data.get('code') in pending:
            return pending[data['code']]
        if data.get('code') != 0:
            raise ValueError('BILIBILI_AUTH_API_ERROR')
        credential = validate_shape({'cookies': dict(response.cookies), 'refresh_token': data['refresh_token']})
        await self.validate(credential)
        save_credentials(self.path, credential)
        return 'logged_in'

    async def refresh_info(self, credential: dict) -> dict:
        info = self.data(await self.request('GET', PASSPORT + '/cookie/info', credential,
                                            params={'csrf': credential['cookies']['bili_jct']}))
        if not isinstance(info.get('refresh'), bool):
            raise ValueError('BILIBILI_AUTH_API_ERROR')
        return info

    async def confirm(self, credential: dict) -> bool:
        response = await self.request('POST', PASSPORT + '/confirm/refresh', credential,
                                    data={'csrf': credential['cookies']['bili_jct'],
                                          'refresh_token': credential['pending_confirmation']})
        confirmed = response.json().get('code') != -101
        if confirmed:
            self.data(response)
            credential.pop('confirmation_unavailable', None)
        else:
            # Official web code retains the new login even if old-session
            # confirmation fails. Verify BOTH login APIs before doing the same;
            # never claim the old session was invalidated.
            await self.validate(credential)
            await self.refresh_info(credential)
            credential['confirmation_unavailable'] = True
        credential.pop('pending_confirmation')
        save_credentials(self.path, credential)
        return confirmed

    async def check(self, *, force: bool = False) -> str:
        credential = load_credentials(self.path)
        if credential.get('refresh_started'):
            raise ValueError('BILIBILI_REFRESH_UNCERTAIN')
        await self.validate(credential)
        if credential.get('pending_confirmation'):
            return 'confirmed' if await self.confirm(credential) else 'valid_confirmation_unavailable'
        info = await self.refresh_info(credential)
        if not info['refresh'] and not force:
            return 'valid_confirmation_unavailable' if credential.get('confirmation_unavailable') else 'valid'
        key = serialization.load_pem_public_key(REFRESH_PUBLIC_KEY)
        encrypted = key.encrypt(f"refresh_{int(info['timestamp'])}".encode(),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        response = await self.request('GET', 'https://www.bilibili.com/correspond/1/' + encrypted.hex(), credential)
        match = re.search(r'<div id="1-name">([^<]+)</div>', response.text)
        if not match:
            raise ValueError('BILIBILI_REFRESH_CSRF_MISSING')
        # A POST may have succeeded despite a lost response: never replay it blindly.
        credential['refresh_started'] = True
        save_credentials(self.path, credential)
        response = await self.request('POST', PASSPORT + '/cookie/refresh', credential,
            data={'csrf': credential['cookies']['bili_jct'], 'refresh_csrf': match[1],
                  'refresh_token': credential['refresh_token'], 'source': 'main_web'})
        data = self.data(response)
        fresh_cookies = dict(response.cookies)
        if not all(fresh_cookies.get(name) for name in ('SESSDATA', 'bili_jct', 'DedeUserID')):
            raise ValueError('BILIBILI_INVALID_CREDENTIALS')
        new = {'cookies': {**credential['cookies'], **fresh_cookies},
               'refresh_token': data['refresh_token'], 'pending_confirmation': credential['refresh_token']}
        if new['cookies']['DedeUserID'] != credential['cookies']['DedeUserID']:
            raise ValueError('BILIBILI_LOGIN_IDENTITY_MISMATCH')
        save_credentials(self.path, new)
        await self.validate(new)
        return 'refreshed' if await self.confirm(new) else 'refreshed_confirmation_unavailable'


async def run_command(args) -> str:
    path = args.file
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = path.with_suffix('.qr.json')
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with httpx.AsyncClient(timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 ZephyrMCP/0.1', 'Referer': 'https://www.bilibili.com/'}) as client:
            login = BilibiliCredentials(client, path)
            if args.command == 'generate':
                atomic_json(session, await login.generate_qr())
                return 'qr_ready'
            if args.command == 'login':
                key = json.loads(session.read_text())['qrcode_key']
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    state = await login.poll_qr(key)
                    if state in ('logged_in', 'expired'):
                        session.unlink(missing_ok=True)
                        return state
                    await asyncio.sleep(2)
                session.unlink(missing_ok=True)
                return 'expired'
            return await login.check(force=args.force)
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description='Private Bilibili QR login and cookie refresh')
    parser.add_argument('command', choices=('generate', 'login', 'check'))
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--force', action='store_true', help='One-time operator refresh acceptance only')
    args = parser.parse_args()
    try:
        state = asyncio.run(run_command(args))
        result = {'state': state, 'checked_at': datetime.now(timezone.utc).isoformat()}
        if args.command == 'check':
            atomic_json(args.file.with_suffix('.status.json'), result)
        print(json.dumps(result), flush=True)
        if state == 'expired':
            raise SystemExit(1)
    except Exception:
        # HTTP errors can contain signed URLs; parsing errors can contain tokens.
        result = {'state': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat()}
        if args.command == 'check' and args.file.parent.is_dir():
            atomic_json(args.file.with_suffix('.status.json'), result)
        print(json.dumps(result), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
