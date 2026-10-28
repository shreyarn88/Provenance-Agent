"""
Agent tools
-----------
Each function here is a "tool" the LLM agent can call to answer questions
about the book pipeline and its provenance.

Data sources (thesis_db):
  MongoDB     books_raw              raw scraped books (with run_id)
              provenance_events      pipeline events (scrape, ETL read/transform/deliver, integrity checks)
              federated_provenance   data handovers between teams
  PostgreSQL  books_clean            cleaned books loaded by the ETL
              finance_books          books received by the finance team

All database access is READ-ONLY. Every tool returns plain dicts/lists
so the results can be passed back to the LLM as JSON.
"""

import os
import re

import psycopg2
import psycopg2.extras
import pymongo

# ---------------------------------------------------------------------------
# Configuration (override with environment variables; never hard-code passwords)
# ---------------------------------------------------------------------------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.environ.get("MONGO_DB", "thesis_db")

PG_CONFIG = {
    "host": os.environ.get("PG_HOST", "localhost"),
    "port": int(os.environ.get("PG_PORT", 5432)),
    "database": os.environ.get("PG_DATABASE", "thesis_db"),
    "user": os.environ.get("PG_USER", "postgres"),
    "password": os.environ.get("PG_PASSWORD", ""),
}

MAX_ROWS = 50  # never send more than this many rows back to the LLM

_mongo_client = None


def _mongo():
    """Return the MongoDB database (connection is created once and reused)."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    return _mongo_client[MONGO_DB]


def _pg():
    """Open a READ-ONLY PostgreSQL connection."""
    conn = psycopg2.connect(**PG_CONFIG)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _clean(doc: dict) -> dict:
    """Remove MongoDB's internal _id so results are JSON-friendly."""
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


# ---------------------------------------------------------------------------
# Tool 1: overview of all data sources
# ---------------------------------------------------------------------------
def describe_data_sources() -> dict:
    """Return every collection/table with its record count and fields."""
    db = _mongo()
    mongo_info = {}
    for name in db.list_collection_names():
        sample = db[name].find_one() or {}
        mongo_info[name] = {
            "documents": db[name].count_documents({}),
            "fields": [k for k in sample.keys() if k != "_id"],
        }

    pg_info = {}
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        for table, column, dtype in cur.fetchall():
            pg_info.setdefault(table, {"columns": {}})["columns"][column] = dtype
        for table in pg_info:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            pg_info[table]["rows"] = cur.fetchone()[0]

    return {"mongodb": mongo_info, "postgresql": pg_info}


# ---------------------------------------------------------------------------
# Tool 2: search provenance events
# ---------------------------------------------------------------------------
def get_pipeline_events(run_id: str = None, event_type: str = None,
                        dataset: str = None, limit: int = MAX_ROWS) -> dict:
    """
    Find provenance events, optionally filtered by run_id, event_type,
    or a dataset name (matches input OR output dataset).
    """
    query = {}
    if run_id:
        query["run_id"] = run_id
    if event_type:
        query["event_type"] = event_type
    if dataset:
        query["$or"] = [{"input_dataset": dataset}, {"output_dataset": dataset}]

    db = _mongo()
    events = [
        _clean(e)
        for e in db.provenance_events.find(query).sort("timestamp", 1).limit(min(limit, MAX_ROWS))
    ]
    return {"filter": query, "count": len(events), "events": events}


# ---------------------------------------------------------------------------
# Tool 3: check whether every ETL execution finished properly
# ---------------------------------------------------------------------------
def check_etl_executions() -> dict:
    """
    Group ETL events into executions and flag problems.

    An execution starts at each 'read_completed' event. Because the
    transformation/delivery events do not carry a run_id, the following
    events are assigned to an execution by their order in time.
    """
    db = _mongo()
    events = db.provenance_events.find({"agent": "etl_pipeline"}).sort("timestamp", 1)

    executions = []
    current = None
    for e in events:
        if e.get("event_type") == "read_completed":
            current = {
                "started_at": e.get("timestamp"),
                "source_run_id": e.get("run_id"),
                "steps": [],
            }
            executions.append(current)
        if current is not None:
            current["steps"].append(e.get("event_type"))

    for number, ex in enumerate(executions, start=1):
        ex["execution_number"] = number
        ex["delivered"] = "delivery_completed" in ex["steps"]
        ex["integrity_checked"] = "integrity_check_completed" in ex["steps"]
        issues = []
        if not ex["delivered"]:
            issues.append("No delivery_completed event: the run may have failed "
                          "before loading data into books_clean.")
        if ex["delivered"] and not ex["integrity_checked"]:
            issues.append("Data was delivered to books_clean without an integrity check.")
        ex["issues"] = issues

    return {
        "total_executions": len(executions),
        "executions_with_issues": sum(1 for ex in executions if ex["issues"]),
        "executions": executions,
        "lineage_note": "transformation_completed and delivery_completed events have no "
                        "run_id; they were linked to their execution by timestamp order.",
    }


