"""LangGraph wiring for the build loop.

    plan -> genui -> logic -> qa -+-> package -> END
                      ^           |
                      +--repair---+   (bounded by --max-repairs)

The repair edge goes back to `logic`, never to `genui`: QA diagnostics are fed to
the Logic subagent to patch the specific callback (CLAUDE.md §4). DESIGN.md is
never revisited — changes flow downstream only.
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
):
    graph = StateGraph(BuildState)

    graph.add_node("plan", make_planning_node(generator))
    graph.add_node("genui", make_genui_node(generator))
    graph.add_node("logic", make_logic_node(generator))
    graph.add_node("qa", make_qa_node(analyzer))
    graph.add_node("package", make_packaging_node(dry_run))

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
        {"package": "package", "repair": "logic", "fail": END},
    )
    graph.add_edge("package", END)

    return graph.compile()
