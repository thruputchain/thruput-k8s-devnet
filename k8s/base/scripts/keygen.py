#!/usr/bin/env python3
"""Generate deployment identities, or create missing Secrets in-cluster."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
SECRET_NAMES = ("thruput-validator-key", "thruput-faucet-key")


def base58(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    result = ""
    while number:
        number, digit = divmod(number, 58)
        result = BASE58[digit] + result
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + result


# Ed25519 public key derivation (RFC 8032, section 5.1.5), so key generation
# needs no crypto library and no particular openssl build.
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
GY = 4 * pow(5, P - 2, P) % P


def _recover_x(y: int, sign: int) -> int:
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * pow(2, (P - 1) // 4, P) % P
    return P - x if x & 1 != sign else x


G = (_recover_x(GY, 0), GY, 1, _recover_x(GY, 0) * GY % P)


def _add(a: tuple, b: tuple) -> tuple:
    p1 = (a[1] - a[0]) * (b[1] - b[0]) % P
    p2 = (a[1] + a[0]) * (b[1] + b[0]) % P
    p3 = 2 * a[3] * b[3] * D % P
    p4 = 2 * a[2] * b[2] % P
    e, f, g, h = p2 - p1, p4 - p3, p4 + p3, p2 + p1
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _multiply(scalar: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    seed = os.urandom(32) if seed is None else seed
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    x, y, z, _ = _multiply(scalar, G)
    zinv = pow(z, P - 2, P)
    x, y = x * zinv % P, y * zinv % P
    return seed, (y | (x & 1) << 255).to_bytes(32, "little")


def secret(name: str, namespace: str, seed: bytes | None = None) -> dict:
    seed, public = keypair(seed)
    validator = name == SECRET_NAMES[0]
    prefix = "VALIDATOR" if validator else "GROUNDHOG_FAUCET"
    return {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {
            "name": name, "namespace": namespace,
            "labels": {"thruput.network/keys": "true"},
        },
        "stringData": {
            f"{prefix}_SECRET_KEY": (seed + public if validator else seed).hex(),
            f"{prefix}_PUBLIC_KEY": base58(public),
        },
    }


def manifests(namespace: str, seed: bytes | None = None) -> None:
    # JSON documents are valid YAML; no YAML dependency is needed.
    for index, name in enumerate(SECRET_NAMES):
        if index:
            print("---")
        print(json.dumps(secret(name, namespace, seed), indent=2))


def ensure() -> None:
    namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    url = "https://kubernetes.default.svc/api/v1/namespaces/" + urllib.parse.quote(namespace, safe="") + "/secrets"
    for name in SECRET_NAMES:
        request = urllib.request.Request(
            url, data=json.dumps(secret(name, namespace)).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, context=context, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
        if status not in (201, 409):
            raise RuntimeError(f"could not create {name}: Kubernetes HTTP {status}")
        print(f"{name}: {'created' if status == 201 else 'kept (already exists)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("manifests")
    generate.add_argument("--namespace", default="thruput")
    generate.add_argument("--seed-hex", help="32-byte seed for deterministic tests only")
    commands.add_parser("ensure")
    args = parser.parse_args()
    if args.command == "ensure":
        ensure()
    else:
        if args.seed_hex is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", args.seed_hex):
            parser.error("--seed-hex must be 64 hex characters")
        manifests(args.namespace, bytes.fromhex(args.seed_hex) if args.seed_hex else None)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(f"keygen: {error}") from None
