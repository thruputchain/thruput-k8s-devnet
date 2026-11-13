#!/usr/bin/env python3
"""Generate deployment identities, or create missing Secrets in-cluster."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
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


def keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    seed = os.urandom(32) if seed is None else seed
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    result = subprocess.run(
        ["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
        input=bytes.fromhex("302e020100300506032b657004220420") + seed,
        capture_output=True,
    )
    if result.returncode or len(result.stdout) != 44 or not result.stdout.startswith(
        bytes.fromhex("302a300506032b6570032100")
    ):
        raise RuntimeError("openssl pkey could not derive an Ed25519 public key")
    return seed, result.stdout[-32:]


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
