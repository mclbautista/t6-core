
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    code TEXT,
    code_form TEXT,
    rotation TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_holder_per_code ON projects (code_form)
    WHERE rotation = 'in' AND code_form IS NOT NULL;
CREATE TABLE IF NOT EXISTS bindings (
    system TEXT NOT NULL,
    page_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    bound_at REAL NOT NULL,
    active INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (system, page_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    at REAL NOT NULL,
    before TEXT,
    after TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    done_at REAL,
    dispatched_at REAL,
    error TEXT,
    error_final INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    page_id TEXT NOT NULL,
    project_id TEXT,
    detail TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_review_item ON review_items (kind, page_id)
    WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS requests (
    idempotency_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    project_id TEXT NOT NULL
);
