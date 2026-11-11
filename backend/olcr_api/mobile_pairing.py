"""Happier device-key signup and desktop-QR phone linking (no user login form).

Protocol pinned to happier dev 7b37d65; secrets stay in the OS credential store.
Do not substitute sealed boxes: Happier uses ephemeral key + nonce + NaCl box.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import SigningKey


class PairingError(RuntimeError):
    pass


def relay_url(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local)) or not parsed.hostname:
        raise PairingError('RELAY_HTTPS_REQUIRED')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PairingError('INVALID_RELAY_URL')
    return value.rstrip('/')


def _read_secret(key):
    import keyring
    # Fail closed: do not let keyring fall back to a plaintext backend.
    backend = keyring.get_keyring()
    if 'macOS' not in type(backend).__module__:
        raise PairingError('MACOS_KEYCHAIN_REQUIRED')
    return backend.get_password('OLCR.Mobile.Happier', key)


def _save_secret(key, value):
    import keyring
    backend = keyring.get_keyring()
    if 'macOS' not in type(backend).__module__:
        raise PairingError('MACOS_KEYCHAIN_REQUIRED')
    backend.set_password('OLCR.Mobile.Happier', key, value)


class HappierPairing:
    def __init__(self, relay, profile, *, client=None, read_secret=_read_secret, save_secret=_save_secret):
        self.relay = relay_url(relay)
        self.key = hashlib.sha256((profile + '\0' + self.relay).encode()).hexdigest()
        self.client = client
        self.read_secret = read_secret
        self.save_secret = save_secret
        self.seed = None
        self.token = None
        self.session = None
        self.lock = asyncio.Lock()

    async def _request(self, method, path, *, authenticated=True, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.token} if authenticated and self.token else {}
        try:
            if self.client:
                response = await self.client.request(method, self.relay + path, headers=headers, **kwargs)
            else:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                    response = await client.request(method, self.relay + path, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise PairingError('RELAY_TIMEOUT') from None
        except httpx.HTTPError:
            raise PairingError('RELAY_UNREACHABLE') from None
        if response.status_code >= 400:
            codes = {401: 'RELAY_AUTH_REJECTED', 403: 'RELAY_SIGNUP_OR_FEATURE_DISABLED',
                     404: 'PAIRING_EXPIRED_OR_UNSUPPORTED', 429: 'RELAY_RATE_LIMITED'}
            raise PairingError(codes.get(response.status_code, 'RELAY_ERROR'))
        try:
            return response.json()
        except ValueError:
            raise PairingError('RELAY_INVALID_RESPONSE') from None

    async def _authenticate(self):
        if self.seed is None:
            stored = await asyncio.to_thread(self.read_secret, self.key)
            if stored:
                self.seed = base64.b64decode(stored, validate=True)
            else:
                self.seed = secrets.token_bytes(32)
                await asyncio.to_thread(self.save_secret, self.key, base64.b64encode(self.seed).decode())
        key = SigningKey(self.seed)
        challenge = secrets.token_bytes(32)
        encode = lambda value: base64.b64encode(value).decode()
        result = await self._request('POST', '/v1/auth', authenticated=False,
            json={'publicKey': encode(bytes(key.verify_key)), 'challenge': encode(challenge),
                  'signature': encode(key.sign(challenge).signature)})
        self.token = result.get('token')
        if not isinstance(self.token, str) or not self.token:
            raise PairingError('RELAY_INVALID_RESPONSE')

    async def start(self):
        async with self.lock:
            await self._authenticate()
            secret = secrets.token_urlsafe(32)
            secret_hash = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode().rstrip('=')
            result = await self._request('POST', '/v1/auth/pairing/start', json={'secretHash': secret_hash})
            if not result.get('pairId') or not result.get('expiresAt'):
                raise PairingError('RELAY_INVALID_RESPONSE')
            url = 'happier:///pair?' + urlencode({'v': '1', 'pairId': result['pairId'],
                                                  'secret': secret, 'server': self.relay})
            self.session = {'id': result['pairId'], 'expires_at': result['expiresAt'], 'url': url}
            return dict(self.session)

    def _active(self):
        if not self.session:
            raise PairingError('NO_ACTIVE_PAIRING')
        expiry = datetime.fromisoformat(self.session['expires_at'].replace('Z', '+00:00'))
        if expiry <= datetime.now(timezone.utc):
            self.session = None
            raise PairingError('PAIRING_EXPIRED')
        return self.session

    async def status(self):
        session = self._active()
        result = await self._request('GET', '/v1/auth/pairing/status', params={'pairId': session['id']})
        # Do not return publicKey/token/seed to the desktop UI.
        return {'state': result['state'], 'expires_at': session['expires_at'],
                'device_label': result.get('requestedDeviceLabel'), 'confirm_code': result.get('confirmCode')}

    async def approve(self, code):
        async with self.lock:
            session = self._active()
            result = await self._request('GET', '/v1/auth/pairing/status', params={'pairId': session['id']})
            if result.get('state') != 'requested' or not hmac.compare_digest(str(result.get('confirmCode', '')), code):
                raise PairingError('CONFIRMATION_CODE_MISMATCH')
            recipient = base64.b64decode(result['requestedPublicKey'], validate=True)
            ephemeral = PrivateKey.generate()
            box = Box(ephemeral, PublicKey(recipient))
            plaintext = (b'HPAC\0' if result.get('supportsCredentialVariants') else b'') + self.seed
            encrypted = box.encrypt(plaintext)
            bundle = bytes(ephemeral.public_key) + bytes(encrypted)
            await self._request('POST', '/v1/auth/account/response', json={
                'publicKey': result['requestedPublicKey'], 'response': base64.b64encode(bundle).decode()})
            # Credentials have been released. Never allow another approval even
            # if the relay consume request fails afterwards.
            self.session = None
            await self._request('POST', '/v1/auth/pairing/consume', json={'pairId': session['id']})
            return {'state': 'approved', 'individual_revocation_supported': False}

    async def cancel(self):
        async with self.lock:
            if self.session:
                pair_id = self.session['id']
                self.session = None
                await self._request('POST', '/v1/auth/pairing/consume', json={'pairId': pair_id})
            return {'state': 'cancelled'}
