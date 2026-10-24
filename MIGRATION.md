# Migration: committed `.adz` blobs → source directories

Audit trail for the switch from committed binary bundles to a source layout with
release-only `.adz` artefacts. It exists so the byte change is provable instead of
asserted in a chat thread.

## Anchor

- `pre_migration_commit`: `ea1169cff05a8adec424ff2950e5f38d563831e8` — last commit on `main` that still contained
  `ADZ/*.adz`. Every legacy bundle is recoverable:
  `git show ea1169cff05a8adec424ff2950e5f38d563831e8:ADZ/<Name>.adz > /tmp/orig.adz`.
- Migration date: 2026-10-24
- Packer: `scripts/pack_adventure.py pack` (Python standard library only, fixed DOS
  timestamp `1980-01-01 00:00:00`, `external_attr = 0o644 << 16`, sorted entries).
- No history rewrite happened and none is planned: the legacy bytes stay reachable in
  git, which is what keeps this table verifiable.

## Why the container hash changes

The legacy bundles were written with real file mtimes and umask-dependent permissions;
they are not reproducible. The new bundles are. `old_sha256 != new_sha256` is therefore
expected and not a defect.

What has to be identical is the **content**. The tree hash below is `sha256` over the
sorted lines `<relpath>\0<sha256(file)>\n` of the archive members. It ignores
timestamps, permissions, compression method and member order, so it is the honest
comparison between a legacy bundle and its rebuild.

## Bundle inventory

| slug | legacy file (release asset) | members | legacy `.adz` sha256 | legacy bytes | rebuilt sha256 | rebuilt bytes | content tree hash | tree == |
| --- | --- | ---: | --- | ---: | --- | ---: | --- | :---:
| `crashed-on-proxima-9` | `Crashed_on_Proxima-9.adz` | 86 | `579c876bdc708096a7233c656abdba93ba1c06f565e9ccf1449f647ab7741b0c` | 23,654,106 | `4742056151484b4c84caee948eab93f1e17c3a808b3bdb04216d4824cc93941e` | 23,653,811 | `88ef19e4a465f94ade308728902283e2b0e47b5005ab8d9d9ef15adaacb779d3` | yes |
| `crazy-job-interview` | `Crazy_Job_Interview.adz` | 38 | `5399952cc118b5ae156ef0c7574a5b3d1b319a05151c61514b3c39f88c6c869e` | 9,123,404 | `f8ee1c7f986b2bd5bcb93fea2365ec4047bc93ec767b840d401ba1412732af3d` | 9,123,282 | `b25ab87ca31a37718e2b356ab087d753b55737e39949f35b4c3388586c67f75b` | yes |
| `el-profesor-s-purgatory` | `El_Profesors_Purgatory.adz` | 40 | `e9370a408376ddf5a82fdaeb807d62d2cd5aa0e616a82325ef32bc1e838cea15` | 9,459,876 | `0e14365f7d9e6e29678becc290bf88e775ba3228fbe60c3541e6a6f4c12feb5e` | 9,459,744 | `16a0bd4d3074f0f4a34091b0c672432e0a18c537445657b743a2ae5c1cfa6e50` | yes |
| `kitchen-crisis` | `Kitchen_Crisis.adz` | 40 | `0386532e87c358b3d3bcb6e45564ae712a0a3eaadbd4591893544ca3a4229c98` | 8,413,245 | `54ab18feafa9709472a64aafa8a4005b928432aca8dd88f860bb362efffe3aa9` | 8,413,062 | `a5d78829e68c131db4ab34681ba1a2fa7bf05d71d69df214e85376fb5bc3cdeb` | yes |
| `professor-quark-s-quantum-quest` | `Professor_Quarks_Quantum_Quest.adz` | 56 | `c92aa0e28655410ff7025b2fd348ea35d84621133ec36a857f520d5f31a29b74` | 7,531,877 | `c6dbfbe5030d4279bf9ad429520b98bd0a4d01da413513e66f08a43e7d669f31` | 7,531,679 | `30d3837b20d84dd697858d7ec8e462ea99ba9e17f541739554bcf6441746fe12` | yes |
| `sledge-and-order` | `Sledge_and_Order.adz` | 47 | `6917b53d49f20a72de6b0824d669653c5ce4aaaf1aeba8ac766efdee23b01446` | 9,218,135 | `a48e3f3dfa567050ba67c567236495a15ff741fbbb5d1e1dfa4a1a9351bf8c8d` | 9,217,951 | `6129e625a7a87579b489ad5be664fe39ee75b8c8568e23e7c2b5d7f39fdf99d9` | yes |
| `the-alchemist-s-folly` | `The_Alchemists_Folly.adz` | 45 | `38519e5c73b87da485a796cce06b9531fe87396c81e4a40bdd71689b8a2b77f5` | 8,144,758 | `4b8d4595a476739f3a3083847bc96602f4b22026830b4e96000241c4ec0b868e` | 8,144,451 | `97a383f42fd67f28da115767501930c80d0b8cdc3404a1b66ed4f5844004d2d3` | yes |
| `the-echo-archive-riddle-test` | `The_Echo_Archive_Riddle-Test.adz` | 44 | `c61e375eebf6716719427daa4da56d56aa1604225fd58b82d17c49976e374701` | 11,222,049 | `2c81fc86b7773969b38968cb8025e27842c2d335f9890a740aca014c70dcda3b` | 11,221,832 | `eccbb2defbf8c1b49353fbb089f8e5c5369cc431a5c8c61a5e7ecb691c921473` | yes |
| `the-master-s-workshop` | `The_Masters_Workshop.adz` | 30 | `d94f9807f79af4d0da1c401d9997de94203d37a8de83078ff4091e2e1d4c7ccc` | 10,987,554 | `d2284cd39245abc74bba43eecd4d460c5d23ec8cd0864ad9b9d263837d9e94df` | 10,987,441 | `7d36510e7c5abf1f4bfea91ebb09959cb8ac58f18264dc8e01860fd77a82f091` | yes |
| `the-observer-s-labyrinth` | `The_Observers_Labyrinth.adz` | 41 | `ddc7d76b0a376b2bc846d3858c68b8c50d6a59266cfdad6576ffc263f981a4ec` | 9,109,550 | `6bd890d84a0649ae716c98c0895278dc6d562ab3ecccba4f88aec6417bce7fc2` | 9,109,324 | `e346764ff01ee51c0788d9c43652c67ff8ee6c4b2bc1bbe2a8527912820b86dc` | yes |

