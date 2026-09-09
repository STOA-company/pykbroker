import ast
from pathlib import Path

from pykbroker.contracts import execution


def test_execution_contract_has_no_function_logic():
    tree = ast.parse(Path(execution.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert len(node.body) == 1
            statement = node.body[0]
            assert isinstance(statement, ast.Raise)
            assert isinstance(statement.exc, ast.Name) and statement.exc.id == "NotImplementedError"
        assert not isinstance(node, (ast.For, ast.While, ast.Await))


def test_context_surface_complete():
    expected = {
        "target_qty",
        "remaining",
        "filled_qty",
        "filled_avg",
        "qty_for_krw",
        "split",
        "round_price",
        "held",
        "last",
        "best_bid",
        "best_ask",
        "prev_close",
        "vwap_today",
        "bars",
        "now",
        "session",
        "wait",
        "wait_until",
        "wait_for",
        "place_limit",
        "place_market",
        "cancel",
        "cancel_open",
        "note",
        "done",
    }
    assert expected <= set(vars(execution.ExecutionContext))
