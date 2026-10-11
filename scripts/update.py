#!/usr/bin/env python3
"""Fail-closed, dependency-free updater for Claude's Loon and Clash routing rules."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request

SOURCES = (
    ("v2fly", "https://raw.githubusercontent.com/v2fly/domain-list-community/master/data/anthropic"),
    ("metacubex", "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/geosite/classical/anthropic.yaml"),
)

PROTECTED_ROOTS = frozenset({
    "google.com", "googleapis.com", "gstatic.com", "cloudflare.com",
    "cloudflare.net", "cloudfront.net", "amazonaws.com", "azureedge.net",
    "azurewebsites.net", "github.io", "github.com", "githubusercontent.com",
    "pages.dev", "workers.dev", "vercel.app", "netlify.app", "b-cdn.net",
    "fastly.net", "akamai.net", "akamaiedge.net", "jsdelivr.net", "unpkg.com",
})
TRACKING_ROOTS = frozenset({
    "usefathom.com", "google-analytics.com", "googletagmanager.com",
    "doubleclick.net", "segment.io", "segment.com", "hotjar.com",
})
PUBLIC_CCTLD_LABELS = frozenset({
    "ac", "co", "com", "edu", "gov", "mil", "net", "org", "nom", "go", "ne", "or",
})
LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z", re.ASCII)


def validate_rule(kind: str, domain: str) -> tuple[str, str]:
    labels = domain.split(".")
    if (kind not in {"DOMAIN", "DOMAIN-SUFFIX"} or not domain.isascii()
            or len(domain) > 253 or len(labels) < 2
            or not all(LABEL.fullmatch(label) for label in labels)
            or not re.fullmatch(r"[a-zA-Z]{2,63}", labels[-1], re.ASCII)):
        raise ValueError("Invalid domain rule")
    domain = domain.lower()
    if (domain in PROTECTED_ROOTS
            or (len(labels) == 2 and len(labels[-1]) == 2
                and labels[0].lower() in PUBLIC_CCTLD_LABELS)
            or any(domain == root or domain.endswith("." + root) for root in TRACKING_ROOTS)):
        raise ValueError("Public/shared root or tracking domain is forbidden")
    return kind, domain


MAX_BYTES = 256 * 1024
MAX_RULES = 64


def parse_source(source: str, content: bytes) -> set[tuple[str, str]]:
    """Parse only the two reviewed grammars, never a generic YAML language."""
    if source not in {"v2fly", "metacubex"} or len(content) > MAX_BYTES:
        raise ValueError("Unknown source or source exceeds 256 KiB")
    text = content.decode("utf-8")
    if "<" in text or ">" in text:
        raise ValueError("HTML is not a rule source")
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if source == "metacubex":
        if not lines or lines.pop(0) != "payload:":
            raise ValueError("Missing classical YAML payload header")
    rules = set()
    for line in lines:
        if source == "v2fly":
            kind, domain = (("DOMAIN", line[5:]) if line.startswith("full:")
                            else ("DOMAIN-SUFFIX", line))
        else:
            if not line.startswith("- "):
                raise ValueError("Expected a classical YAML list item")
            value = line[2:].strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                    raise ValueError("Invalid YAML quoting")
                value = value[1:-1]
            parts = value.split(",")
            if len(parts) != 2:
                raise ValueError("Only policy-free DOMAIN/DOMAIN-SUFFIX are supported")
            kind, domain = parts
        rules.add(validate_rule(kind, domain))
    if not rules or len(rules) > MAX_RULES:
        raise ValueError("Source must contain 1..64 unique rules")
    return rules


def parse_rule_list(content: bytes) -> set[tuple[str, str]]:
    if len(content) > MAX_BYTES:
        raise ValueError("Local rule list exceeds 256 KiB")
    rules = set()
    for raw_line in content.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("AND,") or line.startswith("DOMAIN-KEYWORD,stun"):
            continue
        parts = line.split(",")
        if len(parts) != 2:
            raise ValueError("Invalid local rule list")
        rules.add(validate_rule(*parts))
    if not rules or len(rules) > MAX_RULES:
        raise ValueError("Local rule list must contain 1..64 unique rules")
    return rules


def fetch_source(url: str, *, opener=None, sleeper=time.sleep) -> bytes:
    """Three finite attempts, 20s socket timeout each, verified TLS, 256 KiB cap."""
    if url not in dict(SOURCES).values():
        raise ValueError("Only the two fixed source URLs may be fetched")
    request = urllib.request.Request(url, headers={
        "User-Agent": "claude-rules-updater/1", "Accept": "text/plain, application/yaml",
        "Accept-Encoding": "identity",
    })
    context = ssl.create_default_context()
    for attempt in range(3):
        try:
            with (opener or urllib.request.urlopen)(request, timeout=20, context=context) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError("Unexpected HTTP status or redirected source")
                if "html" in response.headers.get("Content-Type", "").lower():
                    raise ValueError("HTML response refused")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                    raise ValueError("Invalid or oversized Content-Length")
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("Source exceeds 256 KiB")
                if length is not None and len(body) != int(length):
                    raise ValueError("Truncated or mismatched response length")
                return body
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in {429, 500, 502, 503, 504}:
                raise OSError(f"Source HTTP {error.code}; no retry") from error
            last_error = error
        except (OSError, http.client.HTTPException) as error:
            last_error = error
        if attempt < 2:
            sleeper(2 ** attempt)
    raise OSError("Source fetch failed after 3 attempts") from last_error


def atomic_write_outputs(changes: list[tuple[Path, bytes, bytes | None]]) -> None:
    temporary_paths = []
    staged = []
    replaced = []

    def stage(target: Path, content: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="." + target.name + ".", suffix=".tmp",
                                         dir=target.parent, delete=False) as handle:
            path = Path(handle.name)
            temporary_paths.append(path)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o644)
        return path

    try:
        for target, new_bytes, old_bytes in changes:
            new_path = stage(target, new_bytes)
            backup = stage(target, old_bytes) if old_bytes is not None else None
            staged.append((target, new_path, backup))
        try:
            for target, new_path, backup in staged:
                os.replace(new_path, target)
                replaced.append((target, backup))
        except OSError:
            for target, backup in reversed(replaced):
                if backup is None:
                    target.unlink()
                else:
                    os.replace(backup, target)
            raise
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


def run_update(root: Path, *, fetcher=None) -> dict[str, int | bool]:
    fetcher = fetcher or fetch_source
    root = Path(root)
    output = root / "Loon" / "Claude.list"
    old_rules_bytes = output.read_bytes() if output.exists() else None
    previous = parse_rule_list(old_rules_bytes) if old_rules_bytes is not None else set()
    clash_output = root / "Clash" / "Claude.yaml"
    old_clash_bytes = clash_output.read_bytes() if clash_output.exists() else None
    metadata_path = root / "metadata.json"
    old_metadata_bytes = metadata_path.read_bytes() if metadata_path.exists() else None
    seed = parse_rule_list((root / "data" / "seed.list").read_bytes())
    collected = []
    evidence = []
    for name, url in SOURCES:
        body = fetcher(url)
        rules = parse_source(name, body)
        collected.append(rules)
        evidence.append({"name": name, "url": url, "rule_count": len(rules),
                         "sha256": hashlib.sha256(body).hexdigest()})
    if collected[0] != collected[1]:
        raise ValueError("Upstream rule sets disagree; preserving previous output")
    rules = seed | previous | collected[0]
    if len(rules) > MAX_RULES:
        raise ValueError("Merged rule set exceeds 64 rules; manual review required")
    sorted_rules = sorted(rules)

    # Loon 格式渲染：前置防 WebRTC/STUN 泄漏 + 前置防 Claude QUIC/UDP 泄漏 + 基础代理规则
    loon_lines = [
        "# === WebRTC / STUN Leak Prevention ===",
        "AND,((PROTOCOL,UDP),(DEST-PORT,3478)),REJECT",
        "AND,((PROTOCOL,UDP),(DEST-PORT,5349)),REJECT",
        "# === Claude QUIC / UDP Leak Prevention ===",
    ]
    for kind, domain in sorted_rules:
        loon_lines.append(f"AND,((PROTOCOL,UDP),({kind},{domain})),REJECT")
    loon_lines.append("# === Claude Regular Proxy Rules ===")
    for kind, domain in sorted_rules:
        loon_lines.append(f"{kind},{domain}")
    rendered = ("\n".join(loon_lines) + "\n").encode("ascii")

    # Clash 格式渲染 (Classical Rule-set)
    clash_lines = [
        "payload:",
        "  # === WebRTC / STUN Leak Prevention ===",
        "  - 'AND,((PROTOCOL,UDP),(DST-PORT,3478))'",
        "  - 'AND,((PROTOCOL,UDP),(DST-PORT,5349))'",
        "  # === Claude QUIC / UDP Leak Prevention ===",
    ]
    for kind, domain in sorted_rules:
        clash_lines.append(f"  - 'AND,((PROTOCOL,UDP),({kind},{domain}))'")
    clash_lines.append("  # === Claude Regular Proxy Rules ===")
    for kind, domain in sorted_rules:
        clash_lines.append(f"  - {kind},{domain}")
    clash_rendered = ("\n".join(clash_lines) + "\n").encode("ascii")
    metadata = {
        "schema_version": 1, "rule_count": len(rules), "seed_rule_count": len(seed),
        "rules_sha256": hashlib.sha256(rendered).hexdigest(), "sources": evidence,
    }
    metadata_bytes = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")
    changes = [(path, new_bytes, old_bytes) for path, new_bytes, old_bytes in (
        (output, rendered, old_rules_bytes),
        (clash_output, clash_rendered, old_clash_bytes),
        (metadata_path, metadata_bytes, old_metadata_bytes),
    ) if new_bytes != old_bytes]
    atomic_write_outputs(changes)
    return {"added": len(rules - previous), "retained": len(previous),
            "count": len(rules), "changed": bool(changes)}


def main(*, root: Path | None = None, fetcher=None) -> int:
    try:
        summary = run_update(root or Path(__file__).resolve().parents[1], fetcher=fetcher)
    except (OSError, ValueError) as error:
        print(f"update failed: {error}", file=sys.stderr)
        return 1
    print(f"added={summary['added']} retained={summary['retained']} "
          f"count={summary['count']} changed={str(summary['changed']).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
