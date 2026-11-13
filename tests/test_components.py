from collections import Counter
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "k8s"
DAEMONS = {
    "hog-db", "hog-auth", "hog-coordinator", "hog-mempool", "hog-event",
    "hog-exec", "hog-indexer", "hog-gateway", "hog-faucet",
}


def render(path):
    result = subprocess.run(
        ["kubectl", "kustomize", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


def documents(text, kind):
    return [doc for doc in text.split("---\n")
            if re.search(rf"(?m)^kind: {kind}$", doc)]


def containers(document):
    # kubectl emits lists at the same indentation as their containing key.
    for section in re.finditer(
        r"(?m)^( +)(?:initContainers|containers):\n", document
    ):
        indent = len(section[1])
        lines = []
        for line in document[section.end():].splitlines():
            depth = len(line) - len(line.lstrip())
            if line.strip() and (depth < indent or
                                (depth == indent and not line.lstrip().startswith("- "))):
                break
            lines.append(line)
        blocks = re.split(rf"(?m)^ {{{indent}}}- ", "\n".join(lines))[1:]
        for block in blocks:
            name = re.search(rf"(?m)^(?: {{{indent + 2}}}|\A)name: ([\w-]+)$", block)
            if name:
                yield name[1], block


class ComponentTests(unittest.TestCase):
    def test_component_kinds(self):
        for component in ("ingress", "network-policy", "monitoring", "resources"):
            text = (K8S / "components" / component / "kustomization.yaml").read_text()
            self.assertIn("apiVersion: kustomize.config.k8s.io/v1alpha1\n", text)
            self.assertRegex(text, r"(?m)^kind: Component$")

    def test_production_placement_and_release(self):
        overlay = K8S / "overlays/production"
        hosted = Counter()
        for path in sorted((overlay / "pods").glob("*/kustomization.yaml")):
            hosted.update(re.findall(r'thruput.network/hosts-(hog-[a-z]+): "true"', path.read_text()))
        self.assertEqual(hosted, Counter({name: 1 for name in DAEMONS}))
        components = re.findall(r"- ../../components/([\w-]+)", (overlay / "kustomization.yaml").read_text())
        self.assertEqual(components[-1], "release")

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl not installed")
    def test_production_with_optional_components(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            shutil.copytree(K8S, temporary / "k8s")
            wrapper = temporary / "wrapper"
            wrapper.mkdir()
            kustomization = (
                "apiVersion: kustomize.config.k8s.io/v1beta1\n"
                "kind: Kustomization\nresources:\n"
                "  - ../k8s/overlays/production\ncomponents:\n"
                "  - ../k8s/components/ingress\n"
                "  - ../k8s/components/monitoring\n"
            )
            (wrapper / "kustomization.yaml").write_text(kustomization)
            rendered = render(wrapper)
            self.check_render(rendered)
            # The wrapper overrides hosts, class, and issuer with JSON patches.
            override = temporary / "override"
            override.mkdir()
            patch = (
                "  - target:\n      kind: Ingress\n      name: {name}\n"
                "    patch: |-\n"
                "      - {{op: replace, path: /spec/rules/0/host, value: {host}}}\n"
                "      - {{op: replace, path: /spec/tls/0/hosts/0, value: {host}}}\n"
                "      - {{op: replace, path: /spec/ingressClassName, value: custom}}\n"
                "      - {{op: replace, path: /metadata/annotations/cert-manager.io~1cluster-issuer, value: custom-issuer}}\n"
            )
            (override / "kustomization.yaml").write_text(
                "apiVersion: kustomize.config.k8s.io/v1beta1\n"
                "kind: Kustomization\nresources:\n  - ../wrapper\npatches:\n"
                + patch.format(name="hog-gateway", host="rpc.custom.example")
                + patch.format(name="hog-faucet", host="faucet.custom.example"))
            overridden = render(override)
            for doc in documents(overridden, "Ingress"):
                self.assertIn("ingressClassName: custom", doc)
                self.assertIn("cert-manager.io/cluster-issuer: custom-issuer", doc)
                host = "rpc.custom.example" if "name: hog-gateway\n" in doc else "faucet.custom.example"
                self.assertIn("host: " + host, doc)
                self.assertIn("- " + host, doc)

    def check_render(self, rendered):
        ingresses = documents(rendered, "Ingress")
        self.assertEqual(len(ingresses), 2)
        for doc in ingresses:
            host = "rpc.devnet.example" if "name: hog-gateway\n" in doc else "faucet.devnet.example"
            self.assertIn("host: " + host, doc)
            self.assertIn("- " + host, doc)
            self.assertIn("ingressClassName: traefik", doc)
            self.assertIn("cert-manager.io/cluster-issuer: letsencrypt", doc)
        services = documents(rendered, "Service")
        for name in ("hog-gateway", "hog-faucet"):
            service = next(doc for doc in services if f"  name: {name}\n" in doc)
            self.assertIn("type: ClusterIP", service)
        deny = [doc for doc in documents(rendered, "NetworkPolicy")
                if "  name: default-deny-ingress\n" in doc]
        self.assertEqual(len(deny), 1)
        self.assertNotRegex(rendered, r"image: thruput-validator(?:\s|$)")
        for doc in documents(rendered, "StatefulSet") + documents(rendered, "Job"):
            for name, block in containers(doc):
                self.assertRegex(block, r"(?m)^\s*(?:- )?image: \S+", name)
        hosted = Counter()
        expected = {
            "core": DAEMONS - {"hog-gateway", "hog-faucet", "hog-indexer"} | {"hog-bootstrap"},
            "indexer": {"hog-indexer"},
            "edge": {"hog-gateway", "hog-faucet"},
        }
        for doc in documents(rendered, "StatefulSet"):
            pod = re.search(r"(?m)^  name: thruput-(core|indexer|edge)$", doc)[1]
            actual = set()
            self.assertNotIn("nodeSelector:", doc)
            self.assertIn("preferredDuringSchedulingIgnoredDuringExecution:", doc)
            for name, block in containers(doc):
                self.assertRegex(block, r"(?m)^\s*(?:- )?image: \S+")
                if name.startswith("hog-"):
                    actual.add(name)
                    hosted[name] += 1
                    self.assertRegex(block, r"limits:\n\s+memory: \S+")
            self.assertEqual(actual, expected[pod])
        self.assertEqual(hosted, Counter({name: 1 for name in DAEMONS | {"hog-bootstrap"}}))
        self.assertEqual(len(documents(rendered, "ServiceMonitor")), 5)


if __name__ == "__main__":
    unittest.main()
