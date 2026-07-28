"""LangGraph wiring for the build loop.

    plan -> genui -> logic -> qa -+-> package -> END
             ^        ^           |
             +--------+--repair---+   (bounded by --max-repairs)

The repair edge routes by *diagnostic ownership*, not blindly to Logic: an error
anchored in `lib/ui/` can only be fixed by GenUI, which may write there. See
`src/ports/ownership.py` for the mapping and why it refines CLAUDE.md §4.
Planning output (pubspec.yaml, DESIGN.md) is frozen once GenUI starts, so
diagnostics against it fail the build rather than looping — changes flow
downstream, never upstream.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.graph.nodes import (
    make_genui_node,
    make_logic_node,
    make_packaging_node,
    make_planning_node,
    make_qa_node,
    make_router,
    route_after_agent,
)
from src.graph.state import BuildState
from src.ports.analyzer import DartAnalyzer
from src.ports.generator import CodeGenerator


def build_graph(
    generator: CodeGenerator,
    analyzer: DartAnalyzer,
    *,
    max_repairs: int = 3,
    dry_run: bool = True,
    test_runner=None,
    flutter_root: str | None = None,
):
    graph = StateGraph(BuildState)

    graph.add_node("plan", make_planning_node(generator))
    graph.add_node("genui", make_genui_node(generator))
    graph.add_node("logic", make_logic_node(generator))
    graph.add_node("qa", make_qa_node(analyzer, test_runner))
    graph.add_node("package", make_packaging_node(dry_run, flutter_root))

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "genui")

    # A subagent that writes outside its lane fails the build immediately.
    graph.add_conditional_edges(
        "genui", route_after_agent, {"continue": "logic", "fail": END}
    )
    graph.add_conditional_edges(
        "logic", route_after_agent, {"continue": "qa", "fail": END}
    )
    graph.add_conditional_edges(
        "qa",
        make_router(max_repairs),
        {
            "package": "package",
            "repair_ui": "genui",     # UI-owned diagnostics; flows on to logic
            "repair_logic": "logic",  # state-owned diagnostics
            "fail": END,
        },
    )
    graph.add_edge("package", END)

    return graph.compile()
