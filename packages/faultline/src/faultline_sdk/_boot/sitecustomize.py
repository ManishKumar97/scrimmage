"""Faultline's startup file. faultline-run puts this folder first on PYTHONPATH.

It does nothing unless FAULTLINE_TOKEN is set; then it switches the SDK on. Either
way it then runs the next sitecustomize.py on the path, which this one hid, so the
program starts exactly as it would without Faultline.
"""

import importlib.machinery
import importlib.util
import os
import sys


def _chain() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


if os.environ.get("FAULTLINE_TOKEN"):
    try:
        from faultline_sdk import bootstrap
    except ImportError:
        bootstrap = None  # another Python without Faultline: do nothing, say nothing
    if bootstrap is not None:
        bootstrap.start()
_chain()
