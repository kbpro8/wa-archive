"""Turning what someone types into an FTS5 query.

Shared by the CLI and the reader so that `wa-archive search kirim` and typing
`kirim` into the reader mean exactly the same thing.
"""

from __future__ import annotations

FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}

# Characters that mean the user is writing FTS syntax on purpose.
_FTS_SYNTAX = '"*():'


def build_fts_query(raw: str, exact: bool = False) -> str:
    """Turn a plain phrase into an FTS5 query with prefix matching.

    Indonesian attaches affixes to word stems (prospektus -> prospektusnya,
    kirim -> mengirimkan), so an exact-token match misses constantly. Bare
    terms get a trailing '*' unless --exact is passed or the user is already
    writing FTS syntax themselves.
    """
    if exact or any(ch in raw for ch in _FTS_SYNTAX):
        return raw
    terms = []
    for token in raw.split():
        if token.upper() in FTS_OPERATORS:
            terms.append(token.upper())
        else:
            # Strip punctuation that FTS5 would treat as syntax. Anything left
            # is a bare term, so a trailing '*' is always safe here.
            safe = "".join(ch for ch in token if ch.isalnum() or ch in "_-")
            if safe:
                terms.append(f"{safe}*")
    return " ".join(terms) or raw
