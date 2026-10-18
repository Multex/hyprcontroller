#!/usr/bin/env python3
"""Save and verify the desktop configuration before installing hyprcontroller."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone

HOME = Path.home()
STATE = HOME / '.local/state/hyprcontroller'
SOURCES = [HOME / '.config' / name for name in
           ('hypr', 'quickshell', 'illogical-impulse', 'sunshine', 'hyprcontroller', 'systemd/user')]
SOURCES += [Path('/etc/udev/rules.d'), Path('/etc/systemd/user')]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def describe(path):
    s = path.lstat()
    item = {'mode': stat.S_IMODE(s.st_mode), 'mtime_ns': s.st_mtime_ns,
            'uid': s.st_uid, 'gid': s.st_gid}
    if path.is_symlink():
        item.update(kind='symlink', target=os.readlink(path))
    elif path.is_dir():
        item.update(kind='directory')
    elif path.is_file():
        item.update(kind='file', sha256=digest(path))
    else:
        return None
    return item


def inventory(roots):
    entries = {}
    pending = list(roots)
    seen = set()
    while pending:
        p = pending.pop()
        if str(p) in seen or not os.path.lexists(p):
            continue
        seen.add(str(p))
        item = describe(p)
        if item is None:
            continue
        entries[str(p).lstrip('/')] = item
        if p.is_symlink():
            target = p.resolve()
            if target.exists():
                pending.append(target)
        elif p.is_dir():
            pending.extend(p.iterdir())
    return entries


def verify(archive, manifest):
    """Extract files first, links last, without traversing archived symlinks."""
    with tempfile.TemporaryDirectory(prefix='hyprcontroller-backup-check-') as tmp:
        scratch = Path(tmp)
        with tarfile.open(archive, 'r:gz') as tf:
            for member in tf.getmembers():
                dest = scratch / member.name
                item = manifest['entries'].get(member.name)
                if item is None or member.mode != item['mode'] or member.uid != item['uid'] or member.gid != item['gid'] or abs(member.mtime - item['mtime_ns'] / 1e9) > 0.000001:
                    raise RuntimeError(f'Archived metadata differs: {member.name}')
                if member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise RuntimeError('Unsafe archive path')
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(member) as source, dest.open('wb') as out:
                        shutil.copyfileobj(source, out)
                    dest.chmod(member.mode)
                elif member.islnk():
                    raise RuntimeError('Unexpected hard link')
            for member in tf.getmembers():
                if member.issym():
                    dest = scratch / member.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.symlink_to(member.linkname)
            for name, item in manifest['entries'].items():
                p = scratch / name
                if item['kind'] == 'directory':
                    p.chmod(item['mode'])
                os.utime(p, ns=(item['mtime_ns'], item['mtime_ns']), follow_symlinks=False)
                check = describe(p)
                fields = ('kind', 'mode', 'mtime_ns', 'sha256', 'target')
                if any(check.get(k) != item.get(k) for k in fields):
                    raise RuntimeError(f'Backup verification failed: {name}')


def command_output(command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f'Inventory unavailable: {exc}\n'
    if result.returncode:
        return f'Inventory command failed ({result.returncode}): ' + (result.stderr or result.stdout) + '\n'
    return result.stdout


def package_inventory():
    for command in (['pacman', '-Q'], ['dpkg-query', '-W'], ['rpm', '-qa']):
        if shutil.which(command[0]):
            return '# Package inventory: ' + ' '.join(command) + '\n' + command_output(command)
    return 'Package inventory unavailable: no supported package manager found (pacman, dpkg-query, rpm).\n'


def create():
    old_umask = os.umask(0o077)
    try:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        folder = STATE / 'backups' / stamp
        folder.mkdir(parents=True, mode=0o700)
        manifest = {'created_utc': stamp, 'home': str(HOME),
                    'sources': [str(p) for p in SOURCES],
                    'entries': inventory(SOURCES), 'verified': False}
        (folder / 'packages.txt').write_text(package_inventory())
        (folder / 'identity.txt').write_text(command_output(['id']))
        (folder / 'user-services.txt').write_text(command_output(
            ['systemctl', '--user', 'list-unit-files', '--no-pager']))
        archive = folder / 'desktop.tar.gz'
        with tarfile.open(archive, 'w:gz', dereference=False) as tf:
            # Disable inode reuse so every regular file is independently verifiable.
            for name in sorted(manifest['entries']):
                tf.inodes.clear()
                tf.add('/' + name, arcname=name, recursive=False)
        verify(archive, manifest)
        manifest['archive_sha256'] = digest(archive)
        manifest['verified'] = True
        (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (folder / 'RESTORE.txt').write_text(
            'Stop hyprcontroller before restoring. Use the project rollback command to remove its installed files.\n'
            'This archive contains private Sunshine credentials. Keep it local and out of version control.\n'
            'Configuration paths are stored without the leading slash. Restore only files changed by the project.\n'
            'Do not extract the entire archive over a running desktop. Later edits must be checked before replacement.\n'
            'The manifest records original ownership, permissions, symlinks and file checksums.\n'
            'Run: python scripts/backup.py --verify <backup-directory>\n')
        print(json.dumps({'backup': str(folder), 'verified': True,
                          'entries': len(manifest['entries']),
                          'bytes': archive.stat().st_size}))
    finally:
        os.umask(old_umask)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if args.verify:
        manifest = json.loads((args.verify / 'manifest.json').read_text())
        archive = args.verify / 'desktop.tar.gz'
        if digest(archive) != manifest['archive_sha256']:
            raise SystemExit('Archive checksum does not match')
        verify(archive, manifest)
        print('Backup verified')
    else:
        create()
