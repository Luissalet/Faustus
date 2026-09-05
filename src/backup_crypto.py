"""Encryption for the full backup profile (SEC-1 / B-010).

A backup of `data/` contains `.app_key` and everything that key protects, plus
`auth.json`, `sessions.json` and the MCP OAuth files. Putting the lock and the
key in the same box is not a backup, it is a credential bundle with a tar
header — and that file then travels to a NAS, a cloud folder, a USB stick.

So the `full` profile is encrypted, always, with a key that is NEVER written
into the archive: it is derived from a passphrase the operator supplies.

The format, deliberately boring and streamable, because a backup can be
gigabytes and neither encrypting nor verifying may load it into memory:

    MAGIC        b"FAUSTUSBK1\\n"
    HEADER_LEN   4 bytes, big endian
    HEADER       JSON: kdf, iterations, salt, nonce prefix, chunk size
    CHUNK*       4-byte big-endian length, then AES-256-GCM ciphertext+tag

Each chunk gets its own nonce (`prefix || counter`, never repeated under one
key) and is authenticated with the header digest, its own counter and whether
it is the last one. That last flag is what makes truncation an error instead
of a shorter, apparently valid restore.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import Any, BinaryIO, Dict, Optional

MAGIC = b"FAUSTUSBK1\n"
DEFAULT_CHUNK = 1024 * 1024
KDF_ITERATIONS = 600_000
ENCRYPTED_SUFFIX = ".enc"


class BackupCryptoError(Exception):
    """Wrong passphrase, tampered file, truncated file, unknown format."""


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def derive_key(passphrase: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    """PBKDF2-HMAC-SHA256. A passphrase is guessable; a KDF is what makes
    guessing expensive, so the iteration count is stored with the file and
    honoured on read rather than assumed."""
    if not passphrase:
        raise BackupCryptoError("a passphrase is required for an encrypted backup")
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, int(iterations), dklen=32)


def _aad(header_digest: bytes, counter: int, final: bool) -> bytes:
    return header_digest + struct.pack(">Q?", counter, final)


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + struct.pack(">Q", counter)


def build_header(chunk: int = DEFAULT_CHUNK) -> Dict[str, Any]:
    return {
        "v": 1,
        "cipher": "aes-256-gcm",
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(os.urandom(16)).decode("ascii"),
        "nonce_prefix": base64.b64encode(os.urandom(4)).decode("ascii"),
        "chunk": int(chunk),
    }


def encrypt_stream(src: BinaryIO, dst: BinaryIO, passphrase: str,
                   *, chunk: int = DEFAULT_CHUNK) -> Dict[str, Any]:
    """Encrypt `src` into `dst`. Returns the header actually written.

    Also returns the SHA-256 of the PLAINTEXT, which the manifest records: it
    is what lets a restore prove it got the bytes that were backed up, without
    the manifest itself having to be secret.
    """
    header = build_header(chunk)
    raw = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    key = derive_key(passphrase, base64.b64decode(header["salt"]), header["iterations"])
    prefix = base64.b64decode(header["nonce_prefix"])
    aead = _aesgcm(key)

    dst.write(MAGIC)
    dst.write(struct.pack(">I", len(raw)))
    dst.write(raw)

    plain_hash = hashlib.sha256()
    counter = 0
    pending = src.read(chunk)
    if not pending:
        pending = b""
    while True:
        nxt = src.read(chunk)
        final = not nxt
        plain_hash.update(pending)
        sealed = aead.encrypt(_nonce(prefix, counter), pending, _aad(digest, counter, final))
        dst.write(struct.pack(">I", len(sealed)))
        dst.write(sealed)
        counter += 1
        if final:
            break
        pending = nxt
    return {"header": header, "chunks": counter, "sha256": plain_hash.hexdigest()}


class DecryptingReader:
    """A read-only, forward-only file object over an encrypted backup.

    `tarfile.open(fileobj=reader, mode="r|gz")` reads straight through this, so
    verifying or restoring a 4 GB encrypted snapshot costs one chunk of memory
    and no temporary plaintext copy on disk.
    """

    def __init__(self, fileobj: BinaryIO, passphrase: str):
        self._fh = fileobj
        magic = fileobj.read(len(MAGIC))
        if magic != MAGIC:
            raise BackupCryptoError("not a Faustus encrypted backup")
        (length,) = struct.unpack(">I", self._exact(4))
        raw = self._exact(length)
        try:
            self.header = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupCryptoError("unreadable backup header") from exc
        if self.header.get("cipher") != "aes-256-gcm":
            raise BackupCryptoError(f"unsupported cipher: {self.header.get('cipher')!r}")
        self._digest = hashlib.sha256(raw).digest()
        self._aead = _aesgcm(derive_key(
            passphrase, base64.b64decode(self.header["salt"]), self.header["iterations"]))
        self._prefix = base64.b64decode(self.header["nonce_prefix"])
        self._counter = 0
        self._buf = b""
        self._done = False
        self._sha = hashlib.sha256()

    def _exact(self, n: int) -> bytes:
        out = self._fh.read(n)
        if out is None or len(out) != n:
            raise BackupCryptoError("backup file ends in the middle of a record")
        return out

    def _fill(self) -> None:
        """Decrypt exactly one chunk into the buffer."""
        if self._done:
            return
        head = self._fh.read(4)
        if not head:
            # The stream ended without a chunk marked final: someone truncated
            # it, or the write was interrupted. Either way it is not a backup.
            raise BackupCryptoError("encrypted backup is truncated (no final chunk)")
        if len(head) != 4:
            raise BackupCryptoError("encrypted backup is truncated")
        (length,) = struct.unpack(">I", head)
        sealed = self._exact(length)
        from cryptography.exceptions import InvalidTag
        for final in (False, True):
            try:
                plain = self._aead.decrypt(
                    _nonce(self._prefix, self._counter), sealed,
                    _aad(self._digest, self._counter, final))
            except InvalidTag:
                continue
            self._counter += 1
            self._buf += plain
            self._sha.update(plain)
            self._done = final
            return
        raise BackupCryptoError(
            "the encrypted backup did not authenticate: wrong passphrase, or the file was altered")

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self._buf) < size) and not self._done:
            self._fill()
        if size < 0 or size >= len(self._buf):
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._fh.close()

    @property
    def sha256(self) -> str:
        """SHA-256 of the plaintext read so far — complete once the file is."""
        return self._sha.hexdigest()

    def __enter__(self) -> "DecryptingReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def is_encrypted(path: Any) -> bool:
    """Does this file start with the Faustus encrypted-backup magic?"""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def decrypt_file(src: Any, dst: Any, passphrase: str) -> Dict[str, Any]:
    """Write the plaintext archive out. For a restore, or for any tool that
    only knows how to read a tar.gz."""
    with open(src, "rb") as fh, DecryptingReader(fh, passphrase) as reader:
        with open(dst, "wb") as out:
            while True:
                block = reader.read(DEFAULT_CHUNK)
                if not block:
                    break
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        return {"ok": True, "sha256": reader.sha256, "path": str(dst)}


def passphrase_from_env(names=("FAUSTUS_BACKUP_PASSPHRASE", "ODYSSEUS_BACKUP_PASSPHRASE")) -> Optional[str]:
    """The passphrase an unattended snapshot uses, if the operator set one.

    An environment variable, not a setting: a passphrase kept in `settings.json`
    would live in the very directory the backup protects, which is the mistake
    this whole lot exists to undo.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None
