import os
import pymongo
import psycopg2
import hashlib
import json
from datetime import datetime, timezone

# ── Hashing / Integrity Verification ─────────────────────────────────────────
def generate_hash(data: list) -> str:
    
    sorted_data = sorted(data, key=lambda x: x.get("title", ""))
    # Only hash stable fields
    stable_fields = ["title", "rating", "source_url", "run_id"]
    clean_data = [
        {k: v for k, v in record.items() if k in stable_fields}
        for record in sorted_data
    ]
    data_string = json.dumps(clean_data, sort_keys=True, default=str)
    hash_value = hashlib.sha256(data_string.encode()).hexdigest()
    return hash_value


def verify_integrity(hash_before: str, hash_after: str) -> bool:
    """
    Compare two hashes to verify data integrity.
    Returns True if hashes match, False if tampering detected.
    """
    return hash_before == hash_after

# ── Configuration ─────────────────────────────────────────────────────────────
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB = "thesis_db"
MONGO_COLLECTION = "books_raw"

PG_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "thesis_db",
    "user": "postgres",
    "password": os.environ.get("PG_PASSWORD", ""),
}

#  Read from MongoDB 
def read_from_mongodb():
    print("\n[ETL] Stage 1: Reading from MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    books = list(db[MONGO_COLLECTION].find({}, {"_id": 0}))
    print(f"[ETL] Read {len(books)} books from MongoDB")

    # Capture provenance event
    event = {
        "tool": "MongoDB",
        "event_type": "read_completed",
        "run_id": books[0].get("run_id") if books else "unknown",
        "input_dataset": "books_raw",
        "output_dataset": "books_in_memory",
        "records_read": len(books),
        "source": f"mongodb://localhost:27017/{MONGO_DB}/{MONGO_COLLECTION}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "etl_pipeline",
    }
    db["provenance_events"].insert_one(event)
    print(f"[Provenance] MongoDB read event captured")
    client.close()
    return books

# Transform 
def transform(books):
    print("\n[ETL] Stage 2: Transforming data...")
    transformed = []
    skipped = 0

    for book in books:
        try:
            # Clean price: remove £ symbol and convert to float
            raw_price = book.get("price", "£0.00")
            price = float(raw_price.replace("£", "").strip())

            # Normalize rating: extract word from "star-rating Three"
            raw_rating = book.get("rating", "star-rating Unknown")
            rating = raw_rating.replace("star-rating ", "").strip()

            transformed.append({
                "title": book.get("title", "Unknown"),
                "price": price,
                "rating": rating,
                "source_url": book.get("url", ""),
                "run_id": book.get("run_id", ""),
                "scraped_at": book.get("scraped_at", ""),
                "loaded_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            skipped += 1

    print(f"[ETL] Transformed {len(transformed)} books, skipped {skipped}")

    # Capture provenance event
    client = pymongo.MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    event = {
        "tool": "PythonETL",
        "event_type": "transformation_completed",
        "input_dataset": "books_in_memory",
        "output_dataset": "books_transformed",
        "records_input": len(books),
        "records_output": len(transformed),
        "records_skipped": skipped,
        "transformations": "price cleaned to float, rating normalized to word",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "etl_pipeline",
    }
    db["provenance_events"].insert_one(event)
    print(f"[Provenance] Transformation event captured")
    client.close()
    return transformed

#  Write to PostgreSQL 
def write_to_postgres(books):
    print("\n[ETL] Stage 3: Writing to PostgreSQL...")
    conn = psycopg2.connect(**PG_CONFIG)
    cur = conn.cursor()

    # Create table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS books_clean (
            id SERIAL PRIMARY KEY,
            title TEXT,
            price NUMERIC(10,2),
            rating TEXT,
            source_url TEXT,
            run_id TEXT,
            scraped_at TEXT,
            loaded_at TEXT
        )
    """)

    # Insert books
    inserted = 0
    for book in books:
        cur.execute("""
            INSERT INTO books_clean
            (title, price, rating, source_url, run_id, scraped_at, loaded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            book["title"], book["price"], book["rating"],
            book["source_url"], book["run_id"],
            book["scraped_at"], book["loaded_at"]
        ))
        inserted += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"[ETL] Inserted {inserted} books into PostgreSQL books_clean table")

    # Capture provenance event
    client = pymongo.MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    event = {
        "tool": "PostgreSQL",
        "event_type": "delivery_completed",
        "input_dataset": "books_transformed",
        "output_dataset": "books_clean",
        "rows_inserted": inserted,
        "destination": "postgresql://localhost:5432/thesis_db/books_clean",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "etl_pipeline",
    }
    db["provenance_events"].insert_one(event)
    print(f"[Provenance] PostgreSQL delivery event captured")
    client.close()

#  Main 
def main():
    print("=" * 50)
    print("  ETL Pipeline with Provenance Capture")
    print("=" * 50)

    # Stage 1: Read from MongoDB
    books = read_from_mongodb()

    # Generate hash of raw MongoDB data
    hash_mongodb = generate_hash(books)
    print(f"\n[Integrity] MongoDB data hash: {hash_mongodb[:16]}...")

    # Stage 2: Transform
    transformed = transform(books)

    # Generate hash of transformed data
    hash_transformed = generate_hash(transformed)
    print(f"[Integrity] Transformed data hash: {hash_transformed[:16]}...")

    # Stage 3: Write to PostgreSQL
    write_to_postgres(transformed)

    # Read back from PostgreSQL to verify integrity
    print("\n[Integrity] Verifying data integrity...")
    conn = psycopg2.connect(**PG_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT title, price::text, rating, source_url, run_id, scraped_at, loaded_at
        FROM books_clean
        ORDER BY title
    """)
    pg_books = [
        {
            "title": row[0], "price": row[1], "rating": row[2],
            "source_url": row[3], "run_id": row[4],
            "scraped_at": row[5], "loaded_at": row[6]
        }
        for row in cur.fetchall()
    ]
    cur.close()
    conn.close()

    hash_postgres = generate_hash(pg_books)
    print(f"[Integrity] PostgreSQL data hash: {hash_postgres[:16]}...")

    # Store integrity verification in provenance
    client = pymongo.MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    integrity_event = {
        "tool": "IntegrityVerifier",
        "event_type": "integrity_check_completed",
        "hash_mongodb_raw": hash_mongodb,
        "hash_transformed": hash_transformed,
        "hash_postgresql": hash_postgres,
        "integrity_verified": verify_integrity(hash_transformed, hash_postgres),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "etl_pipeline",
    }
    db["provenance_events"].insert_one(integrity_event)
    client.close()

    # Print result
    if verify_integrity(hash_transformed, hash_postgres):
        print("\n✓ INTEGRITY VERIFIED — Data matches between transformation and PostgreSQL")
    else:
        print("\n✗ INTEGRITY FAILED — Data mismatch detected!")

    print("\n[ETL] Pipeline complete!")
    print("[ETL] Check MongoDB provenance_events for full lineage.")

if __name__ == "__main__":
    main()