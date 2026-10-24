# DuckDB fileindex の作例

必要な実行時資材は、対応するDuckDB 1.4.1と`fileindex.duckdb_extension`だけです。
YYアプリ、Python、ODBC、Dockerは実行時には使いません。

このディレクトリを作業フォルダーとしてDuckDBを開始します。

```sh
duckdb -unsigned
```

```sql
LOAD '/absolute/path/fileindex.duckdb_extension';
.read demo.sql
```

`.read`はCLIコマンドです。ライブラリから使用する場合はdemo.sqlの各SQLを実行します。
`customers.yyidx`が残っている場合は新規作成がエラーになります。
作例は地域とIDによる複合索引`customers-region-id.yyidx`も作成します（fileindex 0.2.0以降）。
地域の等価条件とIDの範囲条件を組み合わせ、EXPLAIN ANALYZEで索引利用を確認できます。
再作成するときだけ`file_index_create`に`replace := true`を追加してください。

固定長ファイルの場合は、たとえば4バイトのIDと6バイトの名称を持つ10バイトのレコードを
次のように定義します。レコード間に改行は入れません。

```sql
SELECT * FROM file_index_create(
  'customers.dat', 'fixed.yyidx', format := 'fixed',
  widths := [4,6], record_length := 10, key_type := 'BIGINT'
);
SELECT c1 AS id, rtrim(c2) AS name
FROM file_index_scan('fixed.yyidx') WHERE c1 = 2;
```

列名は`c1`、`c2`です。幅はUTF-8の文字数ではなくバイト数です。
詳しいオプションと制約は[オフラインマニュアル](../README.md)を参照してください。
