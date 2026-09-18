#!/usr/bin/env python3
"""Add a plugin release to the store, reading it from the release itself.

    scripts/add_release.py ScotMesh/repeatertastic-meshflow v0.1.2

Downloads the release's bundle, hashes it, reads the plugin.yaml inside, and writes
plugins/<id>.json and index.json. Everything a node needs comes from the bundle, so the store
can't drift from what is actually published: a wrong checksum, a version that disagrees with the
manifest or a permission the plugin doesn't ask for are all impossible this way.

Editorial fields (summary, description, tags, network, logo) are kept from the existing entry, or
taken from the manifest for a plugin that isn't listed yet.

Needs gh on PATH and PyYAML.
"""

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The same ids the schema and the daemon accept. This gates a value that comes out of a zip
# published by someone else, before it is ever joined to a path.
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

# The architectures a bundle can carry, by the suffix its binaries use.
ARCHES = {"amd64": "linux/amd64", "arm64": "linux/arm64", "arm": "linux/arm"}

# What a card may carry, matching the daemon's own list.
LOGO_TYPES = (".png", ".svg", ".webp")
MAX_LOGO = 1 << 20


def run(*args):
    """Run gh. The arguments are a list, never a shell string, so a repo or tag containing shell
    metacharacters is an argument and nothing more."""
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout  # noqa: S603


def release_asset(repo, tag):
    """The one .zip on the release, with its download URL."""
    data = json.loads(run("gh", "api", f"repos/{repo}/releases/tags/{tag}"))
    zips = [a for a in data["assets"] if a["name"].endswith(".zip")]
    if len(zips) != 1:
        sys.exit(f"{repo} {tag} has {len(zips)} zip assets; expected exactly one")
    return zips[0], data.get("published_at")


