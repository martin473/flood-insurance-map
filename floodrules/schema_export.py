"""Turn a Pydantic model into a JSON Schema that strict tool use accepts.

Strict mode wants every object closed (additionalProperties: false) with all
properties listed as required, and it doesn't enforce min/max/length keywords.
To be safe across model versions this also inlines $refs and turns
Optional[X] (anyOf [X, null]) into a nullable type.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP = {"title", "default", "minimum", "maximum", "minLength", "maxLength",
         "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems"}


def tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    return _clean(_inline(raw, defs))


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline(merged, defs)
        return {k: _inline(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(v) for v in node]
    if not isinstance(node, dict):
        return node

    node = {k: v for k, v in node.items() if k not in _DROP}

    # Optional[X] -> X with a nullable type
    if "anyOf" in node:
        options = node["anyOf"]
        non_null = [o for o in options if o.get("type") != "null"]
        if len(non_null) == 1 and len(options) == 2:
            inner = dict(non_null[0])
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            inner.update(rest)
            t = inner.get("type")
            if isinstance(t, str):
                inner["type"] = [t, "null"]
            if "enum" in inner:
                inner["enum"] = list(inner["enum"]) + [None]
            node = inner

    node = {k: _clean(v) for k, v in node.items()}

    if node.get("type") == "object" or (isinstance(node.get("type"), list) and "object" in node["type"]):
        props = node.get("properties", {})
        node["additionalProperties"] = False
        node["required"] = list(props.keys())
    return node
