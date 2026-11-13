"""Assemble the shared modules and supplement into a local runtime.

This setup command does not start the web server or send any broker request.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / 'code/application'
    base = root.parent / 'code/application'
    expected = json.loads((root / 'code/source-manifest.json').read_text('utf-8'))['original_source_files_sha256']
    selected = {}
    for name, digest in expected.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise SystemExit(f'Invalid application path: {name}')
        candidates = [(source / relative).resolve(), (base / relative).resolve()]
        if not candidates[0].is_relative_to(source.resolve()) or not candidates[1].is_relative_to(base.resolve()):
            raise SystemExit(f'Invalid application path: {name}')
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise SystemExit(f'Missing application source: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f'Application source checksum mismatch: {name}')
        selected[name] = path
    runtime = root / 'runtime'
    if runtime.exists() and any(runtime.iterdir()):
        raise SystemExit('runtime already exists. Keep its settings and records; use start.cmd. Setup does not overwrite it.')
    for name, path in selected.items():
        destination = runtime / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    venv.EnvBuilder(with_pip=True).create(runtime / '.venv')
    python = runtime / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(runtime / 'requirements.txt')], check=True)
    print('Setup complete. Configure your own NH credentials outside the submission folder, then use start.cmd.')


if __name__ == '__main__':
    main()
