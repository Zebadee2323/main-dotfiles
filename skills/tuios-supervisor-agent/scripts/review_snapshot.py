#!/usr/bin/env python3
"""Read-only Git snapshot guard. Does not stage, commit, or alter the checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def files(root):
    paths = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
    result = {}
    for raw in sorted(set(paths) - {b''}):
        name = os.fsdecode(raw)
        path = root / name
        if path.is_symlink():
            data, mode = os.fsencode(os.readlink(path)), '120000'
        elif path.is_file():
            data = path.read_bytes()
            mode = '100755' if path.stat().st_mode & 0o111 else '100644'
        elif path.exists():
            raise ValueError(f'Unsupported directory/submodule: {name}')
        else:
            continue
        result[name] = {'sha256': digest(data), 'mode': mode}
    return result


def capture(root, base):
    root = Path(os.fsdecode(git(root, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if git(root, 'ls-files', '--unmerged', '-z'):
        raise ValueError('Resolve index conflicts before capturing approval')
    gd = Path(os.fsdecode(git(root, 'rev-parse', '--absolute-git-dir')).strip()).resolve()
    state = {'version': 1, 'root': str(root), 'git_dir': str(gd),
             'branch': git(root, 'symbolic-ref', '--short', 'HEAD').decode().strip(),
             'head': git(root, 'rev-parse', 'HEAD').decode().strip(),
             'base': git(root, 'rev-parse', '--verify', base+'^{commit}').decode().strip(),
             'index': digest(git(root, 'ls-files', '--stage', '-z')),
             'status': digest(git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')),
             'files': files(root), 'operations': {}}
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        path = gd / name
        if path.is_dir():
            raise ValueError('Finish active rebase before capturing approval')
        if path.exists():
            state['operations'][name] = path.read_text()
    state['id'] = digest(json.dumps(state, sort_keys=True).encode())
    return state


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, sort_keys=True, indent=2)
            out.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verify_commit(root, snapshot, commit):
    actual = {}
    for record in git(root, 'ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if not record:
            continue
        meta, raw = record.split(b'\t', 1)
        mode, kind, oid = meta.split()
        if kind != b'blob':
            raise ValueError('Submodule verification requires explicit review')
        actual[os.fsdecode(raw)] = {'mode': mode.decode(), 'sha256': digest(git(root, 'cat-file', 'blob', oid.decode()))}
    return actual == snapshot['files']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['capture', 'verify', 'verify-commit'])
    p.add_argument('--root', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--base')
    p.add_argument('--commit', default='HEAD')
    a = p.parse_args()
    try:
        if a.action == 'capture':
            if not a.base:
                p.error('capture requires --base')
            root = Path(os.fsdecode(git(a.root, 'rev-parse', '--show-toplevel')).strip()).resolve()
            if Path(a.manifest).resolve().is_relative_to(root):
                raise ValueError('Store manifests outside the checkout')
            value = capture(root, a.base)
            if capture(root, a.base) != value:
                raise ValueError('Checkout changed during capture')
            atomic_write(a.manifest, value)
            print(json.dumps({'ok': True, 'snapshot': value['id'], 'manifest': a.manifest}))
        else:
            value = json.loads(Path(a.manifest).read_text())
            if a.action == 'verify':
                ok = capture(a.root, value['base']) == value
            else:
                ok = str(Path(a.root).resolve()) == value['root'] and verify_commit(a.root, value, a.commit)
            print(json.dumps({'ok': ok, 'snapshot': value['id']}))
            return 0 if ok else 1
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
