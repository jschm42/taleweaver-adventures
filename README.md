<p align="center">
  <img src="https://raw.githubusercontent.com/jschm42/taleweaver/main/frontend/src/assets/svg/app-logo.svg" width="150" alt="TaleWeaver Logo">
  <br><em>Official Repository for TaleWeaver Adventure Blueprints.</em>
</p>

# TaleWeaver Adventures

Welcome to the official repository for **TaleWeaver** adventures! This collection contains curated adventure blueprints, settings, and storylines designed to be played with the [TaleWeaver Engine](https://github.com/jschm42/taleweaver).

TaleWeaver is a generative AI roleplaying engine that uses Large Language Models and cinematic Text-to-Speech to create immersive, dynamic narratives. This repository serves as a library for sharing and preserving these created worlds.

## 📂 Repository Structure

*   **`<slug>/`**: One directory per adventure — the single source of truth.
    ```text
    kitchen-crisis/
      adventure.adv     # required: the blueprint (JSON)
      assets/           # optional: scene images, portraits, cover
      README.md         # short summary of the adventure
    ```
    The directory name is the adventure's **slug** and must match its release tag
    (`adv/<slug>/v<version>`). Slugs are immutable: player saves are keyed on them.
*   **`Concepts/`**: Markdown notes and pre-blueprint world-building ideas.
*   **`scripts/pack_adventure.py`**: The deterministic packer (see below).
*   **`catalog.json`**: Machine-readable index of the published releases.
*   **`MIGRATION.md`**: Audit trail of the move from committed `.adz` blobs to this layout.

`.adz` bundles are **not** committed any more. They are build artefacts and live as assets
on the [releases page](https://github.com/jschm42/taleweaver-adventures/releases); `*.adz`
is in `.gitignore`.

### 📝 Documentation Convention

Every adventure directory carries a `README.md` describing the adventure. There is no
longer a separate `ADZ/` folder duplicating the directory names.

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
kitchen-crisis/
  adventure.adv          # required
  assets/                # optional: scene images, portraits, ...
    61762864-..._protagonist_ee1cbfe4.jpg
  README.md              # never packed
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

### Where do I get the `.adz` bundles?

From the [releases page](https://github.com/jschm42/taleweaver-adventures/releases) — one
release per adventure and version, tagged `adv/<slug>/v<version>`, with these assets:

| asset | purpose |
| --- | --- |
| `<slug>-<version>.adz` | the playable bundle |
| `catalog-entry.json` | catalogue metadata incl. `sha256` for verification |
| `cover.png` | cover artwork |

Releases are built by `.github/workflows/release-adventure.yml` on every
`adv/<slug>/<version>` tag; the workflow packs twice and refuses to publish if the two
digests differ. Every bundle can be rebuilt locally and compared against the release's
`sha256` — see the release notes of any adventure for the exact command.

You can import any adventure into your local TaleWeaver instance:

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