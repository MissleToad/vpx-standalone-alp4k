# Table discovery history

The release workflow publishes `table-history.json` alongside `manifest.json`.
The manifest includes `firstAvailableAt`, `firstAvailableRelease`, `updatedAt`,
and `updatedRelease` for each table. Generated history is never committed.

On the first run, the generator reconstructs history from published manifests.
Forks start from their source repository's published catalog, so experimental
fork releases do not replace official arrival dates. Upstream tags are fetched
into `refs/catalog-history/source/` to avoid collisions with fork tag names.
After the first run, each repository continues from its own released history
asset, replaying any intervening manifests. Reruns preserve dates; rebuilding
an older release excludes newer history. Missing historical manifests fail the
build instead of inventing arrival dates.

Removed tables remain in history. Reintroductions and detected folder renames
preserve first arrival. Changes to config commits or component versions and
checksums advance the update date; release URLs and regenerated ZIP bytes do not.

For a read-only backfill preview:

```sh
python .github/workflows/scripts/catalog_history.py --output /tmp/table-history.json
python .github/workflows/scripts/generate-manifest.py vpx-mm \
  --history /tmp/table-history.json
```

Publish a release to trigger the workflow, or manually dispatch it with the
existing published release's tag to rebuild its assets. Draft releases are not
supported: the discovery timestamps represent publication dates.

Run history tests with:

```sh
python -m unittest discover -s .github/workflows/scripts -p test_catalog_history.py
```
