"""Audit a committed npm v3 lockfile. Never install packages or run their scripts."""
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib import error, request

NAME = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
SPEC = r"(?:[0-9v~^<>*=xX][0-9a-zA-Z .~^<>=*|+-]*|[a-zA-Z][a-zA-Z0-9._-]*)"
DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
LEVELS = ("critical", "high", "moderate", "low", "info")
OSV = "https://api.osv.dev/v1/querybatch"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_spec(value):
    require(re.fullmatch(rf"(?:npm:{NAME}@)?{SPEC}", value), "unsupported dependency specifier")


def check_overrides(overrides):
    for key, value in overrides.items():
        require(key == "." or re.fullmatch(rf"{NAME}(?:@{SPEC})?", key), "invalid npm override")
        if isinstance(value, dict):
            check_overrides(value)
        else:
            check_spec(value)


def public_packages(lock):
    require(lock.get("lockfileVersion") == 3 and "dependencies" not in lock, "expected npm lockfile v3")
    entries = lock["packages"]
    workspaces = entries[""]["workspaces"]
    require(isinstance(workspaces, list) and "packages/desktop" in workspaces
            and "packages/server" in workspaces and "packages/cli" in workspaces, "missing Desktop workspace")
    names = {}
    for path in workspaces:
        require(re.fullmatch(r"plugins|packages/[a-z0-9][a-z0-9_-]*", path), "invalid local workspace path")
        names[path] = entries[path]["name"]
        require(re.fullmatch(NAME, names[path]), "invalid workspace name")
        link = entries.get("node_modules/" + names[path], {})
        require(link.get("link") is True and link["resolved"] == path, "missing workspace link")
    require(names["packages/desktop"] == "@getpaseo/desktop" and
            {"@getpaseo/cli", "@getpaseo/server"} <= entries["packages/desktop"]["dependencies"].keys(),
            "unexpected Desktop workspace dependencies")
    registry_packages = set()
    for path, item in entries.items():
        for field in DEP_FIELDS:
            for dep, spec in item.get(field, {}).items():
                require(re.fullmatch(NAME, dep), "invalid dependency name")
                check_spec(spec)
        if path in names or path == "":
            require("resolved" not in item and not item.get("link"), "invalid local workspace entry")
            require(re.fullmatch(NAME, item["name"]) and re.fullmatch(VERSION, item["version"]),
                    "invalid local package identity")
            continue
        prefix, *components = path.split("node_modules/")
        require(components and (not prefix or prefix.endswith("/") and prefix[:-1] in names)
                and all(c.endswith("/") and re.fullmatch(NAME, c[:-1]) for c in components[:-1])
                and re.fullmatch(NAME, components[-1]), "invalid lockfile package path")
        if item.get("link"):
            target = item["resolved"]
            require(target in names and path == "node_modules/" + names[target], "invalid workspace link")
            continue
        name, version = item.get("name", components[-1]), item["version"]
        require(re.fullmatch(NAME, name) and re.fullmatch(VERSION, version), "invalid npm identity")
        filename = name.rsplit("/", 1)[-1] + "-" + version + ".tgz"
        require(item["resolved"] == f"https://registry.npmjs.org/{name}/-/{filename}",
                "package URL does not match its public npm identity")
        algorithm, digest = item["integrity"].split("-", 1)
        require(algorithm == "sha512" and len(base64.b64decode(digest, validate=True)) == 64,
                "missing SHA-512 integrity")
        registry_packages.add((name, version))
    require(registry_packages, "empty registry package inventory")
    return sorted(registry_packages)


def prepare(archive, directory):
    def read_file(name):
        member = archive.getmember(name)
        require(member.isfile(), "expected a regular committed manifest")
        return archive.extractfile(member).read()

    raw = read_file("package-lock.json")
    lock = json.loads(raw)
    inventory = public_packages(lock)
    (directory / "package-lock.json").write_bytes(raw)
    for workspace in ["", *lock["packages"][""]["workspaces"]]:
        dest = directory / workspace / "package.json"
        manifest = json.loads(read_file(str(dest.relative_to(directory))))
        entry = lock["packages"][workspace]
        fields = ("name", "version", "workspaces", *DEP_FIELDS)
        require(all(manifest.get(k) == entry.get(k) for k in fields), "manifest disagrees with lockfile")
        # Project config, lifecycle hooks and unrelated package fields are unnecessary for audit.
        manifest = {k: manifest[k] for k in (*fields, "overrides") if k in manifest}
        check_overrides(manifest.get("overrides", {}))
        dest.parent.mkdir(exist_ok=True, parents=True)
        dest.write_text(json.dumps(manifest))
    return inventory, hashlib.sha256(raw).hexdigest()


