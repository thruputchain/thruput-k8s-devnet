#!/usr/bin/env python3
"""Render the faucet config from the committed chain, then exec hog-faucet.

The faucet wallet was funded at genesis by genesis.sh. This reads that wallet
and the committed chain identity from the coordinator's bootstrap endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CONFIG_PATH = "/tmp/faucet.toml"


def identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or any(
        character not in BASE58 for character in value
    ):
        raise ValueError(f"{field} must be a Base58 32-byte identifier")
    return value


def fetch(url: str) -> dict:
    for attempt in range(600):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.load(response)
        except Exception as error:  # Coordinator still starting.
            if attempt % 30 == 0:
                print(f"waiting for {url}: {error}", file=sys.stderr)
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {url}")


def render(env: dict[str, str], metadata: dict, manifest: dict) -> str:
    if metadata.get("schemaVersion") != 3 or manifest.get("schemaVersion") != 4:
        raise ValueError("faucet needs hosted metadata 3 and runtime manifest 4")
    public_key = identifier(env["GROUNDHOG_FAUCET_PUBLIC_KEY"], "faucet public key")
    if metadata["faucet"]["publicKey"] != public_key:
        raise ValueError("genesis funded a different faucet key than the configured secret")
    if metadata["protocol"] != manifest["protocol"]:
        raise ValueError("runtime manifest protocol does not match genesis metadata")
    if metadata["networkHash"] != manifest["networkHash"]:
        raise ValueError("runtime manifest networkHash does not match genesis metadata")
    commit = manifest["hostedBootstrap"]["commit"]
    if commit["height"] != 1:
        raise ValueError("runtime manifest has no committed genesis")
    amount = env["FAUCET_AIRDROP_RAW_AMOUNT"]
    if not amount.isascii() or not amount.isdigit():
        raise ValueError("FAUCET_AIRDROP_RAW_AMOUNT must be a non-negative integer")
    q = json.dumps
    return f"""listen_address = "0.0.0.0:18085"

[network]
network_id = {q(env["NETWORK_ID"])}
protocol = {q(manifest["protocol"])}
genesis_hash = {q(identifier(commit["valueHash"], "genesis hash"))}
network_hash = {q(identifier(manifest["networkHash"], "network hash"))}

[network.endpoints]
rpc_url = {q(env["PUBLIC_RPC_URL"])}

[network.native_token]
token_address = "11111111111111111111111111111112"
decimals = 7
transfer_entrypoint = "0x63285b3523f9dc3f"

[network.faucet]
wallet_address = {q(identifier(metadata["faucet"]["walletAddress"], "faucet wallet"))}
public_key = {q(public_key)}

[airdrop]
raw_amount = {q(amount)}

[airdrop.secret_key_source]
type = "environment"
variable = "GROUNDHOG_FAUCET_SECRET_KEY"

[policy.airdrop]
"""


def main() -> None:
    base = os.environ["BOOTSTRAP_URL"]
    metadata = fetch(f"{base}/metadata.json")
    manifest = fetch(f"{base}/runtime-manifest.json")
    with open(CONFIG_PATH, "w", encoding="utf-8") as output:
        output.write(render(dict(os.environ), metadata, manifest))
    print(f"faucet wallet {metadata['faucet']['walletAddress']} on {os.environ['NETWORK_ID']}")
    os.execv("/usr/bin/hog-faucet", ["hog-faucet", "--config", CONFIG_PATH])


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, TimeoutError, ValueError) as error:
        raise SystemExit(f"error: {error}") from None
