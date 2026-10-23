import importlib
import pkgutil

import agent


def test_every_agent_module_imports():
    """The workflow steps and the webhook import these modules at start. A syntax error in any of them (even one only
    reached from a function other tests do not call) makes every investigation fail at its first step."""
    names = [m.name for m in pkgutil.iter_modules(agent.__path__, prefix="agent.")]
    assert "agent.activities" in names and "agent.steps" in names and "agent.webhook" in names
    for name in names:
        importlib.import_module(name)