def npm_audit(directory, reports, scope, args, lines):
    command = ["npm", "audit", "--package-lock-only", "--ignore-scripts", "--json", "--audit-level=high",
               "--registry=https://registry.npmjs.org/", *args]
    proc = subprocess.run(command, cwd=directory, capture_output=True, timeout=480)
    (reports / f"npm-{scope}.json").write_bytes(proc.stdout)
    if proc.stderr:
        (reports / f"npm-{scope}.log").write_bytes(proc.stderr)
    report = json.loads(proc.stdout)
    require(report.get("auditReportVersion") == 2 and "error" not in report, "npm registry audit failed")
    findings = report["vulnerabilities"]
    packages = json.loads((directory / "package-lock.json").read_bytes())["packages"]
    counts = Counter()
    details = []
    for package, finding in findings.items():
        level = finding["severity"]
        require(re.fullmatch(NAME, package) and level in LEVELS, "invalid npm finding")
        versions = set()
        for node in finding["nodes"]:
            entry = packages[node]
            if entry.get("link"):
                entry = packages[entry["resolved"]]
            versions.add(entry["version"])
        require(versions, "npm finding missing affected lockfile nodes")
        counts[level] += 1
        via = set()
        for item in finding["via"]:
            if isinstance(item, str) and re.fullmatch(NAME, item):
                via.add(item)
            elif isinstance(item, dict):
                url = item.get("url", "")
                if re.fullmatch(r"https://github\.com/advisories/GHSA-[a-z0-9-]+", url):
                    via.add(url)
        details.append((LEVELS.index(level), package + "@" + ", ".join(sorted(versions)), ", ".join(sorted(via))))
    meta = report["metadata"]["vulnerabilities"]
    require(all(counts[s] == meta[s] for s in LEVELS) and len(findings) == meta["total"],
            "inconsistent npm advisory counts")
    blocked = bool(counts["high"] + counts["critical"])
    require(proc.returncode == int(blocked), "npm audit process did not complete successfully")
    lines.append("  " + ", ".join(f"{s} {counts[s]}" for s in LEVELS))
    for severity, package, via in sorted(details):
        lines.append(f"  {LEVELS[severity]} {package}" + (f" via {via}" if via else ""))
    return blocked


def desktop_findings(directory, reports, lines):
    helper = Path(__file__).with_name("security-runtime.cjs")
    proc = subprocess.run(["node", str(helper), str(reports / "npm-workspace.json")],
                          cwd=directory, capture_output=True, timeout=120)
    if proc.stderr:
        (reports / "npm-desktop-production.log").write_bytes(proc.stderr)
    require(proc.returncode == 0, "unable to determine Desktop production dependencies")
    data = json.loads(proc.stdout)
    (reports / "npm-desktop-production.json").write_bytes(proc.stdout)
    lines.append(f"  {len(data['runtimePaths'])} lockfile paths via runtime dependency and peer edges.")
    lines.append("  Includes compatible optional dependencies. Packaging can prune optional components.")
    lines.append("  The full lock covers the renderer workspace. Its bundled JavaScript module list is unverified.")
    for label, matches in [("Direct runtime advisories", data["runtimeAdvisories"]),
                           ("Shipped Electron " + data["electron"]["version"], data["electron"]["advisories"])]:
        lines.append(f"  {label}: {len(matches)} advisory/version matches")
        for hit in matches:
            require(hit["severity"] in LEVELS and re.fullmatch(NAME, hit["name"])
                    and re.fullmatch(VERSION, hit["version"]), "invalid matched advisory")
            url = hit.get("url", "")
            link = url if re.fullmatch(r"https://github\.com/advisories/GHSA-[a-z0-9-]+", url) else ""
            lines.append(f"  {hit['severity']} {hit['name']}@{hit['version']} {link}".rstrip())


