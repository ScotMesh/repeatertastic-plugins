#!/usr/bin/env python3
"""Check the store index before it reaches anybody's RepeaterTastic.

Three passes, cheapest first:

  schema      index.json against schema/store-v1.json
  consistency ids, logos, and index.json's "latest" against plugins/<id>.json
  reachable   one HEAD per download URL, comparing Content-Length to size

The last pass needs the network; skip it with --offline. --deep adds a full
download and sha256 of every release, which is minutes of Actions time, so it
runs on release PRs only.
"""

import argparse
import hashlib
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBLEMS = []


def fail(where, message):
    PROBLEMS.append(f"{where}: {message}")


def load(path):
    try:
        return json.loads((ROOT / path).read_text())
    except FileNotFoundError:
        fail(path, "missing")
    except json.JSONDecodeError as e:
        fail(path, f"not valid JSON, {e}")
    return None


def check_schema(index):
    try:
        import jsonschema
    except ImportError:
        print("  jsonschema not installed, skipping schema pass")
        return
    schema = load("schema/store-v1.json")
    if schema is None:
        return
    validator = jsonschema.Draft202012Validator(schema)
    for e in sorted(validator.iter_errors(index), key=lambda e: e.path):
        where = "index.json" + "".join(f"[{p!r}]" for p in e.path)
        fail(where, e.message)


def newest(releases):
    """Highest version, comparing numerically so 0.10.0 beats 0.9.0."""
    def key(r):
        return [int(p) for p in r["version"].split("-")[0].split(".")]
    return max(releases, key=key)


def check_consistency(index):
    seen = set()
    for plugin in index.get("plugins", []):
        pid = plugin.get("id")
        where = f"index.json plugin {pid!r}"
        if pid in seen:
            fail(where, "id appears twice")
        seen.add(pid)

        logo = plugin.get("logo")
        if logo and not (ROOT / logo).is_file():
            fail(where, f"logo {logo} is not in this repo")

        detail = load(f"plugins/{pid}.json")
        if detail is None:
            continue
        if detail.get("id") != pid:
            fail(f"plugins/{pid}.json", f"id is {detail.get('id')!r}, index says {pid!r}")
        releases = detail.get("releases") or []
        if not releases:
            fail(f"plugins/{pid}.json", "no releases")
            continue

        versions = [r["version"] for r in releases]
        if len(set(versions)) != len(versions):
            fail(f"plugins/{pid}.json", "the same version is listed twice")

        latest = plugin.get("latest", {})
        top = newest(releases)
        if latest.get("version") != top["version"]:
            fail(where, f"latest is {latest.get('version')}, "
                        f"but plugins/{pid}.json's newest release is {top['version']}")
        elif latest != top:
            differ = sorted(k for k in set(latest) | set(top) if latest.get(k) != top.get(k))
            fail(where, f"latest disagrees with plugins/{pid}.json on: {', '.join(differ)}")


def head(url):
    request = urllib.request.Request(url, method="HEAD")
    request.add_header("User-Agent", "repeatertastic-plugins/validate")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.headers.get("Content-Length")


def check_reachable(index, deep):
    for plugin in index.get("plugins", []):
        pid = plugin.get("id")
        detail = load(f"plugins/{pid}.json") or {}
        for release in detail.get("releases", []):
            where = f"{pid} {release.get('version')}"
            url = release.get("url", "")
            try:
                status, length = head(url)
            except Exception as e:
                fail(where, f"{url} did not answer, {e}")
                continue
            if status != 200:
                fail(where, f"{url} returned {status}")
            elif length is not None and int(length) != release.get("size"):
                fail(where, f"size says {release.get('size')} but the server serves {length} bytes")
            if deep:
                digest = hashlib.sha256()
                with urllib.request.urlopen(url, timeout=300) as response:
                    for chunk in iter(lambda: response.read(1 << 20), b""):
                        digest.update(chunk)
                if digest.hexdigest() != release.get("sha256"):
                    fail(where, f"sha256 is {digest.hexdigest()}, the index says {release.get('sha256')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip every network check")
    parser.add_argument("--deep", action="store_true", help="also download each release and verify its sha256")
    args = parser.parse_args()

    index = load("index.json")
    if index is None:
        print("\n".join(PROBLEMS), file=sys.stderr)
        return 1

    print("checking the schema")
    check_schema(index)
    print("checking ids, logos and versions")
    check_consistency(index)
    if args.offline:
        print("skipping the download checks")
    else:
        print("checking every download URL" + (" and its sha256" if args.deep else ""))
        check_reachable(index, args.deep)

    if PROBLEMS:
        print(f"\n{len(PROBLEMS)} problem(s):", file=sys.stderr)
        for problem in PROBLEMS:
            print(f"  {problem}", file=sys.stderr)
        return 1

    count = len(index.get("plugins", []))
    print(f"\n{count} plugin(s), all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
