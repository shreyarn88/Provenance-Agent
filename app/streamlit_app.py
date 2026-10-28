"""
Web UI for the provenance agent.

Run from the project root:
    streamlit run app/streamlit_app.py
"""

import sys
from pathlib import Path

# Make the project root importable (Streamlit only adds the app/ folder)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from agent import tools
from agent.agent import MODEL, PROVIDER, run_agent

st.set_page_config(page_title="Provenance Agent", page_icon="🔎", layout="wide")

EXAMPLES = [
    "Did every ETL run finish successfully?",
    "Where does the data in finance_books come from?",
    "Does the finance team receive cleaned data? Compare 'A Light in the Attic' across all stages.",
    "Were all CSV exports to the finance team actually imported?",
    "Are there duplicate titles in books_clean?",
]

# How each dataset is drawn in the lineage graph: (store type, colour)
NODE_STYLE = {
    "books_raw": ("MongoDB", "#d8f3dc"),
    "books_in_memory": ("ETL step", "#fff3bf"),
    "books_transformed": ("ETL step", "#fff3bf"),
    "books_clean": ("PostgreSQL", "#d0ebff"),
    "team_data_transfer_csv": ("CSV file", "#ffe3e3"),
    "finance_books": ("PostgreSQL", "#d0ebff"),
}


@st.cache_data(ttl=60)
def load_lineage(dataset: str) -> dict:
    return tools.trace_lineage(dataset)


@st.cache_data(ttl=60)
def load_etl() -> dict:
    return tools.check_etl_executions()


def lineage_dot(steps: list) -> str:
    """Turn trace_lineage() steps into a Graphviz DOT graph."""
    lines = [
        "digraph G {",
        "rankdir=LR;",
        'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];',
        'edge [fontname="Helvetica", fontsize=9];',
    ]
    nodes = {s["from"] for s in steps} | {s["to"] for s in steps}
    for name in sorted(nodes):
        if name in NODE_STYLE:
            kind, colour = NODE_STYLE[name]
        elif name.startswith("http"):
            kind, colour = "Website", "#e5dbff"
        else:
            kind, colour = "Dataset", "#f1f3f5"
        lines.append(f'"{name}" [label="{name}\\n({kind})", fillcolor="{colour}"];')

    for s in steps:
        label = f'{s.get("tool") or ""}\\n{s.get("event_type") or ""}\\nx{s.get("times_seen", 1)}'
        attrs = f'label="{label}"'
        # Highlight the governance issue: finance export reads raw, uncleaned data
        if s["from"] == "books_raw" and s["to"] == "team_data_transfer_csv":
            attrs += ', color="#e03131", fontcolor="#e03131", penwidth=2'
        lines.append(f'"{s["from"]}" -> "{s["to"]}" [{attrs}];')

    lines.append("}")
    return "\n".join(lines)


def show_evidence(tool_calls: list) -> None:
    if tool_calls:
        with st.expander(f"Evidence: {len(tool_calls)} tool call(s)"):
            for call in tool_calls:
                st.code(f'{call["tool"]}({call["arguments"]})', language="python")


if "messages" not in st.session_state:
    st.session_state.messages = []

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🔎 Provenance Agent")
    st.caption(f"LLM: {PROVIDER} · {MODEL}")
    st.markdown(
        "Ask questions about the book scraping pipeline. Answers are grounded in "
        "W3C PROV-O provenance records, using read-only tools."
    )

    st.subheader("Try a question")
    for example in EXAMPLES:
        if st.button(example):
            st.session_state.pending = example

    if st.button("🗑️ Clear chat"):
        st.session_state.messages = []

    st.subheader("Pipeline health")
    try:
        etl = load_etl()
        col1, col2 = st.columns(2)
        col1.metric("ETL runs", etl["total_executions"])
        col2.metric("With issues", etl["executions_with_issues"])
    except Exception as exc:
        st.error(f"Database not reachable: {exc}")

chat_tab, lineage_tab = st.tabs(["💬 Chat", "🧬 Lineage & ETL health"])

# ---------------------------------------------------------------- chat
with chat_tab:
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            show_evidence(msg.get("tool_calls"))

    question = st.chat_input("Ask about the pipeline, lineage or data quality...")
    question = question or st.session_state.pop("pending", None)

    if question:
        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Checking provenance records..."):
                result = run_agent(question, verbose=False, history=history)
            answer = result["answer"] or ""
            st.markdown(answer)
            show_evidence(result["tool_calls"])

        st.session_state.messages.append(
            {"role": "assistant", "content": answer, "tool_calls": result["tool_calls"]}
        )
        st.rerun()

# ---------------------------------------------------------------- lineage
with lineage_tab:
    dataset = st.selectbox("Trace a dataset back to its source",
                           ["finance_books", "books_clean", "books_raw"])
    try:
        lineage = load_lineage(dataset)
        if lineage["found"]:
            st.graphviz_chart(lineage_dot(lineage["upstream_steps"]))
            if dataset == "finance_books":
                st.caption("Red arrow: the CSV export to the finance team reads from books_raw, "
                           "so finance receives uncleaned data that bypasses the ETL.")
        else:
            st.info("No lineage found for this dataset.")

        st.subheader("ETL executions")
        etl = load_etl()
        st.dataframe(
            [
                {
                    "#": e["execution_number"],
                    "Started (UTC)": (e["started_at"] or "")[:19].replace("T", " "),
                    "Delivered": "✅" if e["delivered"] else "❌",
                    "Integrity check": "✅" if e["integrity_checked"] else "❌",
                    "Issues": "; ".join(e["issues"]) or "—",
                }
                for e in etl["executions"]
            ],
            hide_index=True,
            column_config={"Issues": st.column_config.TextColumn(width="large")},
        )
        st.caption(etl["lineage_note"])
    except Exception as exc:
        st.error(f"Could not load provenance data: {exc}")
