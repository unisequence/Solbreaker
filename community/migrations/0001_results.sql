CREATE TABLE benchmark_results (
    sequence INTEGER PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload TEXT NOT NULL
);
