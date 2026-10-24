# DuckDB-FileIndex — DuckDB向けCSV・固定長ファイル永続索引拡張

DuckDB **1.4.1 / Windows・Linux x86-64**用の独立したロード可能拡張です。
拡張バージョン0.2.0は、単一列に加えて最大32列の複合索引に対応します。
元のCSVや固定長ファイルをDuckDBのテーブルへ全件コピーせず、別ファイルに保持する索引で検索します。
実行時に必要なのはDuckDBと`fileindex.duckdb_extension`だけです。
Rust、Python、ODBC、Dockerには依存しません。
Python/CMake/C++はビルドと検証にだけ使います。

## 使い方

配布ファイルは開発用の未署名拡張です。公式DuckDB 1.4.1のCLIを次のように起動します。

```sh
duckdb -unsigned
```

```sql
LOAD '/absolute/path/fileindex.duckdb_extension';

SELECT * FROM file_index_create(
  'sales.csv', 'sales.yyidx', key_column := 1, key_type := 'BIGINT'
);
CREATE VIEW sales AS SELECT * FROM file_index_scan('sales.yyidx');

SELECT * FROM sales WHERE id = 123;
SELECT name FROM sales WHERE id >= 100 AND id < 200;
SELECT region, count(*) FROM sales GROUP BY region;
SELECT *, row_number() OVER (PARTITION BY region ORDER BY id) AS rn FROM sales;
EXPLAIN ANALYZE SELECT name FROM sales WHERE id = 123;
```

アプリケーションからはDuckDBの`allow_unsigned_extensions=true`を起動時に設定してLOADします。
SQLはDuckDBの方言です。T-SQL変換、ストアドプロシージャ、カーソルはこの拡張には含めません。
別プロセスでも索引を再利用できます。保存したVIEWも、再接続時に拡張をLOADすれば使用できます。
VIEWから参照する索引パスは絶対パスにすると作業フォルダーに依存しません。

## file_index_create(source, index, ...)

SQLの実行時に作成し、`index_path VARCHAR, rows UBIGINT`を1行返します。
`EXPLAIN`やバインドだけでは索引を作成しません。`SELECT *`で最後まで実行してください。

| オプション | 既定値 | 意味 |
| --- | --- | --- |
| `format` | `'csv'` | `'csv'`または`'fixed'` |
| `key_column` | `1` | 索引キーにする列。1始まり |
| `key_type` | `'VARCHAR'` | `'VARCHAR'`または符号付き64ビット整数`'BIGINT'` |
| `key_columns` | なし | 複合索引の列番号を順序付きで指定。1始まり、1～32列、重複不可。例`[3,1]` |
| `key_types` | 各列`'VARCHAR'` | `key_columns`と同じ順・同じ件数の型。例`['VARCHAR','BIGINT']` |
| `header` | CSVは`true` | 先頭レコードを列名にする。固定長は`false`のみ |
| `delimiter` | `','` | CSVの区切り。引用符・CR・LF・NUL以外のASCII1バイト。TSVはタブを指定 |
| `nullstr` | `'\N'` | 完全一致するフィールドをNULLにする。引用符の有無によらず適用 |
| `widths` | なし | 固定長の各列のバイト幅。例`[4,20,8]` |
| `record_length` | 幅の合計 | 固定長のレコードバイト数。幅の合計と一致必須 |
| `replace` | `false` | 既存索引を置換するか。元ファイルは変更しない |
| `memory_limit` | `'64MB'` | 索引構築に使う内部DuckDBのメモリ予算。16MB以上 |
| `temp_limit` | `'4GB'` | 内部DuckDBのソート等が使う一時ディスクの上限 |

`key_columns`/`key_types`と、単一列用の`key_column`/`key_type`は混在させられません。
`key_types`単独指定も拒否します。型を省略すると全キー列がVARCHARです。
キー以外の列はVARCHARです。数値・日付を使う場合はSQLでCASTしてください。
BIGINTキーは先頭のゼロを除いた数値で比較されます。空白・小数・オーバーフローは拒否します。
VARCHARはUTF-8のバイナリー順です。重複キーとNULLキーを保持し、同じキーのデータを失いません。
DECIMAL/日付キー、ユニーク索引、追加・更新時の差分索引更新は未対応です。
同じファイルに別のキーの索引を別名で作れますが、使用する索引はscanの引数で明示します。

### 複合索引の例と検索に使える列順

