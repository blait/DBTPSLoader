-- 실제 워크로드의 read/write 문장 비율을 측정한다. 결과를 UI의 read_pct에 넣으면
-- 부하가 실제 비율을 재현한다 — 비율이 다르면 다른 워크로드를 측정하는 것이 된다.
--
-- 원본(운용) 인스턴스에서 실행할 것. 읽기 전용 조회다 (DMV 조회).
-- 읽기 = 모든 인덱스의 seek + scan + lookup, 쓰기 = update.
-- 주의: 카운터는 인스턴스 재시작 시 초기화된다 — sqlserver_start_time을 함께 볼 것.

SELECT sqlserver_start_time FROM sys.dm_os_sys_info;

WITH usage_stats AS (
    SELECT
        DB_NAME(database_id) AS db,
        SUM(user_seeks + user_scans + user_lookups) AS reads,
        SUM(user_updates) AS writes
    FROM sys.dm_db_index_usage_stats
    WHERE database_id = DB_ID()
    GROUP BY database_id
)
SELECT
    db,
    reads,
    writes,
    CAST(100.0 * reads / NULLIF(reads + writes, 0) AS decimal(5,1)) AS read_pct,
    CAST(100.0 * writes / NULLIF(reads + writes, 0) AS decimal(5,1)) AS write_pct
FROM usage_stats;

-- Per-table detail (top 30 by activity) — useful to validate the txn mix weights.
SELECT TOP 30
    OBJECT_NAME(s.object_id) AS table_name,
    SUM(s.user_seeks + s.user_scans + s.user_lookups) AS reads,
    SUM(s.user_updates) AS writes
FROM sys.dm_db_index_usage_stats s
WHERE s.database_id = DB_ID() AND OBJECTPROPERTY(s.object_id, 'IsUserTable') = 1
GROUP BY s.object_id
ORDER BY reads + writes DESC;
