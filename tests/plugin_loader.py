"""Load the checkout under a stable package name, regardless of folder name."""
import importlib.util
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
if "blendboost_test_plugin" not in sys.modules:
    spec = importlib.util.spec_from_file_location("blendboost_test_plugin", root / "__init__.py",
                                                 submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
plugin = sys.modules["blendboost_test_plugin"]