```sql
-- tenant、id、payloadという列を持つCSV
SELECT * FROM file_index_create(
  'events.csv', 'events-tenant-id.yyidx',
  key_columns := [1,2], key_types := ['VARCHAR','BIGINT']
);
CREATE VIEW events AS SELECT * FROM file_index_scan('events-tenant-id.yyidx');
SELECT payload FROM events WHERE tenant = 'A' AND id = 100;
SELECT payload FROM events WHERE tenant = 'A' AND id >= 100 AND id < 200;
EXPLAIN ANALYZE SELECT payload FROM events WHERE tenant = 'A' AND id = 100;
```

キーは指定順の辞書順で並べ、各成分は昇順・NULLを先頭にします。文字列を連結してキーにしないため、
カンマや改行、空文字、日本語を含む組み合わせも区別し、重複レコードをすべて保持します。
索引に指定したBIGINT列は、WHEREの有無や射影に関係なくBIGINTとして返します。

| 条件（索引がtenant,id,sequenceの順の場合） | 索引で絞り込める範囲 |
| --- | --- |
| tenant = A | 先頭1列 |
| tenant = A AND id = 100 | 先頭2列 |
| tenant = A AND id >= 100 AND id < 200 | tenantの等価条件とidの範囲 |
| tenant = A AND id = 100 AND sequence >= 5 | 先頭2列の等価条件と3列目の範囲 |
| tenant = A AND sequence = 5 | tenantで絞り、sequenceは残余条件 |
| tenant >= A AND id = 100 | tenantの範囲で絞り、idは残余条件 |
| id = 100のみ | 先頭列がないため逐次走査 |

先頭から等価条件が連続する間だけ後続列へ進み、最初の範囲条件までを二分探索します。
IS NULLはその列の等価条件として後続列の検索につなげられます。IS NOT NULLは範囲条件です。
OR/INやCAST・照合順序など、連続した範囲を確定できない列では絞り込みを止め、条件自体は正しく評価します。
広い候補範囲では単一列と同じ基準で逐次走査へ切り替えます。

単一列索引はv1形式、複合索引はv2形式で保存します。両形式の読み取りに対応します。

CSVはRFC 4180の引用符、`""`、引用符内のカンマ・CRLF/LF、末尾の改行なしに対応します。
レコード区切りにはCRLFと一般的なLFを受け付け、単独CR、不正な引用符、列数の違いを拒否します。
UTF-8 BOMはCSVの先頭だけ読み飛ばします。ヘッダー名は空と大文字小文字を無視した重複を拒否します。
ヘッダーなしCSVと固定長では`c1,c2,...`という列名になります。

固定長は改行区切りのないバイト列です。UTF-8のマルチバイト文字は列境界をまたげません。
文字コードは両形式ともUTF-8に限定します。EBCDIC、MS932、COMP-3などのバイナリー項目は未対応です。
最大4096列、1フィールド16MiB、1レコード64MiB。固定長の端数レコードは拒否します。
入力の空白、固定長末尾の空白、CSV引用符内改行は保持します。

## file_index_scan(index, verify := false)

索引に保存したスキーマで元ファイルを読みます。`CREATE VIEW`で通常のテーブルと同じように使えます。

- キーの`=,<,<=,>,>=`とANDによる範囲条件は、ディスク上の索引を二分探索して候補を絞ります。
- 小さい候補範囲はレコード位置へ直接移動します。4096件超かつ全体の25%超の候補範囲は、
  多数のランダムアクセスを避けるため元ファイルを順に読み、同じ条件を評価します。
- `IS NULL`、`IS NOT NULL`も二分探索に利用します。OR/INだけでは範囲を確定せず逐次走査する場合があります。
- 他の列の条件も評価します。CAST・関数・独自照合順序などの式はDuckDBで評価される場合があります。
- 条件なしでは元ファイルの全件走査です。結合・集約・ウィンドウ関数の計算はDuckDBが担当します。
- 必要列だけをDuckDBへ返します。ただし元ファイルのレコード取得と形式検証はレコード単位です。
- 結果順序は保証しません。順序が必要なSQLにはORDER BYを指定してください。

通常は索引作成時のサイズ・更新時刻を、バインド時、実行開始時、出力チャンクの前後で照合します。
変化した場合は自動で古い索引を使い続けず、再作成を求めるエラーにします。
元ファイルの更新後は`file_index_create(..., replace := true)`で再作成してください。

**サイズと更新時刻の両方を維持した書き換えは通常検査では検出できません。**
そのような更新がある運用では`verify := true`を指定してください。
この場合は元ファイル全体の64ビットFNV-1a照合も行うため、検索前に全件相当の読み取りが必要です。
FNVは破損・変更の検知用で、悪意のある改変に対する暗号学的な保証ではありません。
処理中は元ファイルを更新しない運用が前提で、ファイルをDBのトランザクションでロックする機能はありません。
更新時刻の検査だけで同時編集の完全なスナップショットを保証するものではありません。

