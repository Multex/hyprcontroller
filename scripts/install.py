#!/usr/bin/env python3
"""Install a manual-start prototype, with a checked removal manifest."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tarfile
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup import digest, verify, inventory

ROOT = Path(__file__).resolve().parent.parent


def application_sources():
    for folder in ('hyprcontroller', 'ui', 'docs'):
        for source in sorted((ROOT / folder).rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts:
                yield source
    for name in ('README.md', 'config.example.json', 'LICENSE'):
        yield ROOT / name


def file_record(path):
    return {'path': str(path), 'sha256': digest(path), 'mode': path.stat().st_mode & 0o777,
            'original': None, 'resolved_path': str(path.resolve())}


def stop_running(home):
    if home != Path.home():
        return
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'hyprcontroller/control.sock'
    if not runtime.exists():
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(4)
        try:
            conn.connect(str(runtime))
        except ConnectionRefusedError:
            return
        conn.sendall(b'{"command":"stop"}\n')
        conn.recv(8192)
    deadline = time.monotonic() + 5
    while runtime.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if runtime.exists():
        raise RuntimeError('hyprcontroller did not stop; no installed files removed')


def install(home, backup):
    backup_manifest = json.loads((backup / 'manifest.json').read_text())
    archive = backup / 'desktop.tar.gz'
    if not backup_manifest.get('verified') or digest(archive) != backup_manifest['archive_sha256']:
        raise RuntimeError('A verified desktop backup is required')
    verify(archive, backup_manifest)
    manifest_path = home / '.local/state/hyprcontroller/install.json'
    app = home / '.local/share/hyprcontroller-overlay'
    launcher = home / '.local/bin/hyprcontroller'
    config = home / '.config/hyprcontroller/overlay.json'
    if os.path.lexists(manifest_path) or os.path.lexists(app) or os.path.lexists(launcher):
        raise RuntimeError('An installation or launcher already exists. Review or roll it back first.')
    if config.exists():
        sys.path.insert(0, str(ROOT))
        from hyprcontroller.config import Config
        Config.load(config)
    files = []
    created_dirs = []

    def mkdir(path):
        missing = []
        p = path
        while not p.exists():
            missing.append(p)
            p = p.parent
        for p in reversed(missing):
            p.mkdir(mode=0o700)
            created_dirs.append(str(p))

    def write(path, content, mode=0o644):
        if os.path.lexists(path):
            raise RuntimeError(f'Refusing to replace an existing file: {path}')
        mkdir(path.parent)
        with path.open('xb') as target:
            target.write(content)
        path.chmod(mode)
        files.append(file_record(path))

    try:
        for source in application_sources():
            write(app / source.relative_to(ROOT), source.read_bytes())
        wrapper = ('#!/usr/bin/env python3\nimport sys\n'
                   f'sys.path.insert(0, {str(app)!r})\n'
                   'sys.dont_write_bytecode = True\nfrom hyprcontroller.cli import main\nraise SystemExit(main())\n')
        write(launcher, wrapper.encode(), 0o755)
        if not config.exists():
            write(config, (ROOT / 'config.example.json').read_bytes(), 0o600)
        mkdir(manifest_path.parent)
        manifest = {'version': 1, 'home': str(home), 'backup': str(backup.resolve()),
                    'files': files, 'created_directories': created_dirs,
                    'input_rules_added': [], 'startup_enabled': False,
                    'configuration_files_modified': [],
                    'generated_caches': [importlib.util.cache_from_source(r['path']) for r in files if r['path'].endswith('.py')]}
        with manifest_path.open('x') as out:
            json.dump(manifest, out, indent=2)
            out.write('\n')
        manifest_path.chmod(0o600)
        print(json.dumps({'launcher': str(launcher), 'manifest': str(manifest_path),
                          'automatic_startup': False, 'files_added': len(files)}))
    except BaseException:
        for record in reversed(files):
            Path(record['path']).unlink(missing_ok=True)
        for folder in reversed(created_dirs):
            try:
                Path(folder).rmdir()
            except OSError:
                pass
        raise


def rollback(home, dry_run=False, keep_modified=False):
    manifest_path = home / '.local/state/hyprcontroller/install.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('home') != str(home):
        raise RuntimeError('Installation manifest belongs to another home directory')
    modified = []
    removable = []
    for record in manifest['files']:
        path = Path(record['path'])
        if not path.is_relative_to(home) or path == home:
            raise RuntimeError('Invalid path in installation manifest')
        if not os.path.lexists(path):
            continue
        if str(path.resolve()) != record.get('resolved_path', str(path)) or path.is_symlink() or not path.is_file() or digest(path) != record['sha256'] or path.stat().st_mode & 0o777 != record['mode']:
            modified.append(str(path))
        else:
            removable.append(path)
    result = {'remove': [str(p) for p in removable], 'modified': modified,
              'restore': [], 'startup_enabled': False}
    if dry_run:
        print(json.dumps(result, indent=2))
        return result
    if modified and not keep_modified:
        raise RuntimeError('Later edits detected; nothing removed. Review --dry-run or use --keep-modified:\n' + '\n'.join(modified))
    stop_running(home)
    generated_caches = manifest.get('generated_caches', [importlib.util.cache_from_source(r['path']) for r in manifest['files'] if r['path'].endswith('.py')])
    for name in generated_caches:
        cache = Path(name)
        source_unchanged = any(str(cache) == importlib.util.cache_from_source(str(p)) for p in removable if p.suffix == '.py')
        if source_unchanged and cache.is_relative_to(home) and cache.is_file() and not cache.is_symlink():
            cache.unlink()
            try:
                cache.parent.rmdir()
            except OSError:
                pass
    for path in reversed(removable):
        path.unlink()
    for folder in reversed(manifest.get('created_directories', [])):
        path = Path(folder)
        if not path.is_relative_to(home):
            raise RuntimeError('Invalid directory in installation manifest')
        try:
            path.rmdir()
        except OSError:
            pass
    # Retain a record when edited files were deliberately kept.
    if modified:
        manifest['files'] = [record for record in manifest['files'] if record['path'] in modified]
        manifest['rolled_back_with_retained_files'] = True
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    else:
        manifest_path.unlink()
    print(json.dumps(result, indent=2))
    return result


def update(home):
    """Replace only unchanged owned app files, backing up the installed version."""
    manifest_path = home / '.local/state/hyprcontroller/install.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('home') != str(home):
        raise RuntimeError('Installation manifest belongs to another home directory')
    app = home / '.local/share/hyprcontroller-overlay'
    if app.is_symlink() or app.resolve() != app:
        raise RuntimeError('Installed application directory changed')
    records = {r['path']: r for r in manifest['files']}
    config = home / '.config/hyprcontroller/overlay.json'
    for record in records.values():
        path = Path(record['path'])
        # Update never writes the configuration, and editing it is expected.
        if path == config:
            continue
        if not path.is_relative_to(home) or not path.is_file() or path.is_symlink() or \
                str(path.resolve()) != record.get('resolved_path', str(path)) or \
                digest(path) != record['sha256'] or path.stat().st_mode & 0o777 != record['mode']:
            raise RuntimeError(f'Later edit or missing owned file; update refused: {path}')
    changes = {app / source.relative_to(ROOT): source.read_bytes() for source in application_sources()}
    for path in changes:
        if path.resolve() != path or (os.path.lexists(path) and str(path) not in records):
            raise RuntimeError(f'Unowned file or symlink; update refused: {path}')
    stop_running(home)
    backup = home / '.local/state/hyprcontroller/backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-update')
    backup.mkdir(mode=0o700, parents=True)
    entries = inventory([Path(p) for p in records] + [manifest_path])
    archive = backup / 'installed.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        for name in sorted(entries):
            tf.inodes.clear()
            tf.add('/' + name, arcname=name, recursive=False)
    archive.chmod(0o600)
    saved = {'entries': entries, 'archive_sha256': digest(archive)}
    verify(archive, saved)
    saved['verified'] = True
    (backup / 'manifest.json').write_text(json.dumps(saved, indent=2) + '\n')
    (backup / 'manifest.json').chmod(0o600)
    old = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
           for path in changes}
    manifest_bytes = manifest_path.read_bytes()
    temporary_files = set()
    created_dirs = []
    try:
        for path, content in changes.items():
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir(mode=0o700)
                created_dirs.append(parent)
            temp = path.with_name(path.name + '.hyprcontroller-update')
            with temp.open('xb') as out:
                temporary_files.add(temp)
                out.write(content)
            temp.chmod(records.get(str(path), {}).get('mode', 0o644))
            temp.replace(path)
            records[str(path)] = file_record(path)
        manifest['files'] = list(records.values())
        manifest.setdefault('created_directories', []).extend(str(p) for p in created_dirs)
        manifest['generated_caches'] = [importlib.util.cache_from_source(r['path']) for r in records.values() if r['path'].endswith('.py')]
        manifest.setdefault('updates', []).append({'backup': str(backup), 'files': [str(p) for p in changes]})
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    except BaseException:
        for path in temporary_files:
            path.unlink(missing_ok=True)
        for path, original in old.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original[0]); path.chmod(original[1])
        for path in reversed(created_dirs):
            try:
                path.rmdir()
            except OSError:
                pass
        manifest_path.write_bytes(manifest_bytes)
        raise
    finally:
        for path in temporary_files:
            path.unlink(missing_ok=True)
    print(json.dumps({'updated_files': len(changes), 'backup': str(backup), 'configuration_preserved': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'update', 'rollback'])
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--home', type=Path, default=Path.home(), help='Alternate home for isolated installation tests')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--keep-modified', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'install':
            if not args.backup:
                parser.error('--backup is required for installation')
            if args.dry_run or args.keep_modified:
                parser.error('These options apply only to rollback')
            install(args.home.resolve(), args.backup)
        elif args.command == 'update':
            if args.dry_run or args.keep_modified:
                parser.error('These options apply only to rollback')
            update(args.home.resolve())
        else:
            rollback(args.home.resolve(), args.dry_run, args.keep_modified)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
