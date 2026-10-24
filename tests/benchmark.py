"""Verify index lookup against a full scan, reporting time and physical source reads."""
import argparse
import csv
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import time


def literal(value):
    return "'" + str(value).replace('\\', '/').replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duckdb', type=lambda p: str(Path(p).resolve()), required=True)
    parser.add_argument('--extension', type=lambda p: str(Path(p).resolve()), required=True)
    parser.add_argument('--rows', type=int, default=1000000)
    parser.add_argument('--composite', action='store_true', help='Benchmark (tenant,id) composite index')
    parser.add_argument('--work', type=Path, default=Path('target/fileindex-benchmark'))
    args = parser.parse_args()
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    if args.rows < 10 or shutil.disk_usage(args.work).free < args.rows * 256 + 256 * 1024 ** 2:
        raise RuntimeError('Insufficient disk space, or too few rows')
    source, index = args.work / 'records.csv', args.work / 'records.yyidx'
    if source.exists() or index.exists():
        raise RuntimeError('Use a new work directory; existing benchmark files are preserved')
    with source.open('w', encoding='utf-8', newline='') as out:
        writer = csv.writer(out, lineterminator='\r\n')
        writer.writerow(['id', 'tenant', 'payload', 'amount'] if args.composite else ['id', 'payload', 'amount'])
        for i in range(args.rows - 1, -1, -1):
            values = [i, 'record-' + str(i).zfill(10) + '-' + 'x' * 48, i % 1000]
            if args.composite:
                values.insert(1, f't{i%100:02}')
            writer.writerow(values)

    def run(sql, raw=False):
        start = time.perf_counter()
        process = subprocess.run([args.duckdb, '-unsigned', '-json', '-batch', '-bail', '-c',
                                  f'LOAD {literal(args.extension)}; {sql}'],
                                 capture_output=True, encoding='utf-8', errors='replace', timeout=600)
        elapsed = time.perf_counter() - start
        if process.returncode:
            raise RuntimeError(process.stderr)
        return (process.stdout if raw else json.loads(process.stdout)), elapsed

    key = "key_columns:=[2,1],key_types:=['VARCHAR','BIGINT']" if args.composite else "key_type:='BIGINT'"
    _, build_time = run(f"SELECT * FROM file_index_create({literal(source)},{literal(index)},{key},memory_limit:='64MB')")
    scan = f'file_index_scan({literal(index)})'
    schema = "'id':'BIGINT'," + ("'tenant':'VARCHAR'," if args.composite else '') + "'payload':'VARCHAR','amount':'VARCHAR'"
    reference = f"read_csv({literal(source)},header=true,columns={{{schema}}})"
    selected = args.rows // 2
    condition = f"tenant='t{selected%100:02}' AND id={selected}" if args.composite else f'id={selected}'
    times = {}
    for name, relation in [('indexed', scan), ('sequential', reference)]:
        durations = []
        for _ in range(5):
            result, elapsed = run(f'SELECT * FROM {relation} WHERE {condition}')
            expected = {'id': selected, 'payload': 'record-' + str(selected).zfill(10) + '-' + 'x' * 48,
                        'amount': str(selected % 1000)}
            if args.composite:
                expected['tenant'] = f't{selected%100:02}'
            if result != [expected]:
                raise AssertionError(result)
            durations.append(elapsed)
        times[name] = statistics.median(durations)
    count, _ = run(f'SELECT count(*) AS n, sum(id) AS total FROM {scan}')
    if count != [{'n': args.rows, 'total': args.rows * (args.rows - 1) // 2}]:
        raise AssertionError(count)
    profile, _ = run(f'EXPLAIN ANALYZE SELECT payload FROM {scan} WHERE {condition}', raw=True)
    import re
    if not re.search(r'Source records read:[^0-9]*\b1\b', profile):
        raise AssertionError(profile)
    if args.composite and not re.search(r'Index prefix columns:[^0-9]*\b2\b', profile):
        raise AssertionError(profile)
    (args.work / 'lookup-plan.txt').write_text(profile, encoding='utf-8')
    report = {'rows': args.rows, 'composite': args.composite, 'source_bytes': source.stat().st_size, 'index_bytes': index.stat().st_size,
              'index_build_seconds': build_time, 'median_seconds_including_cli_startup': times,
              'source_records_read_for_equality': 1,
              'verification': 'point lookup payload, full row count and key sum matched'}
    (args.work / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
