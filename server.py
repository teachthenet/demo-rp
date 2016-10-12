#!/usr/bin/env python3

from base64 import urlsafe_b64decode
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen
from uuid import uuid4
import json
import os
import re

from bottle import (
    Bottle, redirect, request, response, static_file, template
)
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = json.load(open(os.path.join(DIR, 'config.json')))

# Identity tokens expire after a few minutes, but might be reused while valid.
#
# To defend against replay attacks, sites can optionally supply a nonce during
# authentication which is echoed back in the identity token.
#
# For simplicity, this example uses a plain Python dict. This approach breaks
# in multi-threaded environments. In production, use a real database for this.

NONCES = {}

app = Bottle()


@app.get('/')
def index():
    return template('index')


@app.post('/login')
def login():
    # Read the user's email address from the POSTed form data
    email = request.forms['email']

    # Generate and store a nonce to uniquely identify this login request.
    # This allows us to prevent identity tokens from being used more than once.
    nonce = uuid4().hex
    expiry = datetime.utcnow() + timedelta(minutes=30)
    NONCES[nonce] = expiry.timestamp()

    # Forward the user to the broker, along with all necessary parameters
    auth_url = '%s/auth?%s' % (
        CONFIG['portier_origin'],
        urlencode({
            'login_hint': email,
            'scope': 'openid email',
            'nonce': nonce,
            'response_type': 'id_token',
            'client_id': CONFIG['rp_origin'],
            'redirect_uri': '%s/verify' % CONFIG['rp_origin']
        })
    )
    return redirect(auth_url)


@app.post('/verify')
def verify():
    # Read the signed identity token from the POSTed form data
    token = request.forms['id_token']

    try:
        email = get_verified_email(token)
    except RuntimeError as exc:
        response.status = 400
        return template('error', error=exc)

    # At this stage, the user is verified to own the email address. This is
    # where you'd set a cookie to maintain a session in your app. Be sure to
    # restrict the cookie to your secure origin, with the http-only flag set.
    return template('verified', email=email)


@app.get('/static/<path:path>')
def static(path):
    return static_file(path, os.path.join(DIR, 'static'))


def b64dec(s):
    return urlsafe_b64decode(s.encode('ascii') + b'=' * (4 - len(s) % 4))


def discover_keys(broker):
    """Discover and return the broker's public keys"""""

    # Fetch the OpenID Connect Dynamic Discovery document
    res = urlopen(''.join((broker, '/.well-known/openid-configuration')))
    discovery = json.loads(res.read().decode('utf-8'))
    if 'jwks_uri' not in discovery:
        raise RuntimeError('No jwks_uri in discovery document')

    # Fetch the JWK Set document
    res = urlopen(discovery['jwks_uri'])
    jwks = json.loads(res.read().decode('utf-8'))
    if 'keys' not in jwks:
        raise RuntimeError('No keys found in JWK Set')

    # Return the discovered keys as a Key ID -> RSA Public Key dictionary
    return {key['kid']: jwk_to_rsa(key) for key in jwks['keys']
            if key['alg'] == 'RS256'}


def jwk_to_rsa(key):
    """Convert a deserialized JWK into an RSA Public Key instance"""
    e = int.from_bytes(b64dec(key['e']), 'big')
    n = int.from_bytes(b64dec(key['n']), 'big')
    return rsa.RSAPublicNumbers(e, n).public_key(default_backend())


def get_verified_email(token):
    # Discover and deserialize the key used to sign this JWT
    keys = discover_keys(CONFIG['portier_origin'])

    raw_header, _, _ = token.partition('.')
    header = json.loads(b64dec(raw_header).decode('utf-8'))
    try:
        pub_key = keys[header['kid']]
    except KeyError:
        raise RuntimeError('Cannot find key with ID %s' % header['kid'])

    # We must ensure that all JWTs have a valid cryptographic signature.
    # Portier only supports OpenID Connect's default signing algorithm: RS256.
    #
    # OpenID Connect's JWTs also have five required claims that we must verify:
    #
    # - `aud` (audience) must match this website's origin.
    # - `iss` (issuer) must match the broker's origin.
    # - `exp` (expires) must be in the future.
    # - `iat` (issued at) must be in the past.
    # - `sub` (subject) must be an email address.
    #
    # The following, optional claims may also appear in the JWT payload:
    #
    # - `nbf` (not before) must be in the past.
    # - `nonce` (cryptographic nonce) must not have been seen previously.
    #
    # We delegate to PyJWT, which checks signatures and validates all claims
    # except `sub` and `nonce`. Timestamps are allowed a small margin of error.
    #
    # More info at: https://github.com/jpadilla/pyjwt
    try:
        payload = jwt.decode(token, pub_key,
                             algorithms=['RS256'],
                             audience=CONFIG['rp_origin'],
                             issuer=CONFIG['portier_origin'],
                             leeway=3 * 60)
    except Exception as exc:
        raise RuntimeError('Invalid JWT: %s' % exc)

    # Check that the subject looks like an email address
    subject = payload['sub']
    if not re.match('.+@.+', subject):
        raise RuntimeError('Invalid email address: %s' % subject)

    # Remove / garbage collect expired nonces
    global NONCES
    NONCES = {nonce: expiry for nonce, expiry in NONCES.items()
              if expiry >= datetime.utcnow().timestamp()}

    # Invalidate this nonce by removing it from NONCES
    try:
        NONCES.pop(payload['nonce'])
    except KeyError:
        raise RuntimeError('Invalid or expired nonce')

    return subject


if __name__ == '__main__':
    host, port = urlparse(CONFIG['rp_origin']).netloc.split(':')
    app.run(host=host, port=port)
