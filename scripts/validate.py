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
        fail("schema", "jsonschema is not installed, so the schema was never checked")
        return
    schema = load("schema/store-v1.json")
    if schema is None:
        return
    report(jsonschema.Draft202012Validator(schema), index, "index.json")

    # Every release of every plugin, not just the one index.json calls latest. Without this, an
    # older release could carry anything at all.
    detail_schema = dict(schema)
    detail_schema.pop("$id", None)
    detail_schema.update({"$ref": "#/$defs/detail"})
    for key in ("type", "required", "additionalProperties", "properties"):
        detail_schema.pop(key, None)
    validator = jsonschema.Draft202012Validator(detail_schema)
    for plugin in index.get("plugins", []):
        pid = plugin.get("id")
        detail = load(f"plugins/{pid}.json")
        if detail is not None:
            report(validator, detail, f"plugins/{pid}.json")


def report(validator, document, name):
    for e in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        where = name + "".join(f"[{p!r}]" for p in e.path)
        fail(where, e.message)


def version_key(version):
    """Sortable form of a version, comparing numerically so 0.10.0 beats 0.9.0. Anything that
    isn't a number sorts lowest rather than raising: a bad version is the schema's finding to
    report, not a stack trace out of here."""
    out = []
    for part in str(version).split("+")[0].split("-")[0].split("."):
        out.append(int(part) if part.isdigit() else -1)
    return out


def newest(releases):
    return max(releases, key=lambda r: version_key(r.get("version", "")))


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
            if not plugin.get("image"):
                fail(where, f"there is no plugins/{pid}.json")
            continue
        check_plugin(plugin, detail, pid, where)


def check_plugin(plugin, detail, pid, where):
    """One plugin's index entry against its own file."""
    if detail.get("id") != pid:
        fail(f"plugins/{pid}.json", f"id is {detail.get('id')!r}, index says {pid!r}")
    releases = detail.get("releases") or []
    if not releases:
        fail(f"plugins/{pid}.json", "no releases")
        return

    versions = [r.get("version") for r in releases]
    if len(set(versions)) != len(versions):
        fail(f"plugins/{pid}.json", "the same version is listed twice")

    check_downloads_belong(plugin, releases, pid)

    latest = plugin.get("latest", {})
    if not latest and plugin.get("image"):
        return  # an attached plugin has a container, not a bundle
    top = newest(releases)
    if latest.get("version") != top["version"]:
        fail(where, f"latest is {latest.get('version')}, "
                    f"but plugins/{pid}.json's newest release is {top['version']}")
    elif latest != top:
        differ = sorted(k for k in set(latest) | set(top) if latest.get(k) != top.get(k))
        fail(where, f"latest disagrees with plugins/{pid}.json on: {', '.join(differ)}")


def check_downloads_belong(plugin, releases, pid):
    """A checksum only means something if the bundle comes from the plugin's own repo: otherwise
    one pull request can point the download and the hash at the same place."""
    home = (plugin.get("homepage") or "").rstrip("/")
    if not home.startswith("https://github.com/"):
        return
    want = home + "/releases/download/"
    for r in releases:
        if not str(r.get("url", "")).startswith(want):
            fail(f"plugins/{pid}.json {r.get('version')}",
                 f"the download is not a release of {home}: {r.get('url')}")


def https_only(url):
    """A URL is about to be opened on a runner, from a file anyone can open a pull request to
    change. Anything but https — file:, ftp:, a custom scheme — is refused rather than fetched."""
    if not url.startswith("https://"):
        raise ValueError(f"not an https URL: {url!r}")
    return url


def head(url):
    request = urllib.request.Request(https_only(url), method="HEAD")  # noqa: S310 - checked above
    request.add_header("User-Agent", "repeatertastic-plugins/validate")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.status, response.headers.get("Content-Length")


def check_reachable(index, deep):
    for plugin in index.get("plugins", []):
        pid = plugin.get("id")
        detail = load(f"plugins/{pid}.json") or {}
        for release in detail.get("releases", []):
            check_release(pid, release, deep)


def check_release(pid, release, deep):
    where = f"{pid} {release.get('version')}"
    url = release.get("url", "")
    try:
        status, length = head(url)
    except Exception as e:  # noqa: BLE001 - any failure to reach it is the finding
        fail(where, f"{url} did not answer, {e}")
        return
    if status != 200:
        fail(where, f"{url} returned {status}")
    elif length is not None and int(length) != release.get("size"):
        fail(where, f"size says {release.get('size')} but the server serves {length} bytes")
    if deep:
        check_checksum(where, url, release)


def check_checksum(where, url, release):
    """Download it and hash it, rather than taking the index's word for it."""
    digest = hashlib.sha256()
    with urllib.request.urlopen(https_only(url), timeout=300) as response:  # noqa: S310
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != release.get("sha256"):
        fail(where, f"sha256 is {digest.hexdigest()}, "
                    f"the index says {release.get('sha256')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip every network check")
    parser.add_argument("--deep", action="store_true",
                        help="also download each release and verify its sha256")
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