All ten rebuilds are content-identical to their legacy release asset, and two
independent packs from different working directories produce the same digest.
The legacy digests are the ones GitHub reports for the release assets, so the
`old_sha256` column can be re-checked with `gh release view <tag> --json assets`.

## Slugs

Slugs come from the existing release tags (`adv/<slug>/v<version>`), not from the
legacy file names, because the slug is the identity player saves are keyed on.
**Slugs are immutable from here on**; renaming one silently orphans saves, so a rename
would need an alias table in the client first.

| slug | directory | blueprint title | version |
| --- | --- | --- | --- |
| `crashed-on-proxima-9` | `crashed-on-proxima-9/` | Crashed on Proxima-9 | 3.0.0 |
| `crazy-job-interview` | `crazy-job-interview/` | Crazy Job Interview | 3.0.0 |
| `el-profesor-s-purgatory` | `el-profesor-s-purgatory/` | El Profesor's Purgatory | 3.0.0 |
| `kitchen-crisis` | `kitchen-crisis/` | Kitchen Crisis | 3.0.0 |
| `professor-quark-s-quantum-quest` | `professor-quark-s-quantum-quest/` | Professor Quark's Quantum Quest | 3.0.0 |
| `sledge-and-order` | `sledge-and-order/` | Sledge and Order | 3.0.1 |
| `the-alchemist-s-folly` | `the-alchemist-s-folly/` | The Alchemist's Folly | 3.0.0 |
| `the-echo-archive-riddle-test` | `the-echo-archive-riddle-test/` | The Echo Archive (Riddle-Test) | 3.0.0 |
| `the-master-s-workshop` | `the-master-s-workshop/` | The Master's Workshop | 3.0.0 |
| `the-observer-s-labyrinth` | `the-observer-s-labyrinth/` | The Observer's Labyrinth | 3.0.0 |

