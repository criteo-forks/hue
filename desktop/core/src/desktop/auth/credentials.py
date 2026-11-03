#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Password kept at login for credential passthrough (SESSION.STORE_USER_PASSWORD).

The password is encrypted with a random per-login key: the ciphertext is stored in the
Django session (database), the key only in a browser cookie. Neither the database (or its
backups) nor the server alone can recover the password, and a session row outliving its
cookie becomes undecryptable.
"""

import base64
import logging
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

LOG = logging.getLogger()

COOKIE_NAME = 'hue_credential_key'
SESSION_KEY = 'password_enc'

_VERSION = b'\x01'
_NONCE_SIZE = 12
_UNAVAILABLE_MESSAGE = 'Your stored credentials are no longer available, please log out and log in again.'


def _associated_data(user):
  # Binds the ciphertext to its user: a ciphertext copied into another user's session does not decrypt.
  return ('hue-credential:v1:%s' % user.pk).encode('ascii')


def store_password(request, password):
  """Encrypts the password into the session of the user who just logged in."""
  key = AESGCM.generate_key(bit_length=256)
  nonce = secrets.token_bytes(_NONCE_SIZE)
  ciphertext = AESGCM(key).encrypt(nonce, password.encode('utf-8'), _associated_data(request.user))

  request.session[SESSION_KEY] = base64.b64encode(_VERSION + nonce + ciphertext).decode('ascii')
  # Set on the response by CredentialCookieMiddleware, once the session cookie is known.
  request.credential_key = base64.urlsafe_b64encode(key).decode('ascii')


def get_password(request):
  """
  Returns the password stored at login, or None if none was stored.

  Raises QueryError when a password was stored but cannot be decrypted (key cookie missing, or data
  not matching it). Not AuthenticationRequired: the editor answers it with a credentials prompt whose
  input never reaches the session, so the user would be asked again and again. Only a new login fixes it.
  """
  session = getattr(request, 'session', None)
  blob = session.get(SESSION_KEY) if session is not None else None
  if not blob:
    return None

  key = getattr(request, 'credential_key', None) or request.COOKIES.get(COOKIE_NAME)
  try:
    if not key:
      raise ValueError('no key cookie')
    raw = base64.b64decode(blob)
    if raw[:1] != _VERSION:
      raise ValueError('unknown format')
    nonce, ciphertext = raw[1:1 + _NONCE_SIZE], raw[1 + _NONCE_SIZE:]
    return AESGCM(base64.urlsafe_b64decode(key)).decrypt(nonce, ciphertext, _associated_data(request.user)).decode('utf-8')
  except (InvalidTag, ValueError) as e:  # binascii.Error and UnicodeDecodeError are ValueErrors
    LOG.warning('Stored credentials of user %s cannot be used: %s' % (request.user.username, e or type(e).__name__))
    from notebook.connectors.base import QueryError  # notebook depends on desktop, not the other way around
    raise QueryError(_UNAVAILABLE_MESSAGE)


class CredentialCookieMiddleware(MiddlewareMixin):
  """
  Keeps the key cookie in step with the session cookie: same lifetime and attributes, deleted along with it.

  Must be listed before SessionMiddleware, so that it runs after it on the response and sees the session
  cookie it sets or deletes.
  """

  def process_response(self, request, response):
    session_cookie = response.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_cookie is None:
      return response

    if str(session_cookie['max-age']) == '0':  # SessionMiddleware deleted the session cookie, e.g. on logout
      if COOKIE_NAME in request.COOKIES:
        response.delete_cookie(
          COOKIE_NAME, path=settings.SESSION_COOKIE_PATH, domain=settings.SESSION_COOKIE_DOMAIN,
          samesite=settings.SESSION_COOKIE_SAMESITE,
        )
      return response

    key = getattr(request, 'credential_key', None) or request.COOKIES.get(COOKIE_NAME)
    session = getattr(request, 'session', None)
    if key and session is not None and session.get(SESSION_KEY):
      max_age = session_cookie['max-age']
      response.set_cookie(
        COOKIE_NAME, key,
        max_age=int(max_age) if max_age != '' else None,  # No max-age: browser-session cookie, like the session one
        path=settings.SESSION_COOKIE_PATH, domain=settings.SESSION_COOKIE_DOMAIN,
        secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite=settings.SESSION_COOKIE_SAMESITE,
      )
    return response
