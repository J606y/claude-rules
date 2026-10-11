"""Offline acceptance tests; network access is replaced only at its boundary."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update.py"


def load_updater():
    if not SCRIPT.exists():
        raise AssertionError("Missing production updater scripts/update.py")
    spec = importlib.util.spec_from_file_location("claude_update", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceParsingTests(unittest.TestCase):
    def test_two_source_grammars_normalize_to_same_rule_set(self):
        updater = load_updater()
        v2fly = b"# Claude\nclaude.ai\nfull:api.anthropic.com\nclaude.ai\n"
        # MetaCubeX classical syntax is explicit, not Clash domain syntax.
        meta = b"payload:\n  - 'DOMAIN,api.anthropic.com'\n  - DOMAIN-SUFFIX,claude.ai\n"
        expected = {("DOMAIN-SUFFIX", "claude.ai"), ("DOMAIN", "api.anthropic.com")}
        self.assertEqual(updater.parse_source("v2fly", v2fly), expected)
        self.assertEqual(updater.parse_source("metacubex", meta), expected)

    def test_invalid_domains_are_rejected(self):
        updater = load_updater()
        invalid = [
            "127.0.0.1", "1.2.3.4", "::1", "claude.ai:443", "claude.ai/api",
            "*.claude.ai", "claude.ai,DIRECT", "clau de.ai", "-claude.ai",
            "claude-.ai", "claude..ai", "localhost", "例子.com", "claude.ai.",
            "a" * 64 + ".com", ".".join(["a" * 63] * 4), "_api.claude.ai",
            "https://claude.ai", "claude.ai\t@ads", "123.456", "claude.ai#comment",
        ]
        for domain in invalid:
            with self.subTest(domain=domain):
                with self.assertRaises(ValueError):
                    updater.parse_source("v2fly", (domain + "\n").encode())

    def test_public_roots_and_tracking_families_are_rejected(self):
        updater = load_updater()
        for domain in ["google.com", "cloudflare.com", "b-cdn.net", "com", "co.uk",
                       "github.io", "amazonaws.com", "pages.dev", "cdn.usefathom.com",
                       "usefathom.com", "www.googletagmanager.com", "com.cn"]:
            with self.subTest(domain=domain):
                with self.assertRaises(ValueError):
                    updater.parse_source("v2fly", (domain + "\n").encode())

    def test_exact_tenant_on_shared_cdn_is_allowed(self):
        updater = load_updater()
        self.assertEqual(
            updater.parse_source("v2fly", b"full:servd-anthropic-website.b-cdn.net\n"),
            {("DOMAIN", "servd-anthropic-website.b-cdn.net")},
        )

    def test_empty_sources_are_rejected(self):
        updater = load_updater()
        for source, body in [("v2fly", b""), ("v2fly", b"# comments only\n"),
                             ("metacubex", b"payload:\n# no rules\n")]:
            with self.subTest(source=source, body=body):
                with self.assertRaises(ValueError):
                    updater.parse_source(source, body)

    def test_html_and_non_utf8_are_rejected(self):
        updater = load_updater()
        for source in ("v2fly", "metacubex"):
            for body in [b"<!doctype html><html>error</html>", b"# <html>\nclaude.ai\n", b"\xff"]:
                with self.subTest(source=source, body=body):
                    with self.assertRaises(ValueError):
                        updater.parse_source(source, body)

    def test_unknown_grammars_and_policy_injection_are_rejected(self):
        updater = load_updater()
        invalid = [
            ("unknown", b"claude.ai\n"), ("v2fly", b"domain:claude.ai\n"),
            ("v2fly", b"regexp:.*claude.*\n"), ("v2fly", b"include:anthropic\n"),
            ("v2fly", b"payload:\nclaude.ai\n"), ("v2fly", b"claude.ai @ads\n"),
            ("metacubex", b"DOMAIN-SUFFIX,claude.ai\n"),
            ("metacubex", b"payload:\nDOMAIN-SUFFIX,claude.ai\n"),
            ("metacubex", b"payload:\npayload:\n  - DOMAIN,claude.ai\n"),
            ("metacubex", b"payload:\n  - '+.claude.ai'\n"),
            ("metacubex", b"payload:\n  - DOMAIN-KEYWORD,claude\n"),
            ("metacubex", b"payload:\n  - DOMAIN-SUFFIX,claude.ai,DIRECT\n"),
            ("metacubex", b"payload:\n  - 'DOMAIN,claude.ai\"\n"),
            ("metacubex", b"payload:\n  - DOMAIN,claude.ai # inline\n"),
        ]
        for source, body in invalid:
            with self.subTest(source=source, body=body):
                with self.assertRaises(ValueError):
                    updater.parse_source(source, body)

    def test_source_bytes_and_rule_limits_are_enforced(self):
        updater = load_updater()
        for body in [b"#" + b"a" * (256 * 1024) + b"\nclaude.ai\n",
                     b"".join(f"n{i}.claude.ai\n".encode() for i in range(65))]:
            with self.subTest(size=len(body)):
                with self.assertRaises(ValueError):
                    updater.parse_source("v2fly", body)

    def test_case_normalization_and_maximum_count(self):
        updater = load_updater()
        self.assertEqual(updater.parse_source("v2fly", b"CLAUDE.AI\nclaude.ai\n"),
                         {("DOMAIN-SUFFIX", "claude.ai")})
        body = b"".join(f"n{i}.claude.ai\n".encode() for i in range(64))
        self.assertEqual(len(updater.parse_source("v2fly", body)), 64)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.updater = load_updater()
        self.temp = tempfile.TemporaryDirectory(prefix="claude-rules-", dir=os.environ.get("TMPDIR", "."))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        seed = SCRIPT.parents[1] / "data" / "seed.list"
        self.assertTrue(seed.is_file(), "Missing reviewed seed.list")
        (self.root / "data" / "seed.list").write_bytes(seed.read_bytes())
        self.bodies = {
            "v2fly": b"claude.ai\nfull:api.anthropic.com\n",
            "metacubex": b"payload:\n  - DOMAIN,api.anthropic.com\n  - DOMAIN-SUFFIX,claude.ai\n",
        }

    def fetch(self, url):
        for source, source_url in self.updater.SOURCES:
            if source_url == url:
                return self.bodies[source]
        self.fail(f"Unexpected source URL: {url}")

    def test_generates_policy_free_sorted_rules_and_source_evidence(self):
        summary = self.updater.run_update(self.root, fetcher=self.fetch)
        result = (self.root / "Loon" / "Claude.list").read_bytes()
        lines = result.decode().splitlines()
        expected_seeds = {
            "DOMAIN-SUFFIX,anthropic.com", "DOMAIN-SUFFIX,clau.de", "DOMAIN-SUFFIX,claude.ai",
            "DOMAIN-SUFFIX,claude.com", "DOMAIN-SUFFIX,claude.dev",
            "DOMAIN-SUFFIX,claudemcpclient.com", "DOMAIN-SUFFIX,claudemcpcontent.com",
            "DOMAIN-SUFFIX,claudeusercontent.com", "DOMAIN,servd-anthropic-website.b-cdn.net",
        }
        all_expected = expected_seeds | {"DOMAIN,api.anthropic.com"}
        self.assertEqual(set(lines), all_expected)
        self.assertEqual(lines, sorted(all_expected))
        self.assertEqual(summary, {"added": 10, "retained": 0, "count": 10, "changed": True})

        clash_result = (self.root / "Clash" / "Claude.yaml").read_bytes()
        clash_lines = clash_result.decode().splitlines()
        self.assertEqual(clash_lines[0], "payload:")
        self.assertEqual([line[4:] for line in clash_lines[1:]], lines)

        metadata = json.loads((self.root / "metadata.json").read_bytes())
        self.assertEqual(metadata["rule_count"], 10)
        self.assertEqual(metadata["rules_sha256"], hashlib.sha256(result).hexdigest())
        self.assertEqual(metadata["seed_rule_count"], 9)
        self.assertEqual(len(metadata["sources"]), 2)
        for evidence, (name, url) in zip(metadata["sources"], self.updater.SOURCES):
            self.assertEqual(evidence, {"name": name, "url": url, "rule_count": 2,
                                       "sha256": hashlib.sha256(self.bodies[name]).hexdigest()})
        self.assertNotIn("timestamp", json.dumps(metadata))

    def test_disagreement_rejects_update_without_touching_old_bytes(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {path: path.read_bytes() for path in paths}
        for body in [b"payload:\n  - DOMAIN-SUFFIX,claude.ai\n",
                     b"payload:\n  - DOMAIN,api.anthropic.com\n  - DOMAIN,claude.ai\n"]:
            with self.subTest(body=body):
                self.bodies["metacubex"] = body
                with self.assertRaises(ValueError):
                    self.updater.run_update(self.root, fetcher=self.fetch)
                self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_retains_rules_removed_from_both_sources(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        self.bodies = {"v2fly": b"claude.ai\n", "metacubex": b"payload:\n  - DOMAIN-SUFFIX,claude.ai\n"}
        summary = self.updater.run_update(self.root, fetcher=self.fetch)
        self.assertIn(b"DOMAIN,api.anthropic.com\n", (self.root / "Loon" / "Claude.list").read_bytes())
        self.assertIn(b"  - DOMAIN,api.anthropic.com\n", (self.root / "Clash" / "Claude.yaml").read_bytes())
        self.assertEqual(summary, {"added": 0, "retained": 10, "count": 10, "changed": True})

    def test_identical_update_is_idempotent_without_rewriting_files(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        summary = self.updater.run_update(self.root, fetcher=self.fetch)
        self.assertEqual(summary, {"added": 0, "retained": 10, "count": 10, "changed": False})
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}, before)

    def test_core_suffixes_cover_api_bridge_and_platform(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        rules = self.updater.parse_rule_list((self.root / "Loon" / "Claude.list").read_bytes())
        hosts = ["api.anthropic.com", "bridge.claudeusercontent.com", "platform.claude.com",
                 "platform.anthropic.com", "console.anthropic.com", "claude.ai",
                 "a.claudemcpclient.com", "b.claudemcpcontent.com", "api.claude.dev"]
        for host in hosts:
            with self.subTest(host=host):
                self.assertTrue(any(host == domain or (kind == "DOMAIN-SUFFIX" and host.endswith("." + domain))
                                    for kind, domain in rules))
        for host in ["notclaude.ai", "claude.ai.attacker.com", "other.b-cdn.net"]:
            with self.subTest(unrelated_host=host):
                self.assertFalse(any(host == domain or (kind == "DOMAIN-SUFFIX" and host.endswith("." + domain))
                                     for kind, domain in rules))

    def test_invalid_seed_preserves_both_old_outputs(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        for body in [b"", b"# empty\n", b"DOMAIN,claude.ai,DIRECT\n",
                     b"DOMAIN-SUFFIX,google.com\n", b"DOMAIN-KEYWORD,claude\n",
                     b"#" + b"x" * (256 * 1024),
                     b"".join(f"DOMAIN,n{i}.claude.ai\n".encode() for i in range(65))]:
            with self.subTest(body_length=len(body)):
                (self.root / "data" / "seed.list").write_bytes(body)
                with self.assertRaises(ValueError):
                    self.updater.run_update(self.root, fetcher=self.fetch)
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_invalid_existing_output_is_not_silently_discarded(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        output = self.root / "Loon" / "Claude.list"
        clash_output = self.root / "Clash" / "Claude.yaml"
        metadata = self.root / "metadata.json"
        before_clash = clash_output.read_bytes()
        before_metadata = metadata.read_bytes()
        for body in [b"", b"# empty\n", b"DOMAIN,claude.ai,DIRECT\n", b"<html>error</html>"]:
            with self.subTest(body=body):
                output.write_bytes(body)
                with self.assertRaises(ValueError):
                    self.updater.run_update(self.root, fetcher=self.fetch)
                self.assertEqual(output.read_bytes(), body)
                self.assertEqual(clash_output.read_bytes(), before_clash)
                self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_merged_rule_limit_preserves_old_outputs(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        self.bodies = {
            "v2fly": b"".join(f"n{i}.claude.ai\n".encode() for i in range(64)),
            "metacubex": b"payload:\n" + b"".join(f"  - DOMAIN-SUFFIX,n{i}.claude.ai\n".encode() for i in range(64)),
        }
        with self.assertRaises(ValueError):
            self.updater.run_update(self.root, fetcher=self.fetch)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_failure_from_either_source_preserves_every_output_byte(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        original = self.bodies.copy()
        for source in ("v2fly", "metacubex"):
            for body in [b"", b"<html>failure</html>", b"unknown format", b"x" * (256 * 1024 + 1)]:
                with self.subTest(source=source, body_length=len(body)):
                    self.bodies = original | {source: body}
                    with self.assertRaises(ValueError):
                        self.updater.run_update(self.root, fetcher=self.fetch)
                    self.assertEqual({p: p.read_bytes() for p in paths}, before)
            self.bodies = original.copy()
            def failed_fetch(url, failed_source=source):
                if dict(self.updater.SOURCES)[failed_source] == url:
                    raise OSError("synthetic transport failure")
                return self.fetch(url)
            with self.subTest(source=source, transport_failure=True):
                with self.assertRaises(OSError):
                    self.updater.run_update(self.root, fetcher=failed_fetch)
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_replacement_uses_complete_staged_files(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        self.bodies["v2fly"] += b"full:new.anthropic.com\n"
        self.bodies["metacubex"] += b"  - DOMAIN,new.anthropic.com\n"
        original_replace = os.replace
        calls = []
        def observed_replace(source, target):
            source, target = Path(source), Path(target)
            self.assertEqual(source.parent, target.parent)
            self.assertEqual(target.read_bytes(), before[target])
            self.assertTrue(source.read_bytes().endswith(b"\n"))
            calls.append(target)
            return original_replace(source, target)
        with mock.patch("os.replace", side_effect=observed_replace):
            self.updater.run_update(self.root, fetcher=self.fetch)
        self.assertCountEqual(calls, paths)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_second_replace_failure_rolls_back_both_outputs(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "Clash" / "Claude.yaml", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        self.bodies["v2fly"] += b"full:new.anthropic.com\n"
        self.bodies["metacubex"] += b"  - DOMAIN,new.anthropic.com\n"
        original_replace = os.replace
        attempts = 0
        def failing_replace(source, target):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("synthetic rename failure")
            return original_replace(source, target)
        with mock.patch("os.replace", side_effect=failing_replace):
            with self.assertRaisesRegex(OSError, "synthetic rename failure"):
                self.updater.run_update(self.root, fetcher=self.fetch)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def run_cli(self):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "update.py").write_bytes(SCRIPT.read_bytes())
        transport = self.root / "offline_transport"
        transport.mkdir(exist_ok=True)
        (transport / "sitecustomize.py").write_text(textwrap.dedent('''
            import io, json, os, time, urllib.request
            time.sleep = lambda seconds: None
            class Response(io.BytesIO):
                status = 200
                headers = {}
                def __init__(self, body, url):
                    super().__init__(body)
                    self.url = url
                def geturl(self):
                    return self.url
            def offline_open(request, **kwargs):
                name = "metacubex" if request.full_url.endswith(".yaml") else "v2fly"
                body = json.loads(os.environ["CLAUDE_TEST_BODIES"])[name]
                if body is None:
                    raise OSError("synthetic transport outage")
                return Response(body.encode(), request.full_url)
            urllib.request.urlopen = offline_open
        '''), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(transport)
        env["CLAUDE_TEST_BODIES"] = json.dumps({key: value.decode() if value is not None else None
                                               for key, value in self.bodies.items()})
        return subprocess.run([sys.executable, "scripts/update.py"], cwd=self.root, env=env,
                              text=True, capture_output=True, timeout=10)

    def test_cli_success_reports_explicit_summary(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "added=10 retained=0 count=10 changed=true\n")
        self.assertEqual(result.stderr, "")
        self.assertTrue((self.root / "Loon" / "Claude.list").exists())
        self.assertTrue((self.root / "Clash" / "Claude.yaml").exists())
        self.assertTrue((self.root / "metadata.json").exists())

    def test_cli_second_run_reports_no_change(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "added=0 retained=10 count=10 changed=false\n")

    def test_cli_failure_is_nonzero_preserving_old_file_bytes(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        original = self.bodies.copy()
        for source in ("v2fly", "metacubex"):
            for body in [b"", b"<html>failure</html>", b"unknown format", None]:
                with self.subTest(source=source, body=body):
                    self.bodies = original | {source: body}
                    result = self.run_cli()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("update failed:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual({p: p.read_bytes() for p in paths}, before)


class FakeResponse(io.BytesIO):
    def __init__(self, body, url, headers=None, status=200):
        super().__init__(body)
        self.headers = headers or {}
        self.status = status
        self.url = url
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.updater = load_updater()
        self.url = self.updater.SOURCES[0][1]

    def test_fetch_uses_verified_tls_timeout_and_bounded_reads(self):
        response = FakeResponse(b"claude.ai\n", self.url)
        calls = []
        def opener(request, *, timeout, context):
            self.assertEqual(request.full_url, self.url)
            self.assertEqual(timeout, 20)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            calls.append(request)
            return response
        self.assertEqual(self.updater.fetch_source(self.url, opener=opener), b"claude.ai\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.read_sizes, [256 * 1024 + 1])
        self.assertTrue(response.closed)

    def test_fetch_rejects_oversize_html_status_and_redirects(self):
        cases = [
            (b"x" * (256 * 1024 + 1), {}, 200, self.url),
            (b"claude.ai\n", {"Content-Length": str(256 * 1024 + 1)}, 200, self.url),
            (b"claude.ai\n", {"Content-Length": "garbage"}, 200, self.url),
            (b"claude.ai\n", {"Content-Type": "text/html; charset=utf-8"}, 200, self.url),
            (b"claude.ai\n", {}, 403, self.url),
            (b"claude.ai\n", {}, 200, "http://raw.githubusercontent.com/unsafe"),
        ]
        for body, headers, status, url in cases:
            with self.subTest(length=len(body), headers=headers, status=status, url=url):
                response = FakeResponse(body, url, headers, status)
                with self.assertRaises(ValueError):
                    self.updater.fetch_source(self.url, opener=lambda *a, **k: response)
                self.assertTrue(response.closed)

    def test_fetch_refuses_any_url_outside_fixed_sources(self):
        def forbidden_opener(*args, **kwargs):
            self.fail("Unapproved URL must never reach transport")
        for url in ["http://127.0.0.1/", "https://example.com/rules", self.url + "?token=secret"]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.updater.fetch_source(url, opener=forbidden_opener)

    def test_transient_failures_retry_at_most_three_times(self):
        for error_factory in [lambda: urllib.error.URLError("offline"),
                              lambda: urllib.error.HTTPError(self.url, 503, "offline", {}, None),
                              lambda: TimeoutError("synthetic timeout")]:
            with self.subTest(error=type(error_factory()).__name__):
                attempts = []
                delays = []
                def opener(*args, **kwargs):
                    attempts.append(1)
                    if len(attempts) < 3:
                        raise error_factory()
                    return FakeResponse(b"claude.ai\n", self.url)
                self.assertEqual(self.updater.fetch_source(self.url, opener=opener, sleeper=delays.append),
                                 b"claude.ai\n")
                self.assertEqual(len(attempts), 3)
                self.assertEqual(delays, [1, 2])

    def test_exhausted_transport_failures_are_non_silent(self):
        attempts = []
        delays = []
        def opener(*args, **kwargs):
            attempts.append(1)
            raise urllib.error.URLError("synthetic failure")
        with self.assertRaisesRegex(OSError, "3 attempts"):
            self.updater.fetch_source(self.url, opener=opener, sleeper=delays.append)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [1, 2])

    def test_nontransient_http_error_is_not_retried(self):
        attempts = []
        delays = []
        def opener(*args, **kwargs):
            attempts.append(1)
            raise urllib.error.HTTPError(self.url, 404, "not found", {}, None)
        with self.assertRaises(OSError):
            self.updater.fetch_source(self.url, opener=opener, sleeper=delays.append)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(delays, [])

    def test_fetch_refuses_truncated_response(self):
        response = FakeResponse(b"claude.ai\n", self.url, {"Content-Length": "100"})
        with self.assertRaises(ValueError):
            self.updater.fetch_source(self.url, opener=lambda *a, **k: response)


if __name__ == "__main__":
    unittest.main()
