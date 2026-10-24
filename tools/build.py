"""Build against the pinned official DuckDB SDK; no Cargo required."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.4.1'
ASSETS = {
    'source.zip': ('https://codeload.github.com/duckdb/duckdb/zip/refs/tags/v1.4.1',
                   'c299584bc826d4a148771325b904b7bd04082c8848076e08d0810c2135b79f08'),
    'windows-cli.zip': ('https://github.com/duckdb/duckdb/releases/download/v1.4.1/duckdb_cli-windows-amd64.zip',
                        '3fab5174658bf16189c80eaa047b3e7737847491c9e4433a985adf014618cbd9'),
    'linux-cli.zip': ('https://github.com/duckdb/duckdb/releases/download/v1.4.1/duckdb_cli-linux-amd64.zip',
                      '3e29952507ebd202e8d0e2678df4490689bfc9c86534e240b541791b969df0e1'),
    'linux-static.zip': ('https://github.com/duckdb/duckdb/releases/download/v1.4.1/static-libs-linux-amd64.zip',
                         '9bef95039fe6b37c4be82a098455c98b5e79e10b0fc34ed31bcf563da0475762'),
    'amalgamation.zip': ('https://github.com/duckdb/duckdb/releases/download/v1.4.1/libduckdb-src.zip',
                         '81da1c9943f7b16e8a41456549fba72473ace3c83887e813e5610eb446c19781'),
}


def acquire(sdk, name):
    url, expected = ASSETS[name]
    archive = sdk / name
    if not archive.exists():
        temporary = archive.with_suffix('.download')
        try:
            urllib.request.urlretrieve(url, temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'SHA256 mismatch: {name}')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f'SHA256 mismatch: {archive}')
    marker = sdk / f'.{name}.extracted'
    if not marker.exists():
        with zipfile.ZipFile(archive) as files:
            for member in files.infolist():
                target = (sdk / member.filename).resolve()
                if not target.is_relative_to(sdk.resolve()):
                    raise RuntimeError('archive path escapes SDK directory')
            files.extractall(sdk / 'linux-static' if name == 'linux-static.zip' else
                             sdk / 'amalgamation' if name == 'amalgamation.zip' else sdk)
        marker.touch()


def cmake_executable():
    cmake = shutil.which('cmake')
    if cmake:
        return cmake
    if os.name == 'nt':
        vswhere = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
        location = subprocess.check_output([str(vswhere), '-latest', '-products', '*', '-property', 'installationPath'], text=True).strip()
        candidate = Path(location) / 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'
        if candidate.exists():
            return str(candidate)
    raise RuntimeError('CMake is required (or use --direct on Linux)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=ROOT / 'target/fileindex-sdk')
    parser.add_argument('--build', type=Path, default=ROOT / 'target/fileindex-build')
    parser.add_argument('--direct', action='store_true', help='Linux: use c++ directly without CMake')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--duckdb-static', type=Path, help='Optional matching Windows DuckDB 1.4.1 static library')
    args = parser.parse_args()
    if platform.machine().lower() not in ('amd64', 'x86_64') or sys.platform not in ('win32', 'linux'):
        raise RuntimeError('This pinned SDK build supports Windows/Linux x86-64')
    sdk, build = args.sdk.resolve(), args.build.resolve()
    sdk.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    temporary = build / 'tmp'
    temporary.mkdir(exist_ok=True)
    # Compiler temporary output belongs on the selected build drive, not the OS drive.
    os.environ['TMP'] = str(temporary)
    os.environ['TEMP'] = str(temporary)
    os.environ['TMPDIR'] = str(temporary)
    if shutil.disk_usage(build).free < 2 * 1024 ** 3:
        raise RuntimeError('At least 2 GiB free disk space is required for build/validation')
    acquire(sdk, 'source.zip')
    host = 'windows' if os.name == 'nt' else 'linux'
    if host == 'windows':
        acquire(sdk, 'amalgamation.zip')
    else:
        acquire(sdk, 'linux-static.zip')
    acquire(sdk, host + '-cli.zip')
    cli = sdk / ('duckdb.exe' if host == 'windows' else 'duckdb')
    if host == 'linux':
        cli.chmod(cli.stat().st_mode | 0o111)
    version = subprocess.check_output([str(cli), '-csv', '-noheader', '-c', 'SELECT version();'], text=True).strip()
    if version != 'v' + VERSION:
        raise RuntimeError(f'Unexpected DuckDB version {version}')
    source = sdk / ('duckdb-' + VERSION)
    if args.direct:
        if host != 'linux':
            raise RuntimeError('--direct requires Linux')
        extension = build / 'fileindex.duckdb_extension'
        subprocess.run(['c++', '-std=c++17', '-O2', '-fPIC', '-shared', '-fno-rtti', '-fvisibility=hidden', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-DNDEBUG', '-DDUCKDB_BUILD_LOADABLE_EXTENSION', '-DDUCKDB_STATIC_BUILD',
                        '-isystem', str(source / 'src/include'), str(ROOT / 'src/fileindex.cpp'),
                        '-Wl,--start-group', *map(str, sorted((sdk / 'linux-static').glob('*.a'))), '-Wl,--end-group',
                        '-ldl', '-pthread', '-Wl,--exclude-libs,ALL', '-Wl,--gc-sections', '-o', str(extension)], check=True)
        subprocess.run([sys.executable, str(ROOT / 'tools/metadata.py'), str(extension), 'linux_amd64'], check=True)
    else:
        cmake = cmake_executable()
        configure = [cmake, '-S', str(ROOT), '-B', str(build), '-DDUCKDB_SDK=' + str(sdk), '-DDUCKDB_SOURCE=' + str(source),
                     '-DPython3_EXECUTABLE=' + sys.executable]
        if host == 'windows':
            configure.extend(['-G', 'Visual Studio 17 2022', '-A', 'x64'])
            if args.duckdb_static:
                configure.append('-DDUCKDB_STATIC_LIBRARY=' + str(args.duckdb_static.resolve()))
        else:
            configure.append('-DCMAKE_BUILD_TYPE=Release')
        subprocess.run(configure, check=True)
        subprocess.run([cmake, '--build', str(build), '--config', 'Release', '--parallel', '1'], check=True)
        extension = build / ('Release' if host == 'windows' else '') / 'fileindex.duckdb_extension'
    if args.test:
        subprocess.run([sys.executable, str(ROOT / 'tests/integration.py'), '--duckdb', str(cli),
                        '--extension', str(extension), '--work', str(build), '-v'], check=True)
    print(f'Extension: {extension}')
    print(f'DuckDB CLI: {cli}')


if __name__ == '__main__':
    main()
