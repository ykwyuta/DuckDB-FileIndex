"""Package the extension, offline manual and licenses."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extension', type=Path)
    parser.add_argument('--sdk', type=Path, default=REPO / 'target/fileindex-sdk')
    parser.add_argument('--output', type=Path, default=REPO / 'target/fileindex-dist')
    parser.add_argument('--test', action='store_true', help='Run the packaged SQL demo using the SDK CLI')
    args = parser.parse_args()
    extension = args.extension or REPO / 'target/fileindex-build' / ('Release' if sys.platform == 'win32' else '') / 'fileindex.duckdb_extension'
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(extension, args.output)
    for name in ['README.md', 'VALIDATION.md', 'FORMAT.md']:
        shutil.copy2(ROOT / name, args.output)
    for name in ['COPYING', 'COPYING.EXCEPTION']:
        shutil.copy2(REPO / name, args.output)
    shutil.copy2(args.sdk / 'duckdb-1.4.1/LICENSE', args.output / 'LICENSE-DUCKDB.txt')
    source = args.sdk / 'duckdb-1.4.1'
    for directory, _, names in os.walk(source):
        for name in names:
            if name.lower().startswith(('license', 'copying', 'notice', 'copyright')):
                license_file = Path(directory) / name
                destination = args.output / 'licenses' / license_file.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(license_file, destination)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '*.yyidx', '*.idx')
    shutil.copytree(ROOT / 'demo', args.output / 'demo', dirs_exist_ok=True, ignore=ignore)
    shutil.copytree(ROOT / 'docs', args.output / 'docs', dirs_exist_ok=True)
    # Explicit allowlist: never package .git, SDK caches, or the output itself.
    source_output = args.output / 'source'
    source_output.mkdir(parents=True, exist_ok=True)
    for name in ['src', 'tools', 'tests', 'demo', 'docs', '.github']:
        shutil.copytree(ROOT / name, source_output / name, dirs_exist_ok=True, ignore=ignore)
    for name in ['CMakeLists.txt', 'README.md', 'FORMAT.md', 'VALIDATION.md',
                 'COPYING', 'COPYING.EXCEPTION', '.gitignore', '.gitattributes', 'NOTICE.md']:
        shutil.copy2(ROOT / name, source_output / name)
    shutil.copy2(ROOT / 'NOTICE.md', args.output)
    if args.test:
        cli = args.sdk.resolve() / ('duckdb.exe' if sys.platform == 'win32' else 'duckdb')
        with tempfile.TemporaryDirectory(prefix='fileindex-package-', dir=args.output.parent) as temporary:
            work = Path(temporary)
            shutil.copytree(args.output / 'demo', work / 'demo')
            sql = "LOAD '" + (args.output.resolve() / 'fileindex.duckdb_extension').as_posix().replace("'", "''") + "';\n"
            sql += (work / 'demo/demo.sql').read_text(encoding='utf-8')
            subprocess.run([str(cli), '-unsigned', '-batch', '-bail'], input=sql, encoding='utf-8',
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=work / 'demo', check=True, timeout=60)
        print('Packaged standalone SQL demo: OK')


if __name__ == '__main__':
    main()