## 実行計画と統計

EXPLAINには索引パス・元ファイル・キー列、通常のDuckDB計画にはFiltersが表示されます。
EXPLAIN ANALYZEでは実際に選んだアクセス方法、`Source records read`、`Source bytes read`を表示します。
`Index prefix columns`は実際に索引検索に使った先頭からの列数です。逐次走査時は0です。
`Index entries visited`は候補範囲で評価したエントリー数で、二分探索の比較回数は含めません。
これらは**データレコード数とそのバイト長の合計**です。ヘッダー・BOM・バッファ先読み・索引読取・
verifyの全ファイル照合・OSキャッシュは含めず、物理ディスクI/O量や全I/O量ではありません。
基数見積もりには元の行数を提供します。ヒストグラムや条件選択率の見積もりには対応していません。

## 保存・再作成

元ファイルは読み取り専用で開きます。ソートと索引の作成は、索引保存先と同じディレクトリに
作る専用`.fileindex-*`ディレクトリで行います。完成した索引だけを公開し、通常エラー時には一時領域を回収します。
初回作成では既存ファイルを上書きしません。置換失敗時には既存の索引を保持します。
作成中は索引の旧版と新版、ソート用領域、レコード位置一覧が同時に必要です。
`temp_limit`はソート用であり、完成索引・公開前ファイルを含めたディスク使用量全体の上限ではありません。
`memory_limit`もDuckDBの予算であり、拡張のレコードバッファ等を含むプロセス全体の厳密な上限ではありません。
実行前には**索引保存先の実際のドライブ**の空きを確認してください。
強制終了・電源断後に残る`.fileindex-*`は、構築処理が動いていないことを確認して削除できます。
電源断時の耐久性、オンライン更新、複数の同時再構築の順序制御は保証しません。

索引は元ファイルの絶対パスを持ち、ローカルOSの更新時刻表現を保存します。
元ファイルの移動やOS間移送時は再作成してください。索引単体に元レコードの内容は含みませんが、
キー値・列名・元ファイルのフルパスが含まれます。元ファイルと同等のアクセス権で管理してください。
DuckDBの`enable_external_access=false`では作成・読み取りを拒否します。
現在、DuckDBの個別allowed_paths/allowed_directoriesによる例外許可には対応していません。
対応DuckDB以外のバージョン・アーキテクチャ向けにビルドした拡張の読み込みはサポートしません。

## ビルド・検証

ビルド環境はPython 3.10以降、CMake 3.20以降、WindowsはVisual Studio 2022のC++ツール、LinuxはC++17コンパイラです。
初回に公式DuckDB 1.4.1のソースとCLI、Windowsではamalgamation、Linuxでは静的ライブラリを取得し、固定したSHA256と照合します。
両OSともDuckDBの必要部分を拡張へ静的リンクします。CLI使用時に別途duckdb.dll/libduckdb.soを配置する必要はありません。
取得物・ビルド結果・一時ファイルは既定ではリポジトリの`target/`以下です。

```sh
python tools/build.py --test
python tests/benchmark.py --duckdb target/fileindex-sdk/duckdb --extension target/fileindex-build/fileindex.duckdb_extension --rows 1000000 --work target/fileindex-benchmark
python tools/package.py --test
```

WindowsではCLIに`.exe`、拡張パスに`Release/`を追加します。LinuxはCMakeなしでも`build.py --direct --test`でビルドできます。
`--sdk`と`--build`で保存先を変更できます。ビルドと試験はDockerを使用しません。
CIは`.github/workflows/fileindex.yml`で独立させ、両OSの実CLI試験と配布用資材作成を行います。

[作例](demo/README.md) / [索引フォーマット](FORMAT.md) / [検証結果](VALIDATION.md)

## ライセンスと参考仕様

本拡張は[MIT License](LICENSE)で提供します。
DuckDBのヘッダー/APIを利用します。DuckDBはMITライセンスで、ビルド・配布時にそのライセンスを含めます。
ソースコードはsrc/、検証はtests/、ビルド・配布ツールはtools/、設計書はdocs/design.mdにあります。

- [DuckDB 1.4.1 TableFunction API](https://github.com/duckdb/duckdb/blob/v1.4.1/src/include/duckdb/function/table_function.hpp)
- [DuckDB 1.4.1 拡張メタデータ](https://github.com/duckdb/duckdb/blob/v1.4.1/scripts/append_metadata.cmake)
- [DuckDB C++拡張テンプレート](https://github.com/duckdb/extension-template)
