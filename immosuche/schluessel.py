"""Verschlüsselte E-Mail-Empfänger.

Das Repository ist öffentlich. Die Karte verschlüsselt eingegebene Adressen deshalb im Browser mit dem
öffentlichen Schlüssel (schluessel/oeffentlich.pem, RSA-OAEP mit SHA-256). Nur der Suchlauf kann sie
entschlüsseln: Der private Schlüssel liegt in schluessel/privat.enc, selbst verschlüsselt mit einem
Schlüssel, der aus dem GitHub-Secret MAIL_PASSWORD abgeleitet wird.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger(__name__)
OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _kdf(geheim: str, salz: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salz, iterations=300_000).derive(geheim.encode())


def laden_oder_erzeugen(ordner: Path, geheim: str):
    """Liefert (privater Schlüssel oder None, öffentlicher Schlüssel als PEM-Text)."""
    pub_datei, priv_datei = ordner / "oeffentlich.pem", ordner / "privat.enc"
    if not geheim:
        return None, pub_datei.read_text() if pub_datei.exists() else ""
    if pub_datei.exists() and priv_datei.exists():
        d = json.loads(priv_datei.read_text())
        try:
            roh = AESGCM(_kdf(geheim, base64.b64decode(d["salz"]))).decrypt(
                base64.b64decode(d["nonce"]), base64.b64decode(d["daten"]), None)
            return serialization.load_der_private_key(roh, None), pub_datei.read_text()
        except Exception:
            log.warning("Privater Schlüssel nicht lesbar (Passwort geändert?); erzeuge neuen. "
                        "Bereits gespeicherte Empfänger müssen neu eingegeben werden.")
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    salz, nonce = os.urandom(16), os.urandom(12)
    roh = priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    ordner.mkdir(parents=True, exist_ok=True)
    priv_datei.write_text(json.dumps({"salz": _b64(salz), "nonce": _b64(nonce),
                                      "daten": _b64(AESGCM(_kdf(geheim, salz)).encrypt(nonce, roh, None))}))
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    pub_datei.write_text(pem)
    return priv, pem


def entschluesseln(priv, geheimtext: str) -> str | None:
    try:
        return priv.decrypt(base64.b64decode(geheimtext), OAEP).decode().strip()
    except Exception:
        return None


def verschluesseln(pem: str, text: str) -> str:
    """Nur für Tests; im Betrieb verschlüsselt der Browser."""
    pub = serialization.load_pem_public_key(pem.encode())
    return _b64(pub.encrypt(text.encode(), OAEP))
