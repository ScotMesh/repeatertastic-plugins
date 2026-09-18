# RepeaterTastic plugins

The list of plugins RepeaterTastic offers under **Plugins → Browse store**. It is
a few JSON files and some logos — no server, no accounts, no code. Your node
fetches `index.json` from here, shows you what is available, and downloads the
bundle straight from the plugin's own GitHub release.

Default address, which every node uses unless you point it somewhere else:

```
https://raw.githubusercontent.com/ScotMesh/repeatertastic-plugins/main/index.json
```

Run your own store by forking this repo and setting `plugins.store_url` in
`repeatertastic.yaml` to your copy. Nothing here is privileged; a node will read
any store you tell it to.

## What your node does with it

1. Fetches `index.json` (cached, and re-fetched with `If-None-Match`, so a node
   that checks hourly costs GitHub one 304 an hour).
2. Draws a card per plugin from the index: name, summary, logo, the permissions
   it will ask for, and what it talks to over the network.
3. On install, downloads `latest.url`, **checks the sha256 against the index**
   and refuses the bundle if it disagrees, then unpacks it under the node's
   plugin directory and starts it exactly like a hand-installed plugin.
4. Compares `latest.version` with what is installed, and offers the update.

The store never runs anything. It is a list of addresses and checksums; the node
does the downloading, the checking and the running.

## Adding a plugin

Open a pull request with:

- `plugins/<id>.json` — every release you want on record.
- `logos/<id>.png` (or `.svg`, or `.webp`) — square, 256×256 or larger, transparent
  background. **Add a release** takes this out of the bundle for you.
- an entry in `index.json` whose `latest` block is copied from the newest
  release in your `plugins/<id>.json`.

`<id>` must match the `id` in your bundle's `plugin.yaml`, because that is what
the node installs and upgrades by.

CI checks the schema — for `index.json` **and** every release in your plugin
file — that the logo exists, that `latest` agrees with your plugin file, and that
each download is a release of the repo in your `homepage`. When a pull request
touches a plugin it downloads every bundle and verifies the sha256 itself, so the
checksum in the store is never taken on trust.

Check it yourself before pushing:

```
pip install jsonschema ruff
ruff check scripts/                 # the scripts themselves
python scripts/validate.py          # everything
python scripts/validate.py --offline # no network
python scripts/validate.py --deep    # also download and hash every bundle
```

### Releasing a new version

Don't type a checksum. Run **Actions → Add a release** with the plugin's repo
and the tag, and it opens the pull request for you: it downloads the published
bundle, hashes it, and reads the version, API version, architectures and
permissions out of the `plugin.yaml` inside. The store then can't disagree with
what nodes will actually download.

Locally, the same thing:

```
pip install pyyaml jsonschema
python scripts/add_release.py ScotMesh/repeatertastic-meshflow v0.1.2 --notes "Faster uploads."
python scripts/validate.py
```

`min_host` isn't in the manifest — it's a judgement about which hosts can run a
build — so it carries over from the previous release unless you pass
`--min-host`.

A plugin repo can open its own store PR at the end of its release workflow:

```
gh workflow run add-release.yml -R ScotMesh/repeatertastic-plugins \
  -f repo=$GITHUB_REPOSITORY -f tag=$GITHUB_REF_NAME
```

That needs a token with `actions: write` on this repo, which is why the plugin
repo asks rather than committing here itself.

Old releases stay in `plugins/<id>.json` as a record: the checksum and download
address of every version, so one can be installed by hand or rolled back to.
Nodes themselves only ever install `latest` — a node too old for it is told so
rather than offered an older build.

## The fields

`schema/store-v1.json` is the authority. The ones worth explaining:

| Field | Why it is there |
| --- | --- |
| `summary` | One line, on the card. `description` is the long form on the detail panel. |
| `permissions` | Shown **before** install, so nobody grants blind. Must match what the plugin actually asks the host for. |
| `network` | Plain English, one line per thing it talks to. An empty list means it never leaves the node. |
| `latest.api` | The plugin API version the bundle speaks. A node that speaks an older API says so instead of installing. |
| `latest.min_host` | Oldest RepeaterTastic that can run it. |
| `latest.arches` | A Pi will not be offered an amd64-only bundle. |
| `latest.sha256` | Checked on download, and verified independently by CI. This is the only thing standing between a node and a swapped asset. |
| `homepage` | Must be `https:` — the node renders it as a link in its own web page — and a GitHub repo's downloads must come from that repo's releases. |
| `image` | For plugins that run in their own container. The node shows how to attach it rather than an Install button, because it cannot install into a container it does not own. |

## Docker

Managed plugins run inside the RepeaterTastic container — the daemon downloads
and supervises them, so store installs work in Docker with nothing mounted and
no access to the Docker socket. Keep the plugin directory on a volume
(`-v rt-data:/data`) and installs survive an image upgrade.

A plugin that genuinely needs its own container ships an `image` instead of a
bundle. Those are attached, not installed: you run the container yourself and
point it at the node's plugin socket. The store lists them so they are
discoverable, and the node shows the command rather than pretending it can do it
for you.

## Licence

The index and schema are CC0. Each plugin carries its own licence — see its
`license` field and its repo.