def query_osv(queries):
    data = json.dumps({"queries": queries}).encode()
    req = request.Request(OSV, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except (error.URLError, TimeoutError) as exc:
            if attempt == 2 or isinstance(exc, error.HTTPError) and exc.code < 500 and exc.code != 429:
                raise
            time.sleep(2 ** attempt)


def malware_scan(inventory, result, query=query_osv):
    for start in range(0, len(inventory), 100):
        batch = [{"package": {"ecosystem": "npm", "name": name}, "version": version}
                 for name, version in inventory[start:start + 100]]
        seen = set()
        while batch:
            answer = query(batch)
            require(isinstance(answer, dict) and set(answer) == {"results"}, "invalid OSV batch response")
            rows = answer["results"]
            require(isinstance(rows, list) and len(rows) == len(batch), "OSV returned an incomplete batch")
            next_batch = []
            for pin, row in zip(batch, rows):
                require(isinstance(row, dict) and set(row) <= {"vulns", "next_page_token"},
                        "OSV returned a package error")
                vulns = row.get("vulns", [])
                require(isinstance(vulns, list), "invalid OSV vulnerabilities")
                for vuln in vulns:
                    id_ = vuln["id"]
                    require(re.fullmatch(r"[A-Za-z0-9._:-]+", id_), "invalid OSV advisory ID")
                    if id_.startswith("MAL-"):
                        require(re.fullmatch(r"MAL-\d{4}-\d+", id_), "invalid OSV malware ID")
                        result["findings"].append({"package": pin["package"]["name"], "version": pin["version"],
                                                   "id": id_, "url": "https://osv.dev/vulnerability/" + id_})
                if "next_page_token" in row:
                    token = row["next_page_token"]
                    key = (pin["package"]["name"], pin["version"], token)
                    require(isinstance(token, str) and token and key not in seen, "invalid OSV pagination")
                    seen.add(key)
                    next_batch.append({**pin, "page_token": token})
                else:
                    result["packagesChecked"] += 1
            batch = next_batch


def main(commit, reports, inputs):
    lines = ["Npm dependency security scan", f"Commit {commit}",
             "Gate: high/critical npm advisories anywhere in the lock, or any OSV MAL match.",
             "Severity counts represent affected package names."]
    errors = {}

    def record_error(stage, exc):
        errors[stage] = f"{type(exc).__name__}: {exc}"
        # Keep arbitrary registry and manifest text out of Actions log commands and Markdown.
        cause = f"HTTP {exc.code}" if isinstance(exc, error.HTTPError) else type(exc).__name__
        lines.append(f"  INCOMPLETE: {stage} ({cause}). See security-errors.json.")

    blocked = incomplete = False
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        try:
            with tarfile.open(inputs) as archive:
                inventory, digest = prepare(archive, directory)
            lines.append(f"Lock SHA-256 {digest}")
        except Exception as exc:
            record_error("lockfile validation", exc)
            incomplete = True
        if not incomplete:
            try:
                lines.append("All workspaces, build and development dependencies (npm audit)")
                blocked |= npm_audit(directory, reports, "workspace", [], lines)
                lines.append("Desktop ARM64 declared production dependencies, including CLI and daemon")
                desktop_findings(directory, reports, lines)
            except Exception as exc:
                record_error("npm audit or Desktop attribution", exc)
                incomplete = True
            result = {"ecosystem": "npm", "packagesExpected": len(inventory), "packagesChecked": 0, "findings": []}
            try:
                malware_scan(inventory, result)
            except Exception as exc:
                record_error("OSV malware", exc)
                result["error"] = errors["OSV malware"]
                incomplete = True
            (reports / "osv-malware.json").write_text(json.dumps(result, indent=2) + "\n")
            lines.append(f"OSV malware: {len(result['findings'])} matches, "
                         f"{result['packagesChecked']}/{len(inventory)} exact npm package versions checked")
            for finding in result["findings"]:
                lines.append("  {package}@{version} {url}".format(**finding))
            blocked |= bool(result["findings"])
    if errors:
        (reports / "security-errors.json").write_text(json.dumps(errors, indent=2) + "\n")
    lines.append("Gate " + ("INCOMPLETE" if incomplete else "BLOCKED" if blocked else "PASSED"))
    summary = "\n".join(lines) + "\n"
    (reports / "security-summary.txt").write_text(summary)
    print(summary, end="")
    return 2 if incomplete else int(blocked)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path("/reports"), "/inputs.tar"))
