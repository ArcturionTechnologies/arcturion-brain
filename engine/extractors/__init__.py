"""Extractors map file types → node dicts + extracted-edge dicts."""

from .markdown import extract_markdown
from .code import extract_code
from .plist import extract_plist
from .config_file import extract_config

__all__ = ["extract_markdown", "extract_code", "extract_plist", "extract_config", "route"]


def route(path, family, node_type):
    """Dispatch to the right extractor based on family."""
    if family == "document":
        return extract_markdown(path, node_type)
    if family == "automation":
        return extract_plist(path, node_type)
    if family == "code":
        if path.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini"} or path.name.endswith(".env.tpl"):
            return extract_config(path, node_type)
        return extract_code(path, node_type)
    # Fallback: produce a bare node with no extracted edges
    from .markdown import bare_node
    return bare_node(path, family, node_type), []
