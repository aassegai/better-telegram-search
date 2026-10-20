CREATE TABLE index_rate_samples (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    lane TEXT NOT NULL,
    batch_size INTEGER NOT NULL,
    variant TEXT NOT NULL,
    observed_at REAL NOT NULL,
    work REAL NOT NULL CHECK(work>0),
    seconds REAL NOT NULL CHECK(seconds>0)
);
CREATE INDEX index_rate_samples_window ON index_rate_samples(kind,identity,lane,id);
INSERT INTO schema_migrations(version) VALUES(14);
