import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from test_components import containers, documents

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "k8s"
spec = importlib.util.spec_from_file_location("keygen", K8S / "base/scripts/keygen.py")
keygen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keygen)
SEED = bytes.fromhex("11" * 32)
PUBLIC = "F25s3DdjXdCxYBhh2z8FBusVEMT4b9bGNFVKJi3wFoF4"


class KeyTests(unittest.TestCase):
    def test_known_answer_and_validator_layout(self):
        seed, public = keygen.keypair(SEED)
        self.assertTrue(keygen.base58(public) == PUBLIC)
        validator = keygen.secret(keygen.SECRET_NAMES[0], "thruput", SEED)
        value = validator["stringData"]["VALIDATOR_SECRET_KEY"]
        self.assertTrue(len(value) == 128 and value == (seed + public).hex())
        faucet = keygen.secret(keygen.SECRET_NAMES[1], "thruput", SEED)
        self.assertTrue(faucet["stringData"]["GROUNDHOG_FAUCET_SECRET_KEY"] == SEED.hex())
        for resource in (validator, faucet):
            self.assertEqual(resource["metadata"]["labels"], {"thruput.network/keys": "true"})

    def test_manifests_cli(self):
        result = subprocess.run(
            ["python3", str(K8S / "base/scripts/keygen.py"), "manifests", "--namespace", "custom", "--seed-hex", SEED.hex()],
            check=True, capture_output=True, text=True,
        )
        resources = [json.loads(doc) for doc in result.stdout.split("---")]
        self.assertEqual([r["metadata"]["name"] for r in resources], list(keygen.SECRET_NAMES))
        self.assertTrue(all(r["metadata"]["namespace"] == "custom" for r in resources))
        self.assertNotIn("__VALIDATOR_", result.stdout)

    def test_fresh_independent_seeds(self):
        seeds = []
        for _ in range(2):
            for name in keygen.SECRET_NAMES:
                resource = keygen.secret(name, "thruput")
                values = resource["stringData"]
                seeds.append(next(v[:64] for k, v in values.items() if k.endswith("SECRET_KEY")))
        self.assertTrue(len(set(seeds)) == 4)


class EnsureTests(unittest.TestCase):
    def run_ensure(self, statuses):
        calls = []

        def request(req, **kwargs):
            calls.append((req, kwargs))
            status = statuses[len(calls) - 1]
            if status != 201:
                raise urllib.error.HTTPError(req.full_url, status, "test", {}, None)
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.status = status
            return response

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "namespace").write_text("thruput\n")
            (base / "token").write_text("test-token\n")
            with patch.object(keygen, "SERVICE_ACCOUNT", base), \
                 patch.object(keygen.ssl, "create_default_context") as tls, \
                 patch.object(keygen, "secret", side_effect=lambda n, ns: {"metadata": {"name": n, "namespace": ns}}), \
                 patch.object(keygen.urllib.request, "urlopen", side_effect=request), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                keygen.ensure()
                tls.assert_called_once_with(cafile=str(base / "ca.crt"))
        return calls, output.getvalue()

    def test_create_and_keep_without_overwrite(self):
        calls, output = self.run_ensure([201, 409])
        self.assertIn("thruput-validator-key: created", output)
        self.assertIn("thruput-faucet-key: kept", output)
        self.assertNotIn("test-token", output)
        self.assertEqual(len(calls), 2)
        for req, kwargs in calls:
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.full_url, "https://kubernetes.default.svc/api/v1/namespaces/thruput/secrets")
            self.assertEqual(req.get_header("Authorization"), "Bearer test-token")
            self.assertIn("context", kwargs)

    def test_other_status_fails(self):
        for status in (200, 401, 403, 500):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                self.run_ensure([status])