def read_bundle(path):
    """The manifest, the architectures the bundle carries, and its logo if it has one."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        prefix = ""
        if "plugin.yaml" not in names:
            tops = {n.split("/")[0] for n in names}
            if len(tops) != 1:
                sys.exit("the bundle has no plugin.yaml at its root")
            prefix = tops.pop() + "/"
        manifest = yaml.safe_load(z.read(prefix + "plugin.yaml"))
        arches = sorted({
            tag for name in names
            for suffix, tag in ARCHES.items()
            if re.search(rf"-linux-{suffix}$", name)
        })
        logo = None
        named = str(manifest.get("logo") or "")
        ext = pathlib.Path(named).suffix.lower()
        if named and ext in LOGO_TYPES and prefix + named in names:
            body = z.read(prefix + named)
            if len(body) <= MAX_LOGO:
                logo = (ext, body)
    if not arches:
        sys.exit("no linux binaries named ...-linux-<arch> in the bundle")
    return manifest, arches, logo


def download(url, dest):
    if not url.startswith("https://"):
        sys.exit(f"the release asset is not an https URL: {url!r}")
    headers = {"Accept": "application/octet-stream"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https checked above
    with urllib.request.urlopen(request, timeout=300) as response, open(dest, "wb") as f:  # noqa: S310
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
            f.write(chunk)
    return digest.hexdigest(), size


def load(path, default):
    try:
        return json.loads((ROOT / path).read_text())
    except FileNotFoundError:
        return default


def save(path, data):
    (ROOT / path).write_text(json.dumps(data, indent=2) + "\n")


def version_key(v):
    """Sortable form, numeric part by numeric part. A part that isn't a number sorts lowest
    rather than raising, so a surprising version is reported by the schema, not a traceback."""
    return [int(p) if p.isdigit() else -1
            for p in str(v).split("+")[0].split("-")[0].split(".")]


def build_release(manifest, asset, published, sha256, size, arches, repo, tag, args, detail):
    """The release object as the index and the plugin file both carry it."""
    release = {
        "version": str(manifest["version"]),
        "api": int(manifest.get("api", 1)),
        "released": published,
        "url": f"https://github.com/{repo}/releases/download/{tag}/{asset['name']}",
        "sha256": sha256,
        "size": size,
        "arches": arches,
    }
    if args.notes:
        release["notes"] = args.notes
    # min_host isn't in the manifest: it is a judgement about which hosts can run this build, so
    # it carries over from the last release unless --min-host says otherwise.
    if args.min_host:
        release["min_host"] = args.min_host
    elif detail["releases"]:
        previous = max(detail["releases"], key=lambda r: version_key(r["version"]))
        if previous.get("min_host"):
            release["min_host"] = previous["min_host"]
    # Keep the schema's field order, so the diff on an update is only the values.
    order = ["version", "api", "min_host", "released", "url", "sha256", "size", "arches", "notes"]
    return {k: release[k] for k in order if k in release}


def new_entry(manifest, pid, repo, logo_path):
    """A plugin nobody has listed yet: what the manifest knows, for the pull request to improve."""
    summary = (manifest.get("description") or "").split("\n")[0][:140]
    return {
        "id": pid,
        "name": manifest.get("name", pid),
        "summary": summary or "TODO: one line for the card",
        "description": manifest.get("description", ""),
        "author": manifest.get("author", ""),
        "homepage": manifest.get("homepage", f"https://github.com/{repo}"),
        "license": manifest.get("license", ""),
        "logo": logo_path or f"logos/{pid}.png",
        "permissions": list(manifest.get("permissions", [])),
        "network": list(manifest.get("network", [])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="owner/name of the plugin's GitHub repo")
    parser.add_argument("tag", help="the release tag, e.g. v0.1.2")
    parser.add_argument("--notes", default="", help="one line for the update card")
    parser.add_argument("--min-host", default="",
                        help="oldest RepeaterTastic that can run it "
                             "(kept from the last release otherwise)")
    args = parser.parse_args()

    asset, published = release_asset(args.repo, args.tag)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = pathlib.Path(tmp) / asset["name"]
        print(f"downloading {asset['name']}")
        sha256, size = download(asset["url"], bundle)
        manifest, arches, logo = read_bundle(bundle)

    pid = str(manifest.get("id", ""))
    if not ID.match(pid):
        sys.exit(f"the bundle's id is not one this store accepts: {pid!r}")
    version = str(manifest["version"])
    if version != args.tag.lstrip("v"):
        sys.exit(f"the bundle says version {version} but the tag is {args.tag}")

    detail = load(f"plugins/{pid}.json", {"id": pid, "releases": []})
    release = build_release(manifest, asset, published, sha256, size, arches,
                            args.repo, args.tag, args, detail)
    detail["releases"] = [r for r in detail["releases"] if r["version"] != version] + [release]
    detail["releases"].sort(key=lambda r: version_key(r["version"]))
    save(f"plugins/{pid}.json", detail)

    logo_path = None
    if logo is not None:
        ext, body = logo
        logo_path = f"logos/{pid}{ext}"
        (ROOT / "logos").mkdir(exist_ok=True)
        (ROOT / logo_path).write_bytes(body)

    index = load("index.json", {"version": 1, "plugins": []})
    entry = next((p for p in index["plugins"] if p["id"] == pid), None)
    if entry is None:
        entry = new_entry(manifest, pid, args.repo, logo_path)
        index["plugins"].append(entry)
        index["plugins"].sort(key=lambda p: p["name"].lower())
        print(f"{pid} is new here: check its summary and tags before merging")
    else:
        # The permissions on the card have to be the ones the plugin actually asks for.
        entry["permissions"] = list(manifest.get("permissions", []))
        if manifest.get("network"):
            entry["network"] = list(manifest["network"])
        if logo_path:
            entry["logo"] = logo_path

    # Never move latest backwards: re-running this for an older tag would otherwise offer nodes a
    # downgrade and break the store's own consistency check.
    current = entry.get("latest") or {}
    if version_key(version) >= version_key(current.get("version", "")):
        entry["latest"] = release
    else:
        print(f"kept {current['version']} as latest: {version} is older")
    index["updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    save("index.json", index)

    print(f"{pid} {version} -> {sha256[:12]}… {size} bytes, {', '.join(arches)}")
    print("now run: python3 scripts/validate.py")


if __name__ == "__main__":
    main()
