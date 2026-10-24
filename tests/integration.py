"""Exercise a real official DuckDB CLI with only the loadable extension."""
import argparse
import csv
import json
import os
import shutil
import struct
from pathlib import Path
import subprocess
import tempfile
import unittest


def literal(value):
    return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"


class ExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="fileindex-test-", dir=OPTIONS.work)
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def sql(self, sql, error=None, text=False):
        result = subprocess.run(
            [OPTIONS.duckdb, "-unsigned", "-json", "-batch", "-bail", "-c",
             f"LOAD {literal(OPTIONS.extension)}; {sql}"],
            cwd=self.root, capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if error:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(error.lower(), result.stderr.lower())
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        if text:
            return result.stdout
        # Most tests issue one result-producing statement.
        return json.loads(result.stdout) if result.stdout.strip() else []

    def source(self, name="data.csv", rows=None):
        rows = rows if rows is not None else [
            ["3", "猫,ねこ", "30"], ["1", 'a"b', "10"], ["2", "two\nlines", "20"],
            ["2", "duplicate", "21"], ["\\N", "null", "0"],
            ["-9223372036854775808", "min", "-1"], ["9223372036854775807", "max", "99"],
        ]
        path = self.root / (self._testMethodName + "-" + name)
        with path.open("w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, lineterminator="\r\n")
            writer.writerow(["id", "name", "amount"])
            writer.writerows(rows)
        return path

    def create(self, source, options="key_type := 'BIGINT'", index=None):
        index = index or source.with_suffix(".yyidx")
        result = self.sql(f"SELECT * FROM file_index_create({literal(source)}, {literal(index)}, {options})")
        self.assertEqual(result[0]["index_path"], index.as_posix())
        return index

    def scan(self, index):
        return f"file_index_scan({literal(index)})"

    def csv_file(self, name, columns, rows):
        path = self.root / name
        with path.open('w', encoding='utf-8', newline='') as out:
            writer = csv.writer(out, lineterminator='\r\n')
            writer.writerow(columns)
            writer.writerows(rows)
        return path

    def reference(self, path, columns):
        schema = ','.join(literal(name) + ':' + literal(kind) for name, kind in columns)
        return f"read_csv({literal(path)},header=true,columns={{{schema}}},nullstr='\\N')"

    def test_composite_mixed_types_and_nulls(self):
        source = self.csv_file('composite.csv', ['tenant', 'id', 'tag', 'payload'], [
            ['a', '2', 'b', 'one'], ['a', '2', 'b', 'duplicate'], ['a', '2', 'a', 'two'],
            ['a', '10', 'b', 'three'], ['a', '-1', '', 'four'], ['b', '2', 'b', 'five'],
            ['\\N', '2', 'b', 'null-first'], ['a', '\\N', 'b', 'null-second'],
            ['a', '2', '\\N', 'null-third'], ['', '2', 'b', 'empty-first'],
            ['猫,ねこ', '3', 'line\nbreak', '日本語'], ['A', '2', 'b', 'upper'],
            ['a', '-9223372036854775808', 'b', 'minimum'], ['a', '9223372036854775807', 'b', 'maximum']])
        index = self.create(source, "key_columns:=[1,2,3],key_types:=['VARCHAR','BIGINT','VARCHAR']")
        self.assertEqual(index.read_bytes()[:8], b'YYFIDX02')
        reference = self.reference(source, [('tenant','VARCHAR'),('id','BIGINT'),('tag','VARCHAR'),('payload','VARCHAR')])
        predicates = ["true", "tenant='a'", "tenant='a' AND id=2", "tenant='a' AND id=2 AND tag='b'",
                      "tenant='a' AND id>=2 AND id<10", "tenant='a' AND id>2", "tenant='a' AND id<=2",
                      "tenant='a' AND id=2 AND tag>='a' AND tag<'z'", "tenant>'A' AND tenant<'b' AND id=2",
                      "id=2", "tag='b'", "tenant='a' AND tag='b'", "tenant IS NULL AND id=2",
                      "tenant='a' AND id IS NULL AND tag='b'", "tenant='a' AND id=2 AND tag IS NULL",
                      "tenant IS NOT NULL AND id=2", "tenant='a' AND id IS NOT NULL",
                      "tenant='a' AND id>10 AND id<2", "tenant='a' AND id=2 AND tag='missing'",
                      "tenant='a' AND id=2 AND tag='b' AND payload='duplicate'",
                      "tenant='a' OR id=2", "tenant IN ('a','b') AND id=2", "tenant='a' AND id IN (2,10)",
                      "tenant COLLATE NOCASE='a' AND id=2", "tenant='a' AND CAST(id AS VARCHAR)='2'",
                      "tenant='' AND id=2", "tenant='猫,ねこ' AND id=3 AND tag='line\nbreak'",
                      "tenant='a' AND id IS DISTINCT FROM 2", "tenant='a' AND id IS NOT DISTINCT FROM NULL"]
        for predicate in predicates:
            for columns in ['*', 'payload', 'tenant,id', 'count(*) AS n']:
                with self.subTest(predicate=predicate, columns=columns):
                    query = f'SELECT {columns} FROM {{}} WHERE {predicate}'
                    self.assertCountEqual(self.sql(query.format(self.scan(index))), self.sql(query.format(reference)))
        plan = self.sql(f"EXPLAIN ANALYZE SELECT payload FROM {self.scan(index)} WHERE tenant='a' AND id=2 AND tag='b'", text=True)
        self.assertRegex(plan, r'Index prefix columns:[^0-9]*\b3\b')
        self.assertRegex(plan, r'Source records read:[^0-9]*\b2\b')

    def test_composite_nonfirst_order_and_fixed(self):
        path = self.csv_file('reordered.csv', ['payload', 'id', 'tenant'],
                             [['x','2','a'], ['y','1','b'], ['z','2','a'], ['w','10','a']])
        index = self.create(path, "key_columns:=[3,2],key_types:=['VARCHAR','BIGINT']")
        self.assertEqual(self.sql(f"SELECT payload FROM {self.scan(index)} WHERE tenant='a' AND id=2 ORDER BY payload"),
                         [{'payload':'x'}, {'payload':'z'}])
        fixed = self.root / 'composite-width.fixed'
        fixed.write_bytes(b'0203cat   0103dog   0201fox   0203cat   ')
        index = self.create(fixed, "format:='fixed',widths:=[2,2,6],key_columns:=[1,2],key_types:=['BIGINT','BIGINT']")
        self.assertEqual(self.sql(f"SELECT * FROM {self.scan(index)} WHERE c1=2 AND c2<3"), [{'c1':2,'c2':1,'c3':'fox   '}])
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {self.scan(index)} WHERE c1=2 AND c2=3"), [{'n':2}])
        empty = self.csv_file('empty-composite.csv', ['tenant','id'], [])
        index = self.create(empty, "key_columns:=[1,2],key_types:=['VARCHAR','BIGINT']")
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {self.scan(index)} WHERE tenant='a' AND id>0"), [{'n':0}])

    def test_composite_default_types_and_maximum_keys(self):
        source = self.csv_file('default-types.csv', ['a','b'], [['2','10'],['2','2'],['1','1']])
        index = self.create(source, 'key_columns:=[1,2]')
        self.assertEqual(self.sql(f"SELECT * FROM {self.scan(index)} WHERE a='2' ORDER BY b"),
                         [{'a':'2','b':'10'},{'a':'2','b':'2'}])
        columns = [f'k{i}' for i in range(32)]
        source = self.csv_file('max-keys.csv', columns, [list(range(32))])
        options = 'key_columns:=['+','.join(str(i+1) for i in range(32))+"],key_types:=["+','.join("'BIGINT'" for _ in range(32))+']'
        index = self.create(source, options)
        condition = ' AND '.join(f'k{i}={i}' for i in range(32))
        self.assertEqual(self.sql(f'SELECT k31 FROM {self.scan(index)} WHERE {condition}'), [{'k31':31}])

    def test_composite_prefix_access_and_large_ranges(self):
        path = self.csv_file('composite-large.csv', ['tenant','id','payload'],
                             [[f't{tenant:02}', str(i), f'p-{tenant}-{i}'] for tenant in range(20) for i in range(500)])
        index = self.create(path, "key_columns:=[1,2],key_types:=['VARCHAR','BIGINT']")
        scan = self.scan(index)
        for predicate, reads, prefix in [("tenant='t10' AND id=123",1,2),
                                          ("tenant='t10' AND id>=123 AND id<128",5,2),
                                          ("tenant='t10'",500,1),
                                          ("id=123",10000,0),
                                          ("tenant>='t00'",10000,0)]:
            with self.subTest(predicate=predicate):
                plan = self.sql(f'EXPLAIN ANALYZE SELECT payload FROM {scan} WHERE {predicate}', text=True)
                self.assertRegex(plan, rf'Source records read:[^0-9]*\b{reads}\b')
                self.assertRegex(plan, rf'Index prefix columns:[^0-9]*\b{prefix}\b')
                self.assertIn('Sequential source scan' if reads == 10000 else 'Persistent index lookup', plan)
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {scan} WHERE tenant='t10' AND id>=123 AND id<128"), [{'n':5}])
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {scan} WHERE tenant>='t00' AND id=123"), [{'n':20}])

    def test_composite_invalid_options_and_corruption(self):
        source = self.source()
        for options in ["key_columns:=[]", "key_columns:=[1,1]", "key_columns:=[0,2]", "key_columns:=[1,NULL]",
                        "key_columns:=[1,9]", "key_types:=['BIGINT']", "key_columns:=[1,2],key_types:=['BIGINT']",
                        "key_columns:=[1,2],key_types:=['BIGINT',NULL]", "key_columns:=[1,2],key_types:=['BIGINT','DATE']",
                        "key_columns:=[1,2],key_column:=1", "key_columns:=[1,2],key_type:='BIGINT'",
                        "key_columns:=[1,2],key_types:=NULL", "key_columns:=[1,2],key_types:=['BIGINT','BIGINT']",
                        'key_columns:=[' + ','.join(str(i) for i in range(1,34)) + ']']:
            with self.subTest(options=options):
                self.sql(f"SELECT * FROM file_index_create({literal(source)},{literal(source.with_suffix('.bad'))},{options})", error='fileindex')
        index = self.create(source, "key_columns:=[1,2],key_types:=['BIGINT','VARCHAR']")
        self.sql(f"SELECT * FROM file_index_create({literal(source)},{literal(index)},key_columns:=[1,2],key_types:=['BIGINT','BIGINT'],replace:=true)", error='BIGINT key')
        self.assertEqual(self.sql(f'SELECT count(*) AS n FROM {self.scan(index)}'), [{'n':7}])
        data = bytearray(index.read_bytes())
        position = 8
        for _ in range(3):
            size = struct.unpack_from('<Q',data,position)[0]; position += 8+size
        # Skip source size/time/hash, row count/directory/record length to v2 key count.
        key_count = position+6*8
        for offset, value in [(key_count,0), (key_count,33), (key_count+8,999),
                              (key_count+24,0), (key_count+16,2)]:
            corrupted = bytearray(data); struct.pack_into('<Q',corrupted,offset,value); index.write_bytes(corrupted)
            self.sql(f'SELECT * FROM {self.scan(index)}', error='fileindex')
        index.write_bytes(data)

    def test_v1_independent_binary_fixture(self):
        # Build the documented v1 binary layout independently of the extension's writer.
        source = self.csv_file('v1.csv', ['id','name','amount'],
                               [['2','two','2'], ['\\N','null','0'], ['-1','minus','1'], ['2','dup','3']])
        control = self.create(source)
        native = control.read_bytes(); cursor = 8
        for _ in range(3):
            size = struct.unpack_from('<Q',native,cursor)[0]; cursor += 8+size
        modified = struct.unpack_from('<Q',native,cursor+8)[0]
        content = source.read_bytes(); fingerprint = 14695981039346656037
        for byte in content:
            fingerprint = ((fingerprint ^ byte)*1099511628211) & ((1<<64)-1)
        pack = lambda n: struct.pack('<Q', n & ((1<<64)-1))
        text = lambda s: pack(len(s.encode()))+s.encode()
        records = []; offset = len(content.splitlines(keepends=True)[0])
        for record in content.splitlines(keepends=True)[1:]:
            key = record.split(b',')[0]; records.append((None if key==b'\\N' else int(key),offset,len(record))); offset+=len(record)
        entries = []
        for key, offset, length in sorted(records,key=lambda r:(r[0] is not None, r[0] or 0,r[1])):
            entries.append(pack(key is None)+(b'' if key is None else pack(key))+pack(offset)+pack(length))
        def header(directory):
            return (b'YYFIDX01'+text(source.as_posix())+text('csv')+text('\\N')+
                    b''.join(pack(v) for v in [len(content),modified,fingerprint,4,directory,0,0,1,1,44,0,3])+
                    text('id')+text('name')+text('amount'))
        start=len(header(0)); directory=start+sum(map(len,entries)); offsets=[]; current=start
        for entry in entries:
            offsets.append(pack(current)); current+=len(entry)
        legacy=header(directory)+b''.join(entries)+b''.join(offsets)
        self.assertEqual(native,legacy)
        control.write_bytes(legacy)
        self.assertEqual(self.sql(f'SELECT name FROM {self.scan(control)} WHERE id=2 ORDER BY name'), [{'name':'dup'},{'name':'two'}])

    def test_csv_roundtrip_and_predicates(self):
        source = self.source()
        index = self.create(source)
        scan = self.scan(index)
        reference = (f"read_csv({literal(source)}, header=true, columns="
                     "{'id':'BIGINT','name':'VARCHAR','amount':'VARCHAR'}, nullstr='\\N')")
        predicates = [
            "true", "id = 2", "2 = id", "id < 2", "id <= 2", "id > 2", "id >= 2",
            "id BETWEEN 1 AND 3", "id > 2 AND id < 2", "id != 2", "id IS NULL",
            "id IS NOT NULL", "id IN (1,3,NULL)", "id = 1 OR id = 3", "id NOT IN (1,3)",
            "id IS DISTINCT FROM 2", "id IS NOT DISTINCT FROM 2", "id IS NOT DISTINCT FROM NULL",
            "id = -9223372036854775808", "id = 9223372036854775807", "id = 100000",
            "name LIKE '%line%'", "id = 2 AND name = 'duplicate'", "id=1 OR name='duplicate'",
            "CAST(id AS VARCHAR) = '2'", "abs(id::HUGEINT) = 2", "id IN (1, 2, 300, 500, 700, 900)",
        ]
        for predicate in predicates:
            with self.subTest(predicate=predicate):
                query = f"SELECT * FROM {{}} WHERE {predicate} ORDER BY id NULLS FIRST,name"
                self.assertEqual(self.sql(query.format(scan)), self.sql(query.format(reference)))
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {scan}")[0]["n"], 7)
        self.assertEqual(self.sql(f"SELECT name FROM {scan} WHERE id=2 ORDER BY name"),
                         [{"name": "duplicate"}, {"name": "two\nlines"}])

    def test_string_index_collation_and_residual(self):
        source = self.source(rows=[["a", "one", "1"], ["A", "two", "2"], ["猫", "three", "3"],
                                   ["あ", "four", "4"], ["\\N", "five", "5"], ["", "empty", "6"]])
        index = self.create(source, "key_type := 'VARCHAR'")
        for predicate in ["id='a'", "id > 'A' AND id < '猫'", "id COLLATE NOCASE = 'a'", "id=''",
                          "id IS NULL", "id LIKE 'a%'", "lower(id)='a'", "id IN ('a','猫')"]:
            with self.subTest(predicate=predicate):
                query = f"SELECT * FROM {{}} WHERE {predicate} ORDER BY name"
                reference = f"read_csv({literal(source)}, all_varchar=true, nullstr='\\N')"
                self.assertEqual(self.sql(query.format(self.scan(index))), self.sql(query.format(reference)))

    def test_nonfirst_key_and_projection(self):
        path = self.root / 'second-key.csv'
        path.write_text('name,id,group\nalpha,2,x\nbeta,1,y\ngamma,2,x\n', encoding='utf-8')
        index = self.create(path, "key_column:=2,key_type:='BIGINT'")
        for projection in ['*', 'name', '"group",name', 'count(*) AS n']:
            for predicate in ["id=2", "id=2 AND name='gamma'", "name='alpha'", 'id>0']:
                with self.subTest(projection=projection, predicate=predicate):
                    query = f'SELECT {projection} FROM {{}} WHERE {predicate}'
                    reference = f"read_csv({literal(path)},columns={{'name':'VARCHAR','id':'BIGINT','group':'VARCHAR'}})"
                    self.assertCountEqual(self.sql(query.format(self.scan(index))), self.sql(query.format(reference)))

    def test_only_cli_and_extension_in_isolated_directory(self):
        isolated = self.root / 'isolated'
        isolated.mkdir()
        cli = isolated / Path(OPTIONS.duckdb).name
        extension = isolated / 'fileindex.duckdb_extension'
        shutil.copy2(OPTIONS.duckdb, cli)
        shutil.copy2(OPTIONS.extension, extension)
        (isolated / "日本語's.csv").write_text('id,name\n1,猫\n', encoding='utf-8')
        process = subprocess.run([str(cli), '-unsigned', '-json', '-batch', '-bail', '-c',
                                  f"LOAD {literal(extension)}; SELECT * FROM file_index_create(" +
                                  literal(isolated / "日本語's.csv") + ", 'data.idx',key_type:='BIGINT');"],
                                 cwd=isolated, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        process = subprocess.run([str(cli), '-unsigned', '-json', '-batch', '-bail', '-c',
                                  f"LOAD {literal(extension)}; SELECT name FROM file_index_scan('data.idx') WHERE id=1;"],
                                 cwd=isolated, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), [{'name': '猫'}])

    def test_fixed_width_byte_positions(self):
        path = self.root / "fixed.dat"
        path.write_bytes(b"0003cat   " + b"0001dog   " + b"0002" + "猫   ".encode())
        index = self.create(path, "format := 'fixed', widths := [4,6], key_type := 'BIGINT'")
        self.assertEqual(self.sql(f"SELECT * FROM {self.scan(index)} WHERE c1=2"), [{"c1": 2, "c2": "猫   "}])
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {self.scan(index)}")[0]["n"], 3)

    def test_stale_and_strong_verification(self):
        source = self.source(rows=[["1", "cat", "1"]])
        # DrvFS/NTFS round subsecond timestamps; exact seconds isolate hash validation.
        os.utime(source, (1700000000, 1700000000))
        index = self.create(source)
        stamp = source.stat()
        source.write_bytes(source.read_bytes().replace(b"cat", b"dog"))
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.sql(f"SELECT * FROM file_index_scan({literal(index)}, verify := true)", error="fingerprint changed")
        source.write_bytes(source.read_bytes() + b"2,new,2\r\n")
        self.sql(f"SELECT * FROM {self.scan(index)}", error="source changed")
        self.create(source, "key_type := 'BIGINT', replace := true", index)
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {self.scan(index)}")[0]["n"], 2)

    def test_failed_build_preserves_index_and_source(self):
        source = self.source()
        index = self.create(source)
        before = index.read_bytes()
        original = source.read_bytes()
        self.sql(f"SELECT * FROM file_index_create({literal(source)}, {literal(index)})", error="already exists")
        self.sql(f"SELECT * FROM file_index_create({literal(source)}, {literal(source)}, replace:=true)", error="different files")
        source.write_bytes(b'id,name\r\n1,"unterminated')
        self.sql(f"SELECT * FROM file_index_create({literal(source)}, {literal(index)}, replace:=true)", error="unterminated")
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".fileindex-*")), [])
        source.write_bytes(original)

    def test_invalid_sources_and_options(self):
        for i, payload in enumerate([b'a,a\n1,2\n', b'a,b\n1\n', b'a,b\n1,un"quoted\n',
                                     b'a,b\n1,"ok"bad\n', b'a,b\r1,2\r', b'a,b\n1,\xff\n']):
            path = self.root / f"bad-{i}.csv"
            path.write_bytes(payload)
            self.sql(f"SELECT * FROM file_index_create({literal(path)}, {literal(path.with_suffix('.idx'))})", error="fileindex")
        path = self.source()
        for option in ["key_column:=0", "key_column:=99", "key_type:='DECIMAL'", "format:='parquet'",
                       "format:='fixed',widths:=[4,6],record_length:=11", "delimiter:='||'",
                       "memory_limit:='1MB'", "widths:=[1]", "header:=NULL"]:
            with self.subTest(option=option):
                self.sql(f"SELECT * FROM file_index_create({literal(path)}, {literal(path.with_suffix('.idx'))}, {option})", error="fileindex")
        path = self.root / 'incomplete.fixed'
        path.write_bytes(b'012345')
        self.sql(f"SELECT * FROM file_index_create({literal(path)}, {literal(path.with_suffix('.idx'))}, format:='fixed',widths:=[4,6])", error="incomplete")

    def test_empty_and_headerless(self):
        source = self.source(rows=[])
        index = self.create(source)
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {self.scan(index)}")[0]["n"], 0)
        path = self.root / 'headerless.csv'
        path.write_bytes(b'2;two\n1;one')
        index = self.create(path, "header:=false, delimiter:=';', key_type:='BIGINT'")
        self.assertEqual(self.sql(f"SELECT * FROM {self.scan(index)} WHERE c1=1"), [{"c1": 1, "c2": "one"}])
        path = self.root / 'empty.fixed'
        path.write_bytes(b'')
        index = self.create(path, "format:='fixed',widths:=[4,6]")
        self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {self.scan(index)}")[0]["n"], 0)

    def test_corrupt_and_truncated_index(self):
        source = self.source()
        index = self.create(source)
        content = index.read_bytes()
        for content_bad in [b'invalid!', content[:20], content[:-1], content + b'junk']:
            index.write_bytes(content_bad)
            self.sql(f"SELECT * FROM {self.scan(index)}", error="fileindex")

    def test_external_access_and_explain_no_side_effect(self):
        source = self.source()
        index = source.with_suffix('.idx')
        self.sql(f"EXPLAIN SELECT * FROM file_index_create({literal(source)}, {literal(index)})", text=True)
        self.assertFalse(index.exists())
        self.create(source, index=index)
        self.sql(f"SET enable_external_access=false; SELECT * FROM {self.scan(index)}", error="external access is disabled")

    def test_standalone_view_join_window_and_index_io(self):
        source = self.source(rows=[[str(i), f"row-{i}", str(i % 100)] for i in range(20000)])
        index = self.create(source)
        scan = self.scan(index)
        rows = self.sql(f"SELECT a.id,a.name, row_number() OVER (ORDER BY a.id) AS rn FROM {scan} a "
                        "JOIN (VALUES (100),(101)) b(id) USING(id) ORDER BY a.id")
        self.assertEqual(rows, [{"id": 100, "name": "row-100", "rn": 1}, {"id": 101, "name": "row-101", "rn": 2}])
        report = self.sql(f"EXPLAIN ANALYZE SELECT name FROM {scan} WHERE id=12345", text=True)
        self.assertIn("Persistent index lookup", report)
        self.assertIn("Source records read", report)
        # Parse the textual operator metrics from JSON; require only one physical source record.
        self.assertRegex(report, r'Source records read:[^0-9]*\b1\b')
        broad = self.sql(f"SELECT count(*) AS n FROM {scan} WHERE id>=1000 AND name LIKE 'row-%'")
        self.assertEqual(broad, [{'n': 19000}])
        broad_plan = self.sql(f"EXPLAIN ANALYZE SELECT count(*) FROM {scan} WHERE id>=1000", text=True)
        self.assertIn('Sequential source scan', broad_plan)
        database = self.root / 'view.duckdb'
        result = subprocess.run([OPTIONS.duckdb, str(database), '-unsigned', '-batch', '-bail', '-c',
                                 f"LOAD {literal(OPTIONS.extension)}; CREATE VIEW data AS SELECT * FROM {scan};"],
                                capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([OPTIONS.duckdb, str(database), '-unsigned', '-csv', '-batch', '-bail', '-c',
                                 f"LOAD {literal(OPTIONS.extension)}; SELECT name FROM data WHERE id=12345;"],
                                capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('row-12345', result.stdout)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--duckdb', required=True, type=lambda p: str(Path(p).resolve()))
    parser.add_argument('--extension', required=True, type=lambda p: str(Path(p).resolve()))
    parser.add_argument('--work', default=str(Path.cwd() / 'target'))
    OPTIONS, remaining = parser.parse_known_args()
    Path(OPTIONS.work).mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__, *remaining])