class TemplateTests(unittest.TestCase):
    def test_structure_and_placeholders(self):
        text = (K8S / "base/chuckd.template.json").read_text()
        self.assertEqual(text.count("__VALIDATOR_SECRET_KEY__"), 1)
        self.assertEqual(text.count("__VALIDATOR_PUBLIC_KEY__"), 3)
        template = json.loads(text)
        private = template["validator_config"]["private_info"]
        self.assertEqual(private["secret_key"]["ed25519"], "__VALIDATOR_SECRET_KEY__")
        self.assertEqual(private["public_info"]["public_key"]["ed25519"], "__VALIDATOR_PUBLIC_KEY__")
        self.assertEqual(template["validator_config"]["known_validators"][0]["public_key"]["ed25519"], "__VALIDATOR_PUBLIC_KEY__")
        self.assertEqual(template["scp_config"]["quorum_set"]["validators"][0]["ed25519"], "__VALIDATOR_PUBLIC_KEY__")
        old = subprocess.run(["git", "show", "HEAD:k8s/base/chuckd.json"], cwd=ROOT, capture_output=True, text=True)
        if old.returncode == 0:
            original = json.loads(old.stdout)
            secret = original["validator_config"]["private_info"]["secret_key"]["ed25519"]
            restored = json.loads(text.replace("__VALIDATOR_SECRET_KEY__", secret).replace(
                "__VALIDATOR_PUBLIC_KEY__", "BAdiM6BcmD46jZJyCPDFDJbp58YyU1129wYk4pLJdPid"))
            # A boolean assertion avoids leaking the historical secret in failure output.
            self.assertTrue(restored == original, "template differs from original structure")

    def test_coordinator_rejects_bad_keys_without_echoing_them(self):
        import os
        for secret, public in (("", ""), ("invalid", PUBLIC), ("11" * 64, "invalid"), ("11" * 64, PUBLIC)):
            env = dict(os.environ, VALIDATOR_SECRET_KEY=secret, VALIDATOR_PUBLIC_KEY=public)
            result = subprocess.run(["bash", str(K8S / "base/scripts/coordinator.sh")], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("VALIDATOR_", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_coordinator_render_is_private_and_complete(self):
        import os
        import stat
        seed, public = keygen.keypair(SEED)
        script = (K8S / "base/scripts/coordinator.sh").read_text()
        renderer = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            # Exercise the exact embedded renderer with test-local paths.
            renderer = renderer.replace("/opt/thruput/scripts/chuckd.template.json", str(K8S / "base/chuckd.template.json"))
            renderer = renderer.replace('dir="/tmp"', f'dir={directory!r}')
            env = dict(os.environ, VALIDATOR_SECRET_KEY=(seed + public).hex(), VALIDATOR_PUBLIC_KEY=PUBLIC)
            result = subprocess.run(["python3", "-c", renderer], env=env, capture_output=True, text=True, check=True)
            config = Path(result.stdout.strip())
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            text = config.read_text()
            self.assertNotIn("__VALIDATOR_", text)
            parsed = json.loads(text)
            self.assertTrue(parsed["validator_config"]["private_info"]["secret_key"]["ed25519"] == (seed + public).hex())

    def test_zero_dev_accounts_still_checks_faucet(self):
        script = (K8S / "base/scripts/smoke.sh").read_text()
        checks = script.split("accounts=$(fetch", 1)[1].split("# Faucet:", 1)[0]
        harness = """set -euo pipefail
DEV_ACCOUNTS=0
BOOTSTRAP_URL=http://bootstrap
fetch() { echo '{"faucet":{"role":"faucet","walletAddress":"faucet-wallet"}}'; }
cli() { echo '{"data":{"rawBalance":"1"}}'; }
json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["rawBalance"])'; }
accounts=$(fetch""" + checks
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True)
        self.assertIn("minted: faucet faucet-wallet holds 1", result.stdout)
        self.assertIn("cli airdrop", script.split("# Faucet:", 1)[1])

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl not installed")
    def test_generate_component_and_secret_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(K8S, root / "k8s")
            overlay = root / "k8s/overlays/single-pod/kustomization.yaml"
            overlay.write_text(overlay.read_text().replace(
                "  - ../../components/release", "  - ../../components/keys-generate\n  - ../../components/release"))
            result = subprocess.run(["kubectl", "kustomize", str(overlay.parent)], capture_output=True, text=True, check=True)
        rendered = result.stdout
        self.assertFalse(documents(rendered, "Secret"))
        job = next(doc for doc in documents(rendered, "Job") if "  name: thruput-keygen\n" in doc)
        for required in ("backoffLimit: 3", "ttlSecondsAfterFinished: 600",
                         "runAsNonRoot: true", "type: RuntimeDefault", "runAsUser: 999",
                         "serviceAccountName: thruput-keygen", "name: thruput-registry",
                         "allowPrivilegeEscalation: false", "- ALL", "- python3",
                         "- /opt/thruput/scripts/keygen.py", "- ensure"):
            self.assertIn(required, job)
        self.assertNotRegex(job, r"image: thruput-validator\s")
        import re
        config = next(doc for doc in documents(rendered, "ConfigMap") if "  keygen.py:" in doc)
        config_name = re.search(r"(?m)^  name: (thruput-scripts-\S+)$", config)[1]
        self.assertIn("name: " + config_name, job)
        for kind in ("ServiceAccount", "Role", "RoleBinding", "Job"):
            resource = next(doc for doc in documents(rendered, kind) if "  name: thruput-keygen\n" in doc)
            self.assertIn("app.kubernetes.io/part-of: thruput", resource)
        stateful = documents(rendered, "StatefulSet")[0]
        self.assertNotIn("coordinator-config", stateful)
        for name, block in containers(stateful):
            if name == "hog-coordinator":
                self.assertIn("name: thruput-validator-key", block)
                self.assertNotIn("thruput-faucet-key", block)
            elif name == "hog-faucet":
                self.assertIn("name: thruput-faucet-key", block)
                self.assertNotIn("thruput-validator-key", block)
            elif name == "genesis":
                self.assertIn("key: GROUNDHOG_FAUCET_PUBLIC_KEY", block)
                self.assertIn("name: thruput-faucet-key", block)
                self.assertNotIn("secretRef:", block)
                self.assertNotIn("SECRET_KEY", block)
            else:
                self.assertNotIn("secretRef:", block)
                self.assertNotIn("secretKeyRef:", block)


if __name__ == "__main__":
    unittest.main()
