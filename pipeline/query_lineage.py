import sys
import lineage_store


def trace_lineage(entity_name: str) -> list:
    chain = []
    current = entity_name
    visited = set()

    while current:
        if current in visited:
            chain.append({"entity": current, "note": "cycle detected — stopped"})
            break
        visited.add(current)

        record = lineage_store.get_record(current)
        if not record:
            # Reached the ultimate origin (e.g. a web URL with no provenance record)
            chain.append({"entity": current, "is_origin": True})
            break

        chain.append(record)
        current = record.get("wasDerivedFrom")

    return chain


def print_lineage(entity_name: str) -> None:
    """Print a human-readable lineage chain for the given entity."""
    chain = trace_lineage(entity_name)

    print()
    print(f"  Lineage query: Where did  '{entity_name}'  come from?")
    print("  " + "─" * 55)

    for i, record in enumerate(chain, start=1):
        pad = "  "

        if record.get("is_origin"):
            print(f"\n{pad}[{i}] {record['entity']}")
            print(f"{pad}     ★  Origin — web source, no further provenance")
            continue

        if record.get("note"):
            print(f"\n{pad}[{i}] {record['entity']}  ⚠  {record['note']}")
            continue

        print(f"\n{pad}[{i}] {record['entity']}")
        print(f"{pad}     Activity  : {record.get('wasGeneratedBy')}  (tool: {record.get('tool')})")
        print(f"{pad}     Agent     : {record.get('wasAttributedTo')}")
        print(f"{pad}     Timestamp : {record.get('generatedAtTime')}")

        # Tool-specific details — shown when available
        if record.get("source_url"):
            print(f"{pad}     Source URL: {record['source_url']}")
        if record.get("spider_name"):
            print(f"{pad}     Spider    : {record['spider_name']}  (job: {record.get('job_id')})")
        if record.get("records_collected"):
            print(f"{pad}     Collected : {record['records_collected']} records")
        if record.get("storage_location"):
            print(f"{pad}     Stored at : {record['storage_location']}")
        if record.get("documents_written"):
            print(f"{pad}     Written   : {record['documents_written']} documents")
        if record.get("pipeline_name"):
            print(f"{pad}     ADF Pipeline : {record['pipeline_name']} → {record.get('activity_name')}")
        if record.get("transformation_description"):
            print(f"{pad}     Transform : {record['transformation_description']}")
        if record.get("records_output"):
            print(f"{pad}     Output    : {record['records_output']} records")
        if record.get("destination"):
            print(f"{pad}     Delivered : {record['destination']}")
        if record.get("table"):
            print(f"{pad}     Table     : {record.get('schema')}.{record['table']}  ({record.get('rows_inserted')} rows)")

        derived = record.get("wasDerivedFrom")
        if derived:
            print(f"{pad}     ↑ derived from: {derived}")

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\nUsage: python query_lineage.py <entity_name>")
        print("\nStored entities:")
        for r in lineage_store.all_records():
            print(f"  - {r['entity']}")
    else:
        print_lineage(sys.argv[1])