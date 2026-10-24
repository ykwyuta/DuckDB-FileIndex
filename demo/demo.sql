-- Start: duckdb -unsigned, then LOAD '/absolute/path/fileindex.duckdb_extension';
-- Run from this directory. The original CSV is never rewritten.
SELECT * FROM file_index_create('customers.csv', 'customers.yyidx', key_type := 'BIGINT');
CREATE VIEW customers AS SELECT * FROM file_index_scan('customers.yyidx');
SELECT * FROM customers WHERE id = 2;
SELECT * FROM customers WHERE id BETWEEN 1 AND 2 ORDER BY id, name;
SELECT region, count(*) FROM customers GROUP BY region;
EXPLAIN ANALYZE SELECT name FROM customers WHERE id = 2;

-- Composite index: region equality + numeric id equality/range.
SELECT * FROM file_index_create('customers.csv', 'customers-region-id.yyidx',
  key_columns := [3,1], key_types := ['VARCHAR','BIGINT']);
CREATE VIEW customers_region_id AS SELECT * FROM file_index_scan('customers-region-id.yyidx');
SELECT * FROM customers_region_id WHERE region = '東京' AND id BETWEEN 1 AND 3;
EXPLAIN ANALYZE SELECT name FROM customers_region_id WHERE region = '東京' AND id = 2;
