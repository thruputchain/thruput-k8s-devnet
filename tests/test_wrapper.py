"""Render the wrapper without contacting a cluster."""
import itertools
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("kubectl"), "kubectl not installed")
class WrapperTests(unittest.TestCase):
    def test_render_options(self):
        source = (ROOT / "scripts/k3s-devnet.sh").read_text().split('\ncommand=${1:-}')[0]
        source = re.sub(r'^repo_root=.*$', 'repo_root=' + shlex.quote(str(ROOT)), source, flags=re.M)
        # Temporary generated overlays stay inside the repo so Kustomize can
        # follow relative paths to the real overlays and components.
        (ROOT / ".devnet").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".devnet") as directory:
            for keys, ingress, monitoring, storage, digest in itertools.product((False, True), repeat=5):
                image = "mirror.example/validator:" + "test"
                if digest:
                    image = "mirror.example/validator@sha256:" + "a" * 64
                variables = {
                    "work_dir": directory, "keys_generate": "1" if keys else "",
                    "ingress_hosts": "rpc.devnet.test,faucet.devnet.test" if ingress else "",
                    "monitoring": "1" if monitoring else "",
                    "storage_class": "fast" if storage else "",
                    "volume_size": "100Gi" if storage else "",
                    "ingress_class": "custom", "cluster_issuer": "selfsigned",
                }
                script = source + "\n" + "\n".join(k + "=" + shlex.quote(v) for k, v in variables.items())
                script += '\nrender_overlay "$k8s_root/overlays/production" ' + shlex.quote(image) + ' "https://rpc.devnet.test" >/dev/null\n'
                script += 'kubectl kustomize "$work_dir/overlay"\n'
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered = result.stdout
                self.assertEqual(set(re.findall(r"(?m)^\s+image: (.+)$", rendered)), {image})
                self.assertEqual("name: thruput-keygen\n" in rendered, keys)
                self.assertEqual("kind: Ingress\n" in rendered, ingress)
                self.assertEqual("kind: ServiceMonitor\n" in rendered, monitoring)
                for doc in rendered.split("---\n"):
                    if re.search(r"(?m)^kind: (Job|Ingress|ServiceMonitor)$", doc):
                        self.assertIn("  namespace: thruput\n", doc)
                    if "kind: StatefulSet\n" in doc and storage:
                        self.assertIn("storageClassName: fast", doc)
                        self.assertIn("storage: 100Gi", doc)
                if ingress:
                    self.assertIn("host: rpc.devnet.test", rendered)
                    self.assertIn("host: faucet.devnet.test", rendered)
                    self.assertIn("ingressClassName: custom", rendered)
                    self.assertIn("cert-manager.io/cluster-issuer: selfsigned", rendered)


if __name__ == "__main__":
    unittest.main()
