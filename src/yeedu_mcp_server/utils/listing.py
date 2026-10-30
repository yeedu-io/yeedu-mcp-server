"""Telling "nothing matched" apart from "the query broke".

The listing endpoints answer an empty result with HTTP 404, so the client
raises and the tool reports an error. For a filtered listing that is the wrong
answer to the wrong question: asking which clusters are stuck in INITIATING and
being told "Error: ... (HTTP 404)" looks like a platform fault, when in fact it
is the all-clear. Someone checking whether anything is broken cannot act on a
result that renders a healthy fleet and an unreachable API identically.

Converting every 404 into an empty result would be worse. A listing filtered by
a reference -- `cluster_conf_id`, `cluster_conf_name` -- also 404s when that
reference does not exist, and reporting "no clusters match" for a config name
that was simply misspelled is a false all-clear. That is the one failure mode
worth engineering against here: an SRE who is told nothing is wrong stops
looking.

So the empty case is recognised by a positive match on how the API words it,
and anything else keeps raising:

    No cluster instances found.                              -> empty
    No cluster instance matches were found for ... 'x'.      -> empty
    No Spark job run were found within ... workspace id: 348 -> empty
    The provided cluster config id: 999999 is not found ...  -> error

An unfamiliar 404 therefore surfaces as an error rather than being quietly
read as "nothing to see", which is the direction a mistake should fall.
"""

import re
from typing import Any, Dict, Optional

#: How the API words an empty listing: a message that opens with "No" and
#: reports something not found. The reference-not-found messages open with
#: "The provided", and "not found" does not match `^no\b` because the word
#: boundary rejects the "t" in "not".
_EMPTY_LISTING = re.compile(r"^no\b.*\bfound\b", re.I | re.S)

_PREFIX = "Yeedu API error:"


def api_detail(exc: BaseException) -> str:
    """The API's own message, without the client's wrapper and HTTP suffix."""
    text = str(exc)
    _, found, rest = text.partition(_PREFIX)
    if found:
        text = rest
    return re.sub(r"\(HTTP \d+\)\s*$", "", text).strip()


def is_empty_listing(exc: BaseException) -> bool:
    """True when this 404 means the filters matched nothing."""
    if getattr(exc, "status_code", None) != 404:
        return False
    return bool(_EMPTY_LISTING.match(api_detail(exc)))


def describe_filters(filters: Dict[str, Any]) -> str:
    """Render the filters that were actually sent, for an empty answer to cite.

    An all-clear is only worth anything if it says what it asked. Paging
    parameters are dropped: they are not part of the question.
    """
    skip = {"pageNumber", "limit", "page"}
    parts = []
    for key, value in filters.items():
        if key in skip or value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            value = ", ".join(str(item) for item in value)
        parts.append(f"{key}={value}")
    return "; ".join(parts) if parts else "no filters"


def no_rows(subject: str, filters: Dict[str, Any], hint: Optional[str] = None) -> str:
    """Phrase an empty result as a result.

    Says the query ran, says what it asked, and says the count was zero, so it
    cannot be mistaken either for a failure or for a wider all-clear than the
    filters support.
    """
    out = (f"No {subject} matched. The query ran and returned 0 rows; this is an "
           f"empty result, not a failed call.\n\nFilters applied: "
           f"{describe_filters(filters)}")
    if hint:
        out += f"\n\n{hint}"
    return out


def validate_enum(values, allowed, label: str, hint: str = "") -> Optional[str]:
    """Reject unknown filter values before the API does, or return None.

    `express-openapi-validator` answers an out-of-enum value with a 400 that
    fails the entire request, so one bad status discards every other filter
    sent with it. Checked here, the caller is told which value was wrong and
    what the accepted set is, which is what it needed in order to retry.
    """
    unknown = [v for v in values if str(v).upper() not in allowed]
    if not unknown:
        return None
    out = (f"Error: unknown {label} "
           f"{', '.join(repr(str(v)) for v in unknown)}.\n"
           f"Hint: valid values are {', '.join(sorted(allowed))}.")
    return out + (f" {hint}" if hint else "")
