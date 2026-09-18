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
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The architectures a bundle can carry, by the suffix its binaries use.
ARCHES = {"amd64": "linux/amd64", "arm64": "linux/arm64", "arm": "linux/arm"}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def release_asset(repo, tag):
    """The one .zip on the release, with its download URL."""
    data = json.loads(run("gh", "api", f"repos/{repo}/releases/tags/{tag}"))
    zips = [a for a in data["assets"] if a["name"].endswith(".zip")]
    if len(zips) != 1:
        sys.exit(f"{repo} {tag} has {len(zips)} zip assets; expected exactly one")
    return zips[0], data.get("published_at")


def read_bundle(path):
    """The manifest and the architectures the bundle actually carries."""
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
    if not arches:
        sys.exit("no linux binaries named ...-linux-<arch> in the bundle")
    return manifest, arches


def download(url, dest):
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=300) as response, open(dest, "wb") as f:
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
    return [int(p) for p in v.split("-")[0].split(".")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="owner/name of the plugin's GitHub repo")
    parser.add_argument("tag", help="the release tag, e.g. v0.1.2")
    parser.add_argument("--notes", default="", help="one line for the update card")
    parser.add_argument("--min-host", default="", help="oldest RepeaterTastic that can run it (kept from the last release otherwise)")
    args = parser.parse_args()

    asset, published = release_asset(args.repo, args.tag)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = pathlib.Path(tmp) / asset["name"]
        print(f"downloading {asset['name']}")
        sha256, size = download(asset["url"], bundle)
        manifest, arches = read_bundle(bundle)

    pid = manifest["id"]
    version = str(manifest["version"])
    if version != args.tag.lstrip("v"):
        sys.exit(f"the bundle says version {version} but the tag is {args.tag}")

    release = {
        "version": version,
        "api": int(manifest.get("api", 1)),
        "released": published,
        "url": f"https://github.com/{args.repo}/releases/download/{args.tag}/{asset['name']}",
        "sha256": sha256,
        "size": size,
        "arches": arches,
    }
    if manifest.get("min_host"):
        release["min_host"] = str(manifest["min_host"])
    if args.notes:
        release["notes"] = args.notes

    detail = load(f"plugins/{pid}.json", {"id": pid, "releases": []})
    # min_host isn't in the manifest: it is a judgement about which hosts can run this build, so
    # it carries over from the last release unless --min-host says otherwise.
    if args.min_host:
        release["min_host"] = args.min_host
    elif "min_host" not in release and detail["releases"]:
        previous = max(detail["releases"], key=lambda r: version_key(r["version"]))
        if previous.get("min_host"):
            release["min_host"] = previous["min_host"]
    # Keep the schema's field order, so the diff on an update is only the values.
    order = ["version", "api", "min_host", "released", "url", "sha256", "size", "arches", "notes"]
    release = {k: release[k] for k in order if k in release}

    detail["releases"] = [r for r in detail["releases"] if r["version"] != version] + [release]
    detail["releases"].sort(key=lambda r: version_key(r["version"]))
    save(f"plugins/{pid}.json", detail)

    index = load("index.json", {"version": 1, "plugins": []})
    entry = next((p for p in index["plugins"] if p["id"] == pid), None)
    if entry is None:
        # A plugin nobody has listed yet: fill in what the manifest knows and leave the editorial
        # fields for the pull request to improve.
        entry = {
            "id": pid,
            "name": manifest.get("name", pid),
            "summary": (manifest.get("description") or "").split("\n")[0][:140] or "TODO: one line for the card",
            "description": manifest.get("description", ""),
            "author": manifest.get("author", ""),
            "homepage": manifest.get("homepage", f"https://github.com/{args.repo}"),
            "license": manifest.get("license", ""),
            "logo": f"logos/{pid}.png",
            "permissions": list(manifest.get("permissions", [])),
            "network": list(manifest.get("network", [])),
        }
        index["plugins"].append(entry)
        index["plugins"].sort(key=lambda p: p["name"].lower())
        print(f"{pid} is new here: check its summary, logo and tags before merging")
    else:
        # The permissions on the card have to be the ones the plugin actually asks for.
        entry["permissions"] = list(manifest.get("permissions", []))
        if manifest.get("network"):
            entry["network"] = list(manifest["network"])

    entry["latest"] = release
    index["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save("index.json", index)

    print(f"{pid} {version} -> {sha256[:12]}… {size} bytes, {', '.join(arches)}")
    print("now run: python3 scripts/validate.py")


if __name__ == "__main__":
    main()
