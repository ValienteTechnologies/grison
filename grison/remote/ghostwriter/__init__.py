"""Read-only Ghostwriter GraphQL client.

Ghostwriter exposes a Hasura endpoint at ``{gw_url}/v1/graphql``. GW (and
BookStack) sit behind Cloudflare Access, so every request carries the bearer
token plus the CF service-token header pair (see :mod:`grison.remote.creds`).

Package layout: :mod:`.queries` holds every GraphQL document constant,
:mod:`.errors` holds :class:`GhostwriterError`, and :mod:`.client` holds
:class:`GhostwriterClient` itself. Everything is re-exported here so
``from grison.remote.ghostwriter import GhostwriterClient`` (and friends)
keeps working, and so this package's own ``vars()`` still exposes every
``_..._QUERY``/``_..._MUTATION`` constant the way
:mod:`grison.remote.compat` and ``tests/test_gw_schema_conformance.py``
expect (both do ``import grison.remote.ghostwriter as gw_module`` and scan
``vars(gw_module)`` for that naming convention — see
:mod:`.queries`'s own docstring).
"""

from __future__ import annotations

from .client import GhostwriterClient
from .errors import GhostwriterError
from .queries import (
    _CONTENT_TYPES_QUERY,
    _DELETE_EVIDENCE_MUTATION,
    _DELETE_FINDING_MUTATION,
    _DELETE_PROJECT_NOTE_MUTATION,
    _DELETE_REPORTED_FINDING_MUTATION,
    _DOWNLOAD_EVIDENCE_QUERY,
    _EVIDENCE_BY_PK_QUERY,
    _EVIDENCE_QUERY,
    _EXTRA_FIELD_SPEC_QUERY,
    _FINDING_BY_PK_QUERY,
    _FINDING_QUERY,
    _FINDING_SEVERITY_QUERY,
    _FINDING_TYPE_LOOKUP_QUERY,
    _FINGERPRINT_PROBE,
    _INSERT_FINDING_MUTATION,
    _INSERT_PROJECT_NOTE_MUTATION,
    _INSERT_REPORTED_FINDING_MUTATION,
    _PROJECT_NOTE_BY_PK_QUERY,
    _REPORT_BY_PK_QUERY,
    _REPORT_QUERY,
    _REPORTED_FINDING_BY_PK_QUERY,
    _REPORTED_FINDING_QUERY,
    _SET_TAGS_MUTATION,
    _TAGGED_ITEM_FOR_QUERY,
    _TAGGED_ITEM_QUERY,
    _UPDATE_EVIDENCE_MUTATION,
    _UPDATE_FINDING_MUTATION,
    _UPDATE_REPORT_MUTATION,
    _UPDATE_REPORTED_FINDING_MUTATION,
    _UPLOAD_EVIDENCE_MUTATION,
    _USER_ID_BY_USERNAME_QUERY,
    _WHOAMI_QUERY,
)

__all__ = [
    "GhostwriterClient",
    "GhostwriterError",
    "_CONTENT_TYPES_QUERY",
    "_DELETE_EVIDENCE_MUTATION",
    "_DELETE_FINDING_MUTATION",
    "_DELETE_PROJECT_NOTE_MUTATION",
    "_DELETE_REPORTED_FINDING_MUTATION",
    "_DOWNLOAD_EVIDENCE_QUERY",
    "_EVIDENCE_BY_PK_QUERY",
    "_EVIDENCE_QUERY",
    "_EXTRA_FIELD_SPEC_QUERY",
    "_FINDING_BY_PK_QUERY",
    "_FINDING_QUERY",
    "_FINDING_SEVERITY_QUERY",
    "_FINDING_TYPE_LOOKUP_QUERY",
    "_FINGERPRINT_PROBE",
    "_INSERT_FINDING_MUTATION",
    "_INSERT_PROJECT_NOTE_MUTATION",
    "_INSERT_REPORTED_FINDING_MUTATION",
    "_PROJECT_NOTE_BY_PK_QUERY",
    "_REPORT_BY_PK_QUERY",
    "_REPORT_QUERY",
    "_REPORTED_FINDING_BY_PK_QUERY",
    "_REPORTED_FINDING_QUERY",
    "_SET_TAGS_MUTATION",
    "_TAGGED_ITEM_FOR_QUERY",
    "_TAGGED_ITEM_QUERY",
    "_UPDATE_EVIDENCE_MUTATION",
    "_UPDATE_FINDING_MUTATION",
    "_UPDATE_REPORT_MUTATION",
    "_UPDATE_REPORTED_FINDING_MUTATION",
    "_UPLOAD_EVIDENCE_MUTATION",
    "_USER_ID_BY_USERNAME_QUERY",
    "_WHOAMI_QUERY",
]