## Legacy bundles that were *not* migrated

`ADZ/` held two bundles that were never released under their own tag and are older
than the 3.0 line. They are superseded, not renamed — migrating them would have put a
second, divergent copy of an adventure into the tree:

| legacy file | title | version | members | why it is not a source directory |
| --- | --- | --- | ---: | --- |
| `ADZ/El_Protagonista_Accidental.adz` | El Protagonista Accidental | 1.9.1 | 36 | pre-3.0 prototype; the 3.0 line released it as `el-profesor-s-purgatory` |
| `ADZ/The_Masters_Workshop_Item_Stress_Test.adz` | The Master's Workshop (Item Stress Test) | 1.9.0 | 28 | pre-3.0 QA variant of `the-master-s-workshop` |

Both remain byte-exactly recoverable from the `pre_migration_commit`, and their
descriptions were preserved under `Concepts/`. Note that the *committed* `ADZ/` copies
of the other eight adventures were stale `1.9.x` builds too; the migration took its
sources from the **release assets**, which are the 3.0 line, not from `ADZ/`.

## Covers

Cover artwork is a release asset only (`cover.png`). The migration does not touch cover
bytes, does not normalise them and introduces no re-encoder, so there is no encoder
version to pin. State of the ten currently published covers:

| slug | `cover.png` sha256 | bytes |
| --- | --- | ---: |
| `crashed-on-proxima-9` | `dbe086702cb26705d9af1d1487623af689ad814515fd869fbf3f6debe6011fa3` | 2,957,753 |
| `crazy-job-interview` | `e1c4e4af1bc7091af8a6d201499a1964be556b0328542c945d4207e507ad4c06` | 2,229,825 |
| `el-profesor-s-purgatory` | `539fe6c66b3c71e2235849646ab1914502003a5b43f93e42dde5e8498feef3e1` | 2,273,053 |
| `kitchen-crisis` | `8ce95ae4a9a96cda56e55cc92edf1c8a2e89d192a9fe8456ff5acf87f2ab6f4e` | 2,145,538 |
| `professor-quark-s-quantum-quest` | `928cac382e4ceb0023780d838100fb5655049df5a98eeadc731b2c496802b952` | 665,309 |
| `sledge-and-order` | `79facf57c30bb2f0ff6a6387647b7c5c68514014f0d42f620e296293f7207076` | 2,251,167 |
| `the-alchemist-s-folly` | `2cf382156233ac9f864dda75fea12eb45ed67b1424f88543e6bb4db9e51022db` | 2,400,970 |
| `the-echo-archive-riddle-test` | `583a3fff6461b6f2328f67fea322cbec5c8c0dd758a5ea17a0aa232afd5045d2` | 2,278,999 |
| `the-master-s-workshop` | `dc0c12464df7f1bfe752b4ee8b7767260953d362deb069650d0361e49594efb6` | 2,858,802 |
| `the-observer-s-labyrinth` | `e2a213fa294165bfd6f18ff426ca5ae334c93256b0a3c6db271f92c7a8e5072c` | 2,205,423 |

Any later WebP normalisation is a separate change with its own pinned encoder version
and its own row here.

## Reproducing this table

```bash
git show ea1169cff05a8adec424ff2950e5f38d563831e8:ADZ/Kitchen_Crisis.adz > /tmp/legacy.adz      # any legacy bundle
python3 scripts/pack_adventure.py unpack /tmp/legacy.adz -o /tmp/legacy
python3 scripts/pack_adventure.py pack kitchen-crisis -o /tmp/new.adz
sha256sum /tmp/legacy.adz /tmp/new.adz   # differ: timestamps
# tree hash: sha256 over sorted '<relpath>\0<sha256(file)>\n' of the members
```
