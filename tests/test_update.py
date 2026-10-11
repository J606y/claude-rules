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

    def test_html_and_non_utf8_are_rejected(self):
        updater = load_updater()
        for body in [b"<!DOCTYPE html><html>403</html>", b"<title>Blocked</title>", b"\xff\xfe\x00"]:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    updater.parse_source("v2fly", body)

    def test_empty_sources_are_rejected(self):
        updater = load_updater()
        for body in [b"", b"\n", b"# only comment\n", b"payload:\n", b"payload:\n  # comment\n"]:
            with self.subTest(body=body):
                source = "metacubex" if b"payload" in body else "v2fly"
                with self.assertRaises(ValueError):
                    updater.parse_source(source, body)

    def test_unknown_grammars_and_policy_injection_are_rejected(self):
        updater = load_updater()
        rejected = [
            ("v2fly", b"DOMAIN-SUFFIX,claude.ai,AI\n"),
            ("v2fly", b"IP-CIDR,1.1.1.1/32\n"),
            ("metacubex", b"payload:\n  - DOMAIN-SUFFIX,claude.ai,AI\n"),
            ("metacubex", b"payload:\n  - IP-CIDR,1.1.1.1/32\n"),
            ("metacubex", b"payload:\n  - 'invalid'quote'\n"),
            ("metacubex", b"plain_list_without_payload:\n"),
        ]
        for source, body in rejected:
            with self.subTest(source=source, body=body):
                with self.assertRaises(ValueError):
                    updater.parse_source(source, body)

    def test_case_normalization_and_maximum_count(self):
        updater = load_updater()
        body = b"CLAUDE.AI\nclaude.ai\n"
        self.assertEqual(updater.parse_source("v2fly", body), {("DOMAIN-SUFFIX", "claude.ai")})

    def test_source_bytes_and_rule_limits_are_enforced(self):
        updater = load_updater()
        with self.assertRaises(ValueError):
            updater.parse_source("v2fly", b"#" + b"x" * (256 * 1024))
        overflow = b"".join(f"node{i}.claude.ai\n".encode() for i in range(65))
        with self.assertRaises(ValueError):
            updater.parse_source("v2fly", overflow)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.updater = load_updater()

    def test_fetch_uses_verified_tls_timeout_and_bounded_reads(self):
        called = {}
        class FakeResponse(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/plain", "Content-Length": "11"}
            def geturl(self):
                return self.url
        def fake_open(request, timeout=None, context=None):
            called["timeout"] = timeout
            called["context"] = context
            called["url"] = request.full_url
            called["headers"] = dict(request.header_items())
            res = FakeResponse(b"claude.ai\n")
            res.url = request.full_url
            return res

        url = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
        body = self.updater.fetch_source(url, opener=fake_open)
        self.assertEqual(body, b"claude.ai\n")
        self.assertEqual(called["timeout"], 20)
        self.assertIsInstance(called["context"], ssl.SSLContext)
        self.assertTrue(called["context"].check_hostname)
        self.assertEqual(called["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(called["url"], url)
        self.assertEqual(called["headers"]["User-agent"], "claude-rules-updater/1")

    def test_fetch_rejects_oversize_html_status_and_redirects(self):
        class DummyResponse(io.BytesIO):
            def __init__(self, body, status=200, content_type="text/plain", url="same"):
                super().__init__(body)
                self.status = status
                self.headers = {"Content-Type": content_type}
                self.url = url
            def geturl(self):
                return self.url

        url = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
        cases = [
            DummyResponse(b"ok", status=500, url=url),
            DummyResponse(b"ok", url="https://redirected.example/rules"),
            DummyResponse(b"<html>Error</html>", content_type="text/html", url=url),
            DummyResponse(b"x" * (256 * 1024 + 2), url=url),
        ]
        for res in cases:
            with self.subTest(status=res.status, url=res.geturl(), ct=res.headers.get("Content-Type")):
                with self.assertRaises(ValueError):
                    self.updater.fetch_source(url, opener=lambda req, timeout=20, context=None, r=res: r,
                                              sleeper=lambda s: None)

    def test_fetch_refuses_truncated_response(self):
        class ShortResponse(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/plain", "Content-Length": "100"}
            def geturl(self):
                return self.url
        url = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
        res = ShortResponse(b"short")
        res.url = url
        with self.assertRaises(ValueError):
            self.updater.fetch_source(url, opener=lambda req, timeout=20, context=None: res,
                                      sleeper=lambda s: None)

    def test_transient_failures_retry_at_most_three_times(self):
        attempts = 0
        slept = []
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/plain"}
            def geturl(self):
                return self.url
        def flake(request, timeout=20, context=None):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)
            res = Response(b"claude.ai\n")
            res.url = request.full_url
            return res
        url = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
        body = self.updater.fetch_source(url, opener=flake, sleeper=slept.append)
        self.assertEqual(body, b"claude.ai\n")
        self.assertEqual(attempts, 3)
        self.assertEqual(slept, [1, 2])

    def test_nontransient_http_error_is_not_retried(self):
        attempts = 0
        def fail_404(request, timeout=20, context=None):
            nonlocal attempts
            attempts += 1
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        url = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
        with self.assertRaises(OSError):
            self.updater.fetch_source(url, opener=fail_404, sleeper=lambda s: self.fail("Must not sleep"))
        self.assertEqual(attempts, 1)

    def test_exhausted_transport_failures_are_non_silent(self):
        attempts = 0
        def always_fail(request, timeout=20, context=None):
            nonlocal attempts
            attempts += 1
            raise OSError("network down")
        url = "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"
        with self.assertRaisesRegex(OSError, "Source fetch failed after 3 attempts"):
            self.updater.fetch_source(url, opener=always_fail, sleeper=lambda s: None)
        self.assertEqual(attempts, 3)

    def test_fetch_refuses_any_url_outside_fixed_sources(self):
        with self.assertRaises(ValueError):
            self.updater.fetch_source("https://example.com/other")


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.updater = load_updater()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="claude-rules-")
        self.root = Path(self.temp_dir.name)
        data = self.root / "data"
        data.mkdir()
        (data / "seed.list").write_text(textwrap.dedent("""
            DOMAIN-SUFFIX,anthropic.com
            DOMAIN-SUFFIX,clau.de
            DOMAIN-SUFFIX,claude.ai
            DOMAIN-SUFFIX,claude.com
            DOMAIN-SUFFIX,claude.dev
            DOMAIN-SUFFIX,claudemcpclient.com
            DOMAIN-SUFFIX,claudemcpcontent.com
            DOMAIN-SUFFIX,claudeusercontent.com
            DOMAIN,servd-anthropic-website.b-cdn.net
        """).strip() + "\n", encoding="utf-8")
        self.bodies = {
            "v2fly": b"claude.ai\nfull:api.anthropic.com\n",
            "metacubex": b"payload:\n  - DOMAIN,api.anthropic.com\n  - DOMAIN-SUFFIX,claude.ai\n",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def fetch(self, url: str) -> bytes:
        for source, source_url in self.updater.SOURCES:
            if source_url == url:
                return self.bodies[source]
        self.fail(f"Unexpected source URL: {url}")

    def test_generates_policy_free_sorted_rules_and_source_evidence(self):
        summary = self.updater.run_update(self.root, fetcher=self.fetch)
        result = (self.root / "Loon" / "Claude.list").read_bytes()
        lines = result.decode().splitlines()
        self.assertEqual(lines, sorted(set(lines)))
        self.assertTrue(all(len(line.split(",")) == 2 for line in lines))
        expected_seeds = {
            "DOMAIN-SUFFIX,anthropic.com", "DOMAIN-SUFFIX,clau.de", "DOMAIN-SUFFIX,claude.ai",
            "DOMAIN-SUFFIX,claude.com", "DOMAIN-SUFFIX,claude.dev",
            "DOMAIN-SUFFIX,claudemcpclient.com", "DOMAIN-SUFFIX,claudemcpcontent.com",
            "DOMAIN-SUFFIX,claudeusercontent.com", "DOMAIN,servd-anthropic-website.b-cdn.net",
        }
        self.assertEqual(set(lines), expected_seeds | {"DOMAIN,api.anthropic.com"})
        self.assertEqual(summary, {"added": 10, "retained": 0, "count": 10, "changed": True})

        clash_result = (self.root / "Clash" / "Claude.yaml").read_bytes()
        clash_lines = clash_result.decode().splitlines()
        self.assertEqual(clash_lines[0], "payload:")
        self.assertEqual([line[4:] for line in clash_lines[1:]], lines)

        metadata = json.loads((self.root / "metadata.json").read_bytes())
        self.assertEqual(metadata["rule_count"], len(lines))
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
        self.assertEqual(result.stderr, "")

    def test_cli_failure_is_nonzero_preserving_old_file_bytes(self):
        self.updater.run_update(self.root, fetcher=self.fetch)
        paths = [self.root / "Loon" / "Claude.list", self.root / "metadata.json"]
        before = {p: p.read_bytes() for p in paths}
        self.bodies["v2fly"] = b"invalid payload format\n"
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("update failed:", result.stderr)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)


if __name__ == "__main__":
    unittest.main()
