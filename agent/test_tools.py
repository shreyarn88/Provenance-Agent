"""
Quick manual test for the agent tools (no AI involved yet).

Run from the project root:
    python -m agent.test_tools
"""

import json

from agent import tools


def show(title, result):
    print("=" * 80)
    print(title)
    print("=" * 80)
    print(json.dumps(result, indent=2, default=str)[:3000])  # cut very long output
    print()


if __name__ == "__main__":
    show("1. Data sources", tools.describe_data_sources())
    show("2. Events for the scrape run", tools.get_pipeline_events(run_id="run_20260620_141443"))
    show("3. ETL execution check", tools.check_etl_executions())
    show("4. Lineage of books_clean", tools.trace_lineage("books_clean"))
    show("5. SQL: duplicates in books_clean",
         tools.query_postgres(
             "SELECT title, COUNT(*) AS copies FROM books_clean "
             "GROUP BY title HAVING COUNT(*) > 1 ORDER BY copies DESC LIMIT 5"))
    show("6. SQL safety check (should be blocked)", tools.query_postgres("DELETE FROM books_clean"))
    show("7. One book across all stages", tools.compare_book_across_stages("A Light in the Attic"))
    show("8. Team transfers", tools.get_team_transfers())