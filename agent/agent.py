"""
Provenance Agent
----------------
An LLM agent that answers questions about the book pipeline by calling
the read-only tools in agent/tools.py.

LLM backend (set in .env):
    LLM_PROVIDER=groq     free cloud API (needs GROQ_API_KEY)
    LLM_PROVIDER=ollama   local model via Ollama (no internet needed)
    LLM_MODEL=...         optional: override the default model

Usage (from the project root):
    python -m agent.agent                      # interactive chat
    python -m agent.agent "Did every ETL run finish?"
"""

import inspect
import json
import os
import sys

from openai import APIError, OpenAI, RateLimitError

from agent import tools

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key_env": None,
        "default_model": "qwen2.5:3b",
    },
}

PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()
if PROVIDER not in PROVIDERS:
    sys.exit(f"Unknown LLM_PROVIDER '{PROVIDER}'. Use one of: {', '.join(PROVIDERS)}")
MODEL = os.environ.get("LLM_MODEL", PROVIDERS[PROVIDER]["default_model"])

MAX_STEPS = 8              # max rounds of tool calls before the agent must answer
MAX_RESULT_CHARS = 6000    # keep tool results small

SYSTEM_PROMPT = """You are a data provenance assistant for a book web-scraping pipeline.

The pipeline:
  books.toscrape.com --(Scrapy)--> books_raw (MongoDB)
  --(ETL read, transform, deliver)--> books_clean (PostgreSQL)
  Separately, the marketing team exports books to a CSV that the finance team
  imports into finance_books (PostgreSQL). These handovers are in team transfers.

Rules:
- ALWAYS use the tools to look up facts. Never invent data, counts, dates or names.
- If a question needs several facts, call several tools.
- For SQL, only write SELECT queries on books_clean or finance_books.
  books_clean.price is numeric; finance_books.price is text like '£51.77'.
- In your answer, state the evidence: which dataset, run_id, event or timestamp it comes from.
- If asked to change, delete or insert data, refuse and explain that you have read-only access.
- If the tools do not contain the answer, say so clearly.
- Keep answers short and clear."""

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "describe_data_sources",
        "description": "List all MongoDB collections and PostgreSQL tables with record counts and fields.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_pipeline_events",
        "description": "Search pipeline provenance events (scraping, ETL read/transform/delivery, "
                       "integrity checks). All filters are optional.",
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "e.g. run_20260620_141443"},
            "event_type": {"type": "string", "description": "e.g. scraping_completed, read_completed, "
                           "transformation_completed, delivery_completed, integrity_check_completed"},
            "dataset": {"type": "string", "description": "dataset name, e.g. books_raw"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "check_etl_executions",
        "description": "Check every ETL execution: whether it delivered data and was integrity-checked, "
                       "and list any issues.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "trace_lineage",
        "description": "Trace where a dataset comes from, step by step back to its original source.",
        "parameters": {"type": "object", "properties": {
            "dataset": {"type": "string", "description": "books_raw, books_clean or finance_books"}},
            "required": ["dataset"]}}},
    {"type": "function", "function": {
        "name": "query_postgres",
        "description": "Run ONE read-only SELECT query on PostgreSQL tables books_clean "
                       "(id, title, price numeric, rating, source_url, run_id, scraped_at, loaded_at) "
                       "or finance_books (id, title, price text, rating, source_url, received_at).",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "a SELECT statement"}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "compare_book_across_stages",
        "description": "Show one book in every stage: raw (MongoDB), books_clean and finance_books.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "exact book title"}},
            "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "get_team_transfers",
        "description": "List data handovers between teams (CSV exports and imports), optionally for one team.",
        "parameters": {"type": "object", "properties": {
            "team": {"type": "string", "description": "marketing_team or finance_team"}},
            "required": []}}},
]

TOOL_FUNCTIONS = {
    "describe_data_sources": tools.describe_data_sources,
    "get_pipeline_events": tools.get_pipeline_events,
    "check_etl_executions": tools.check_etl_executions,
    "trace_lineage": tools.trace_lineage,
    "query_postgres": tools.query_postgres,
    "compare_book_across_stages": tools.compare_book_across_stages,
    "get_team_transfers": tools.get_team_transfers,
}


def make_client() -> OpenAI:
    """Create an OpenAI-compatible client for the chosen provider."""
    cfg = PROVIDERS[PROVIDER]
    if cfg["api_key_env"]:
        key = os.environ.get(cfg["api_key_env"])
        if not key:
            sys.exit(f"Missing {cfg['api_key_env']} in your .env file.")
    else:
        key = "ollama"  # Ollama ignores the key, but the client needs one
    return OpenAI(base_url=cfg["base_url"], api_key=key)


def call_tool(name: str, arguments: dict) -> str:
    """Run one tool and return its result as a (shortened) JSON string."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return json.dumps({"error": f"Unknown tool '{name}'."})
    allowed = inspect.signature(func).parameters
    arguments = {k: v for k, v in (arguments or {}).items() if k in allowed}
    try:
        result = func(**arguments)
    except TypeError as exc:
        result = {"error": f"Wrong arguments for {name}: {exc}"}
    except Exception as exc:  # database down, etc.
        result = {"error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(result, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " ... [result truncated]"
    return text


def run_agent(question: str, verbose: bool = True) -> dict:
    """Answer one question; return the answer plus the tool calls made."""
    client = make_client()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace = []

    for _ in range(MAX_STEPS):
        try:
            response = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOL_SCHEMAS, temperature=0,
            )
        except RateLimitError:
            return {"answer": "Free-tier rate limit reached. Wait a minute and try again.",
                    "tool_calls": trace}
        except APIError as exc:
            return {"answer": f"LLM error: {exc}", "tool_calls": trace}

        message = response.choices[0].message
        if not message.tool_calls:
            return {"answer": message.content, "tool_calls": trace}

        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in message.tool_calls
            ],
        })

        for c in message.tool_calls:
            try:
                args = json.loads(c.function.arguments or "{}") or {}
            except json.JSONDecodeError:
                args = {}
            if verbose:
                print(f"  -> calling {c.function.name}({json.dumps(args)})")
            result = call_tool(c.function.name, args)
            trace.append({"tool": c.function.name, "arguments": args})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})

    return {"answer": "I could not finish within the step limit. Try a more specific question.",
            "tool_calls": trace}


def main():
    if len(sys.argv) > 1:
        print(run_agent(" ".join(sys.argv[1:]))["answer"])
        return
    print(f"Provenance agent ({PROVIDER}: {MODEL}). Type a question, or 'exit' to quit.\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in {"exit", "quit"}:
            break
        if question:
            print("\nAgent:", run_agent(question)["answer"], "\n")


if __name__ == "__main__":
    main()
