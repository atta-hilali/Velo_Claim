"""Clean Velo Claim package.

This package is the organized rebuild of the Velo Claim prototype.  The public
entrypoints are intentionally thin: reusable business modules live under
``context``, ``routing``, ``builders``, ``checks``, and ``fallback``; LangGraph
agents in ``agents`` only orchestrate those modules.
"""

from velo_claim.core.container import build_default_container

__all__ = ["build_default_container"]