# ---------------------------------------------------------------------------
# Tool 4: trace where a dataset comes from
# ---------------------------------------------------------------------------
# The two provenance collections use different names for the same dataset.
DATASET_ALIASES = {
    "books_raw_mongodb": "books_raw",
    "finance_books_postgresql": "finance_books",
}


def _canonical(name):
    return DATASET_ALIASES.get(name, name)


def trace_lineage(dataset: str) -> dict:
    """
    Walk the provenance graph UPSTREAM from a dataset back to its origin.
    Uses both provenance_events and federated_provenance.
    """
    db = _mongo()
    dataset = _canonical(dataset)
    edges = {}  # (from, to) -> edge info

    def add_edge(src, dst, event, collection):
        src, dst = _canonical(src), _canonical(dst)
        key = (src, dst)
        if key not in edges:
            edges[key] = {
                "from": src,
                "to": dst,
                "event_type": event.get("event_type"),
                "tool": event.get("tool"),
                "agent": event.get("agent"),
                "collection": collection,
                "times_seen": 0,
                "first_seen": event.get("timestamp"),
                "last_seen": event.get("timestamp"),
            }
        edge = edges[key]
        edge["times_seen"] += 1
        edge["last_seen"] = max(edge["last_seen"] or "", event.get("timestamp") or "")

    for collection in ("provenance_events", "federated_provenance"):
        for e in db[collection].find({"output_dataset": {"$exists": True}}):
            src = e.get("input_dataset") or e.get("source_url")
            if src:
                add_edge(src, e["output_dataset"], e, collection)

    # Walk upstream (breadth-first) from the requested dataset
    path, frontier, visited = [], [dataset], set()
    while frontier:
        target = frontier.pop(0)
        if target in visited:
            continue
        visited.add(target)
        for (src, dst), info in edges.items():
            if dst == target:
                path.append(info)
                frontier.append(src)

    known = sorted({d for pair in edges for d in pair})
    return {
        "dataset": dataset,
        "found": bool(path),
        "upstream_steps": path,
        "known_datasets": known if not path else None,
    }


# ---------------------------------------------------------------------------
# Tool 5: run a read-only SQL query on PostgreSQL
# ---------------------------------------------------------------------------
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b",
    re.IGNORECASE,
)


def query_postgres(sql: str) -> dict:
    """Run a single SELECT query on books_clean / finance_books (read-only)."""
    statement = sql.strip().rstrip(";")
    if ";" in statement:
        return {"error": "Only one SQL statement is allowed."}
    if not re.match(r"^\s*(select|with)\b", statement, re.IGNORECASE):
        return {"error": "Only SELECT queries are allowed."}
    if _FORBIDDEN.search(statement):
        return {"error": "The query contains a forbidden keyword."}

    try:
        with _pg() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(statement)
            rows = cur.fetchmany(MAX_ROWS + 1)
    except psycopg2.Error as exc:
        return {"error": str(exc).strip()}

    truncated = len(rows) > MAX_ROWS
    rows = [{k: (str(v) if v is not None else None) for k, v in r.items()} for r in rows[:MAX_ROWS]]
    return {"row_count": len(rows), "truncated": truncated, "rows": rows}


# ---------------------------------------------------------------------------
# Tool 6: follow one book through every stage
# ---------------------------------------------------------------------------
def compare_book_across_stages(title: str) -> dict:
    """
    Show how one book looks in each stage: raw (MongoDB), clean and
    finance (PostgreSQL). Useful for spotting transformation issues
    and duplicates.
    """
    db = _mongo()
    pattern = {"$regex": f"^{re.escape(title)}$", "$options": "i"}
    raw = [_clean(d) for d in db.books_raw.find({"title": pattern}).limit(MAX_ROWS)]

    with _pg() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM books_clean WHERE LOWER(title) = LOWER(%s) LIMIT %s",
                    (title, MAX_ROWS))
        clean = [{k: str(v) for k, v in r.items()} for r in cur.fetchall()]
        cur.execute("SELECT * FROM finance_books WHERE LOWER(title) = LOWER(%s) LIMIT %s",
                    (title, MAX_ROWS))
        finance = [{k: str(v) for k, v in r.items()} for r in cur.fetchall()]

    return {
        "title": title,
        "raw_mongodb": {"count": len(raw), "records": raw},
        "books_clean": {"count": len(clean), "records": clean},
        "finance_books": {"count": len(finance), "records": finance},
    }


# ---------------------------------------------------------------------------
# Tool 7: team-to-team data handovers
# ---------------------------------------------------------------------------
def get_team_transfers(team: str = None) -> dict:
    """List data handovers between teams (sender or receiver = team)."""
    query = {}
    if team:
        query["$or"] = [{"team": team}, {"receiver_team": team}]
    db = _mongo()
    transfers = [_clean(t) for t in db.federated_provenance.find(query).sort("timestamp", 1)]
    return {"count": len(transfers), "transfers": transfers}