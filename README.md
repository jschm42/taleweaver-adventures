<p align="center">
  <img src="https://raw.githubusercontent.com/jschm42/taleweaver/main/frontend/src/assets/svg/app-logo.svg" width="150" alt="TaleWeaver Logo">
  <br><em>Official Repository for TaleWeaver Adventure Blueprints.</em>
</p>

# TaleWeaver Adventures

Welcome to the official repository for **TaleWeaver** adventures! This collection contains curated adventure blueprints, settings, and storylines designed to be played with the [TaleWeaver Engine](https://github.com/jschm42/taleweaver).

TaleWeaver is a generative AI roleplaying engine that uses Large Language Models and cinematic Text-to-Speech to create immersive, dynamic narratives. This repository serves as a library for sharing and preserving these created worlds.

## 📂 Repository Layout

This repository stores **sources, not binaries**. Every adventure lives in its own
directory at the repository root, named after its canonical slug:

```
<slug>/                     one directory per adventure
  adventure.adv             blueprint (JSON) — required
  assets/*.jpg              scene images, portraits, cover art
  README.md                 short summary / teaser
scripts/pack_adventure.py   deterministic packer + unpacker
catalog.json                machine-readable index (download URLs point at release assets)
Concepts/                   ideas and world-building notes that are not yet full blueprints
```

Example: `the-echo-archive-riddle-test/adventure.adv` plus its artwork in
`the-echo-archive-riddle-test/assets/`.

The directory name is the adventure's **slug**, which is also the release tag prefix
(`adv/<slug>/v<version>`). Legacy asset filenames inside a source directory may differ
from the slug — the authoritative identity is `adventure.origin_id` inside
`adventure.adv`, mirrored as `id` in `catalog.json`.

### 🚫 Why there are no `.adz` files in git

`.adz` bundles are **build artifacts**, not sources. They are produced by
`scripts/pack_adventure.py` and published as **GitHub release assets** only — they are
never committed (see `.gitignore` / `.gitattributes`). This keeps the repository
reviewable: a change to an adventure shows up as a readable diff in `adventure.adv`,
not as an opaque binary blob.

### 📝 Documentation Convention
Every adventure directory carries a `README.md` with a short summary and description of
the adventure.

## 📦 Where do I download the bundles?

Ready-to-import `.adz` bundles — including all artwork — are attached to the
[**Releases**](https://github.com/jschm42/taleweaver-adventures/releases) page. Each
release is tagged `adv/<slug>/v<version>` and ships three assets:

| Asset | Purpose |
| --- | --- |
| `<slug>-<version>.adz` | the full bundle, ready to import into TaleWeaver |
| `catalog-entry.json` | the single catalog entry for that release |
| `cover.png` | cover image |

Direct download URL pattern:

```
https://github.com/jschm42/taleweaver-adventures/releases/download/adv/<slug>/v<version>/<slug>-<version>.adz
```

`catalog.json` in this repository already contains the resolved download URL and
SHA-256 for every adventure, so tooling can fetch bundles straight from the index.

Prefer building from source? See [Packing an Adventure](#-packing-an-adventure-adz) below.

## 🛠️ Adventure Formats

The adventures in this repository come in two primary formats:

*   **`.adv` (JSON):** The core blueprint of an adventure. It contains the world manifesto, scene definitions, NPC data, and RPG mechanics.
*   **`.adz` (ZIP):** An "Adventure Zip" package. This contains the same `.adv` blueprint but also bundles high-quality assets like scene images and character portraits.

## 📦 Packing an Adventure (`.adz`)

`.adz` bundles are **built, not committed**. `scripts/pack_adventure.py` packs a source
directory into a byte-reproducible ZIP archive, so the same sources always yield the same
SHA-256 — across machines, working directories and fresh clones.

A source directory contains the blueprint at its root plus an optional `assets/` tree:

```
my-adventure/
  adventure.adv          # required
  assets/                # optional: scene images, portraits, ...
    cover.png
```

Build and inspect a bundle (Python 3.10+, standard library only — no system `zip` needed):

```bash
# pack: source directory -> archive
python3 scripts/pack_adventure.py pack my-adventure -o dist/my-adventure-3.0.0.adz

# unpack: archive -> source directory (useful to bootstrap sources from an existing bundle)
python3 scripts/pack_adventure.py unpack dist/my-adventure-3.0.0.adz -o my-adventure

# reproducibility check: build twice and compare
python3 scripts/pack_adventure.py pack my-adventure -o /tmp/a.adz
python3 scripts/pack_adventure.py pack my-adventure -o /tmp/b.adz
sha256sum /tmp/a.adz /tmp/b.adz   # both lines must match
```

Guarantees of the packer, which is what makes the digest stable:

* entries are sorted lexicographically, `adventure.adv` always first;
* a fixed DOS timestamp `1980-01-01 00:00:00` for every entry — file mtimes and git
  metadata never leak into the archive;
* fixed `external_attr` (`0o644`) and `create_system = 0` for every entry, so the
  permissions of the source tree do not matter;
* `ZIP_DEFLATED` with a fixed compression level, no directory entries, no comments and
  no extra fields.

`pack` exits with code `1` and a clear message if `adventure.adv` is missing; `unpack`
refuses archives whose paths would escape the target directory.

> Note: the compressed bytes depend on the zlib implementation linked into your CPython
> build. Reproducibility holds for a pinned interpreter (e.g. CI); switching the zlib
> build is a deliberate version bump, not a bug.

## 🚀 How to Use

You can import any adventure from this repository into your local TaleWeaver instance:

### Option 1: Manual Import (Drop-in)
1.  Download the `.adv` or `.adz` file of your choice.
2.  Place the file in your TaleWeaver directory at `data/imports/adventures/`.
3.  Restart the backend or wait for the auto-import cycle. The file will be automatically processed and added to your **Library**.

### Option 2: UI Import
1.  Navigate to the **Library** in the TaleWeaver web interface.
2.  Click the **Import Adventure** button.
3.  Upload the `.adv` or `.adz` file.

## 🎨 Credits & Image Generation

The visual immersion in these adventures is made possible by state-of-the-art AI generation.

*   **Images:** Most scene and character illustrations in the packaged `.adz` files are generated using **FLUX.1 [schnell]** and **FLUX.2 [klein]** models by **[Black Forest Labs](https://blackforestlabs.ai/)**.
*   **Engine:** All adventure logic and narrative structure are generated by the **TaleWeaver** omniscient AI Gamemaster.

## ⚖️ License & AI Content

*   **Repository License:** All files in this repository (adventure blueprints, packaged adventures, concepts, documentation, and assets) are licensed under the [Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0)](LICENSE). You are free to share and adapt the material for non-commercial purposes, provided that appropriate credit is given. See the [LICENSE](LICENSE) file for the full legal text.
*   **AI-Generated Content:** Content produced by AI models (text and images) is subject to the terms of the respective providers. Images generated with Black Forest Labs' FLUX models are typically available for use under their specific open or commercial licenses, but the surrounding adventure material in this repository remains under CC BY-NC 4.0.

---
*Weave your own stories. Explore the infinite.*