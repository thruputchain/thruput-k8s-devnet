from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "k8s"
DAEMONS = (
    "hog-mempool",
    "hog-db",
    "hog-event",
    "hog-exec",
    "hog-auth",
    "hog-indexer",
    "hog-coordinator",
    "hog-gateway",
    "hog-faucet",
)

FAUCET_SPEC = importlib.util.spec_from_file_location("k8s_faucet", K8S / "base/scripts/faucet.py")
assert FAUCET_SPEC and FAUCET_SPEC.loader
faucet = importlib.util.module_from_spec(FAUCET_SPEC)
FAUCET_SPEC.loader.exec_module(faucet)

SET_IMAGE_SPEC = importlib.util.spec_from_file_location("set_image", ROOT / "scripts/set-image.py")
assert SET_IMAGE_SPEC and SET_IMAGE_SPEC.loader
set_image = importlib.util.module_from_spec(SET_IMAGE_SPEC)
SET_IMAGE_SPEC.loader.exec_module(set_image)


def hosted_daemons(pod_kustomization: Path) -> list[str]:
    return re.findall(r'thruput\.network/hosts-(hog-[a-z]+): "true"', pod_kustomization.read_text(encoding="utf-8"))


class ManifestContractTests(unittest.TestCase):
    def overlays(self) -> list[Path]:
        overlays = sorted(path for path in (K8S / "overlays").iterdir() if path.is_dir())
        self.assertEqual([path.name for path in overlays], ["multi-node", "single-pod", "split"])
        return overlays

    def test_release_pins_one_published_tag(self) -> None:
        component = (K8S / "components/release/kustomization.yaml").read_text(encoding="utf-8")
        self.assertIn("newName: ghcr.io/thruputchain/thruput-validator\n", component)
        self.assertRegex(component, r"(?m)^    newTag: \d+\.\d+\.\d+$")

    def test_set_image_rewrites_the_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kustomization.yaml"
            path.write_text("images:\n  - name: x\n    newTag: 0.1.1\n", encoding="utf-8")
            set_image.set_tag("v0.2.0", path)
            self.assertEqual(path.read_text(encoding="utf-8"), "images:\n  - name: x\n    newTag: 0.2.0\n")
            with self.assertRaises(ValueError):
                set_image.set_tag("latest", path)
            path.write_text("images: []\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                set_image.set_tag("0.2.0", path)

    def test_each_daemon_component_targets_its_own_host_label(self) -> None:
        for daemon in DAEMONS:
            component = K8S / "components" / daemon
            self.assertIn(
                f"labelSelector: thruput.network/hosts-{daemon}=true",
                (component / "kustomization.yaml").read_text(encoding="utf-8"),
            )
            patch = (component / "patch.yaml").read_text(encoding="utf-8")
            self.assertIn(f'thruput.network/{daemon}: "true"', patch)
            self.assertIn(f"- name: {daemon}\n", patch)

    def test_every_overlay_hosts_each_daemon_exactly_once(self) -> None:
        for overlay in self.overlays():
            hosted = [
                daemon
                for pod in sorted((overlay / "pods").iterdir())
                for daemon in hosted_daemons(pod / "kustomization.yaml")
            ]
            self.assertEqual(sorted(hosted), sorted(DAEMONS), overlay.name)
            text = (overlay / "kustomization.yaml").read_text(encoding="utf-8")
            components = re.findall(r"- \.\./\.\./components/([a-z-]+)", text)
            self.assertEqual(sorted(components), sorted((*DAEMONS, "release")), overlay.name)
            self.assertEqual(components[-1], "release", f"{overlay.name}: release must run last to set every image")

    def test_services_select_the_daemon_label(self) -> None:
        services = (K8S / "base/services.yaml").read_text(encoding="utf-8")
        for daemon in DAEMONS:
            self.assertIn(f"  name: {daemon}\n", services)
            self.assertIn(f'    thruput.network/{daemon}: "true"\n', services)
        self.assertEqual(services.count("publishNotReadyAddresses: true"), len(DAEMONS))

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl not installed")
    def test_overlays_render_one_container_per_daemon(self) -> None:
        for overlay in self.overlays():
            rendered = subprocess.run(
                ["kubectl", "kustomize", str(overlay)], check=True, capture_output=True, text=True
            ).stdout
            for daemon in DAEMONS:
                containers = re.findall(rf"(?m)^ {{8,}}(?:- )?name: {daemon}$", rendered)
                self.assertEqual(len(containers), 1, f"{overlay.name}: {daemon}")
            self.assertNotRegex(rendered, r"image: thruput-validator\s", overlay.name)
            init = re.findall(r"(?m)^ {8}(?:- )?name: (genesis|prepare-data)$", rendered)
            if "genesis" in init:
                position = init.index("genesis")
                self.assertGreater(position, 0, f"{overlay.name}: genesis must follow prepare-data")
                self.assertEqual(init[position - 1], "prepare-data", overlay.name)


class FaucetRenderTests(unittest.TestCase):
    PUBLIC_KEY = "F25s3DdjXdCxYBhh2z8FBusVEMT4b9bGNFVKJi3wFoF4"
    GENESIS = "88TGrHLrssWVxLuQFBymZrtdhcBFSRkeoWoYny2p1x62"
    NETWORK = "11111111111111111111111111111111"

    def inputs(self) -> tuple[dict[str, str], dict, dict]:
        env = {
            "GROUNDHOG_FAUCET_PUBLIC_KEY": self.PUBLIC_KEY,
            "FAUCET_AIRDROP_RAW_AMOUNT": "100000000",
            "NETWORK_ID": "devnet",
            "PUBLIC_RPC_URL": "http://hog-gateway:18545",
        }
        metadata = {
            "schemaVersion": 3,
            "protocol": "hogp/a67b703a554a9ca",
            "networkHash": self.NETWORK,
            "faucet": {"publicKey": self.PUBLIC_KEY, "walletAddress": self.GENESIS},
        }
        manifest = {
            "schemaVersion": 4,
            "protocol": "hogp/a67b703a554a9ca",
            "networkHash": self.NETWORK,
            "hostedBootstrap": {"commit": {"height": 1, "valueHash": self.GENESIS}},
        }
        return env, metadata, manifest

    def test_renders_committed_identity(self) -> None:
        config = faucet.render(*self.inputs())
        self.assertIn(f'genesis_hash = "{self.GENESIS}"', config)
        self.assertIn('rpc_url = "http://hog-gateway:18545"', config)
        self.assertIn('variable = "GROUNDHOG_FAUCET_SECRET_KEY"', config)
        self.assertIn("[network.native_token]", config)

    def test_rejects_a_genesis_funding_another_key(self) -> None:
        env, metadata, manifest = self.inputs()
        metadata["faucet"]["publicKey"] = self.GENESIS
        with self.assertRaisesRegex(ValueError, "different faucet key"):
            faucet.render(env, metadata, manifest)

    def test_rejects_an_uncommitted_manifest(self) -> None:
        env, metadata, manifest = self.inputs()
        manifest["hostedBootstrap"]["commit"]["height"] = 0
        with self.assertRaisesRegex(ValueError, "no committed genesis"):
            faucet.render(env, metadata, manifest)


if __name__ == "__main__":
    unittest.main()
