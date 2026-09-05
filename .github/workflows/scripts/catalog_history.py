"""Release-derived discovery dates. No publishing side effects in this module.

A ledger retains removed tables; reintroductions keep their original arrival.
Fingerprints use install/config content, not release URLs or re-zipped bytes.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen
from datetime import datetime, timezone

DATE_FIELDS = ('firstAvailableAt', 'firstAvailableRelease', 'updatedAt', 'updatedRelease')
CONTENT_FIELDS = ('configVersion', 'tableChecksum', 'tableVersion', 'backglassChecksum',
                  'backglassVersion', 'romChecksum', 'romVersion', 'pupChecksum', 'pupVersion',
                  'altSoundChecksum', 'altSoundVersion', 'coloredROMChecksum', 'coloredROMVersion',
                  'diffChecksum', 'diffVersion', 'specialDMDChecksum', 'specialDMDVersion')

def fingerprint(entry):
    content = {k: entry.get(k) for k in CONTENT_FIELDS if entry.get(k) is not None}
    for key, value in content.items():
        if key.endswith('Checksum') and isinstance(value, list):
            content[key] = sorted(value)
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)

def table_renames(previous, current, cwd='.', ref_prefix='refs/tags/', previous_ref=None):
    if not previous or (previous == current and previous_ref is None):
        return {}
    # The release runner is blobless: restrict rename detection to YAML so
    # it never fetches old launcher artwork or other large binary blobs.
    result = subprocess.run(['git', 'diff', '--name-status', '-M',
                             previous_ref or f'{ref_prefix}{previous}', f'{ref_prefix}{current}', '--', 'external/*/table.yml'],
                            cwd=cwd, text=True, capture_output=True, check=True)
    aliases = {}
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[0].startswith('R'):
            old, new = (Path(p) for p in parts[1:])
            if old.name == new.name == 'table.yml' and old.parent != new.parent:
                aliases[new.parent.name] = old.parent.name
    return aliases

def advance(history, manifest, tag, published_at, aliases=None):
    """Apply a full published manifest, preserving immutable arrival dates."""
    out = copy.deepcopy(history or {'schemaVersion': 1, 'tables': {}})
    if out.get('publishedAt') and utc(published_at) < utc(out['publishedAt']):
        raise ValueError('Cannot apply an older release to a newer history ledger')
    ledger = out.setdefault('tables', {})
    for new, old in (aliases or {}).items():
        if new not in ledger and old in ledger:
            ledger[new] = copy.deepcopy(ledger[old])
    for key, entry in manifest.items():
        if not isinstance(entry, dict) or entry.get('enabled') is False:
            continue
        digest = fingerprint(entry)
        previous = ledger.get(key)
        if previous is None:
            previous = {'firstAvailableAt': published_at, 'firstAvailableRelease': tag,
                        'updatedAt': published_at, 'updatedRelease': tag}
        elif previous.get('fingerprint') != digest:
            previous['updatedAt'] = published_at
            previous['updatedRelease'] = tag
        previous['fingerprint'] = digest
        ledger[key] = previous
    out.update(schemaVersion=1, latestRelease=tag, publishedAt=published_at)
    return out

def stamp(manifest, history):
    """Enrich a manifest; never add ledger-only (removed) entries to it."""
    for key, entry in manifest.items():
        record = history.get('tables', {}).get(key)
        if record:
            for field in DATE_FIELDS:
                entry[field] = record[field]
    return manifest

def download_json(url):
    with urlopen(url, timeout=60) as response:
        return json.load(response)

def backfill(repo, releases=None, cwd='.', history=None, ref_prefix='refs/tags/'):
    if releases is None:
        # gh handles the developer's existing auth; no credential is printed or
        # persisted in the ledger. This operation is read-only.
        result = subprocess.run(['gh', 'api', '--paginate', '--slurp',
                                 f'repos/{repo}/releases'], capture_output=True, text=True, check=True)
        releases = [r for page in json.loads(result.stdout) for r in page]
    releases = sorted((r for r in releases if not r.get('draft') and not r.get('prerelease')),
                      key=lambda r: utc(r['published_at']))
    for release in releases:
        assets = [a for a in release['assets'] if a['name'] == 'manifest.json']
        if not assets:
            # A missing historical asset could conceal an earlier arrival.
            # Fail rather than quietly assert a later first-availability date.
            raise ValueError(f"Release {release['tag_name']} has no manifest")
        manifest = download_json(assets[0]['browser_download_url'])
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError(f"Invalid manifest for {release['tag_name']}")
        aliases = table_renames(history and history['latestRelease'], release['tag_name'], cwd, ref_prefix)
        history = advance(history, manifest, release['tag_name'], release['published_at'], aliases)
        print(f"{release['tag_name']}: {len(manifest)} tables")
    return history

def release_record(release, assets):
    return {
        'tag_name': release.tag_name,
        'published_at': release.published_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'assets': [{'name': a.name, 'browser_download_url': a.browser_download_url}
                   for a in assets],
    }

def source_history(source, published_at):
    """Seed forks from the upstream catalog, not unrelated fork test releases.

    Forks can reuse upstream tag names for different commits, so fetch upstream
    tags into a separate namespace for accurate table-rename detection.
    """
    ref_prefix = 'refs/catalog-history/source/'
    subprocess.run(['git', 'fetch', '--no-tags', '--filter=blob:none', source.clone_url,
                    f'+refs/tags/*:{ref_prefix}*'], check=True)
    releases = [release_record(r, r.get_assets()) for r in source.get_releases()
                if not r.draft and not r.prerelease and r.published_at <= published_at]
    history = backfill(source.full_name, releases=releases, ref_prefix=ref_prefix)
    if not history:
        raise ValueError(f'No published catalog history in {source.full_name}')
    return history, f"{ref_prefix}{history['latestRelease']}"

def release_history(repo, release, tables):
    """Load release assets only, bootstrapping missing history from manifests.

    Never read a working-tree ledger. On the first release after rollout,
    reconstruct prior published manifests. Later runs resume from the newest
    ledger and replay any intervening releases published without one.
    """
    history = None
    pending = []
    previous_ref = None
    candidates = sorted((r for r in repo.get_releases() if not r.draft and not r.prerelease
                         and r.published_at <= release.published_at),
                        key=lambda r: r.published_at, reverse=True)
    for previous in candidates:
        assets = list(previous.get_assets())
        asset = next((a for a in assets if a.name == 'table-history.json'), None)
        if asset:
            history = download_json(asset.browser_download_url)
            if (history.get('schemaVersion') != 1
                    or history.get('latestRelease') != previous.tag_name
                    or not isinstance(history.get('tables'), dict)):
                raise ValueError(f'Invalid history asset for {previous.tag_name}')
            break
        # The current manifest may not exist yet (or may be an old rerun).
        # Its complete replacement is supplied in tables below.
        if previous.tag_name != release.tag_name:
            pending.append(release_record(previous, assets))
    if history is None and getattr(repo, 'fork', False) and repo.source:
        history, previous_ref = source_history(repo.source, release.published_at)
    else:
        history = backfill(repo.full_name, releases=pending, history=history)
    aliases = table_renames(history and history.get('latestRelease'), release.tag_name,
                            previous_ref=previous_ref)
    date = release.published_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return advance(history, tables, release.tag_name, date, aliases)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill table discovery metadata from published manifests (read-only).')
    parser.add_argument('--repo', default='LegendsUnchained/vpx-standalone-alp4k')
    parser.add_argument('--output', required=True,
                        help='Output asset path, e.g. /tmp/table-history.json (not source data).')
    args = parser.parse_args()
    history = backfill(args.repo)
    Path(args.output).write_text(json.dumps(history, indent=2, sort_keys=True) + '\n')
