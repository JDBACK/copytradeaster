"""AES-256-GCM encryption for credential blobs.

Blob layout: nonce(12B) || ciphertext+tag. Key must be exactly 32 bytes.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


def encrypt(key: bytes, plaintext: bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("encryption key must be 32 bytes")
    nonce = os.urandom(_NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def decrypt(key: bytes, blob: bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("encryption key must be 32 bytes")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)
