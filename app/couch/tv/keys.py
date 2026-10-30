"""Eigene Schlüssel für den Fernseher (E142) in <config>/tv: Ordner 0700, Dateien 0600.

  client.key, client.pem   RSA 2048 mit selbst signiertem Zertifikat (CN „PC5 Launcher“): Damit koppelt der PC5
                           Launcher einmal (Code am Fernseher) und meldet sich danach an der Fernbedienung an
  adbkey                   RSA 2048 (PEM) für ADB über das Netz (wahlfrei); der Fernseher fragt beim ersten Mal, ob
                           er diesen Schlüssel zulassen soll

Die Namen sind fest: Ein schon vorhandenes Paar wird übernommen (keine neue Kopplung nötig). Vorhandene Schlüssel
werden nie überschrieben und nie gelöscht („Kopplung vergessen“ vergisst nur den Fernseher in tv.json). Fehlt nur
client.pem, entsteht es neu aus client.key.

Braucht das Python-Paket cryptography (auf Bazzite vorhanden). Fehlt es, liefert available() False und die
Einstellungen sagen das (MISSING), statt abzustürzen.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import struct
from typing import Any

from .. import system
from . import TvError

CLIENT_KEY = "client.key"
CLIENT_CERT = "client.pem"
ADB_KEY = "adbkey"
COMMON_NAME = "PC5 Launcher"
ADB_KEY_NAME = b"PC5-Launcher@couch"
MISSING = ("Dafür fehlt auf diesem System das Python-Paket „cryptography“ (meist python3-cryptography). Ohne es lässt "
           "sich kein Fernseher koppeln.")
# DigestInfo von SHA-1 (PKCS#1 v1.5): adbd prüft die Unterschrift über das Token so
_SHA1_INFO = bytes.fromhex("3021300906052b0e03021a05000414")


def available() -> bool:
    try:
        import cryptography.x509  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
    except Exception:  # noqa: BLE001 (auch kaputte Installationen: dann eben nicht)
        return False
    return True


def _need() -> None:
    if not available():
        raise TvError(MISSING, "Unavailable")


def ensure_folder(folder: str) -> str:
    """Ordner anlegen bzw. auf 0700 bringen, vorhandene Dateien darin auf 0600. Liefert den Ordner."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    if os.path.islink(folder) or not os.path.isdir(folder):
        raise TvError("Der Ordner für die Schlüssel des Fernsehers ist kein Ordner.", "Unavailable")
    if os.stat(folder).st_mode & 0o077:
        os.chmod(folder, 0o700)
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not os.path.islink(path) and os.path.isfile(path) and st.st_mode & 0o077:
            os.chmod(path, 0o600)
    return folder


def _read(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as f:
        return f.read(64 * 1024)


def _new_rsa() -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def _load_private(path: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    try:
        return serialization.load_pem_private_key(_read(path), password=None)
    except (ValueError, TypeError) as e:
        raise TvError("Ein Schlüssel für den Fernseher ist unlesbar (%s)." % os.path.basename(path), "Unavailable") from e


def _self_signed(key: Any) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def has_client(folder: str) -> bool:
    return os.path.isfile(os.path.join(folder, CLIENT_KEY)) and os.path.isfile(os.path.join(folder, CLIENT_CERT))


def client_files(folder: str) -> tuple[str, str]:
    """(Zertifikat, Schlüssel); legt fehlende an. Ein vorhandener Schlüssel bleibt immer, wie er ist."""
    _need()
    ensure_folder(folder)
    key_path, cert_path = os.path.join(folder, CLIENT_KEY), os.path.join(folder, CLIENT_CERT)
    if os.path.isfile(key_path):
        key = _load_private(key_path)
    else:
        key = _new_rsa()
        system.write_atomic(key_path, _pem_private(key), mode=0o600)
    if not os.path.isfile(cert_path):
        system.write_atomic(cert_path, _self_signed(key), mode=0o600)
    return cert_path, key_path


def adb_key(folder: str) -> Any:
    """Privater ADB-Schlüssel (legt ihn beim ersten Mal an)."""
    _need()
    ensure_folder(folder)
    path = os.path.join(folder, ADB_KEY)
    if os.path.isfile(path):
        return _load_private(path)
    key = _new_rsa()
    system.write_atomic(path, _pem_private(key), mode=0o600)
    return key


def _hex_bytes(n: int) -> bytes:
    text = "%X" % n
    return bytes.fromhex(("0" + text) if len(text) % 2 else text)


def pairing_secret(client_cert_pem: bytes, server_cert_der: bytes, code: str) -> bytes:
    """Geheimnis der Kopplung: SHA-256 über Modulus und Exponent beider öffentlicher Schlüssel (Hex wie im Protokoll,
    der Exponent mit führender Null) und die letzten zwei Bytes des Codes. Das erste Byte muss gleich den ersten
    beiden Zeichen des Codes sein (fängt Tippfehler ab, check_code)."""
    from cryptography import x509
    client = x509.load_pem_x509_certificate(client_cert_pem).public_key().public_numbers()
    server = x509.load_der_x509_certificate(server_cert_der).public_key().public_numbers()
    h = hashlib.sha256()
    for number in (client.n, client.e, server.n, server.e):     # 65537 -> 01 00 01, wie im Protokoll
        h.update(_hex_bytes(number))
    h.update(bytes.fromhex(code[2:]))
    return h.digest()


def check_code(secret: bytes, code: str) -> bool:
    return bool(secret) and secret[0] == int(code[:2], 16)


def android_pubkey(key: Any) -> bytes:
    """Öffentlicher Schlüssel im Format von Androids android_pubkey_encode, base64, Name und NUL (ADB AUTH 3)."""
    import base64
    n = key.public_key().public_numbers().n
    words = 2048 // 32
    n0inv = (-pow(n % 2 ** 32, -1, 2 ** 32)) % 2 ** 32
    rr = pow(2, 4096, n)
    blob = struct.pack("<II", words, n0inv) + n.to_bytes(256, "little") + rr.to_bytes(256, "little") \
        + struct.pack("<I", 65537)
    return base64.b64encode(blob) + b" " + ADB_KEY_NAME + b"\x00"


def sign_token(key: Any, token: bytes) -> bytes:
    """RSA PKCS#1 v1.5 über das Token als SHA-1-Wert (so prüft adbd). Von Hand gerechnet: Manche Systeme (Fedora)
    sperren SHA-1 beim Signieren in OpenSSL per Krypto-Vorgabe."""
    nums = key.private_numbers()
    n, d = nums.public_numbers.n, nums.d
    size = (n.bit_length() + 7) // 8
    info = _SHA1_INFO + bytes(token)[:20]
    em = b"\x00\x01" + b"\xff" * (size - 3 - len(info)) + b"\x00" + info
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(size, "big")


def cert_pem(folder: str) -> bytes:
    return _read(os.path.join(folder, CLIENT_CERT))
