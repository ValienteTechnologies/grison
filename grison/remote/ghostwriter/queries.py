"""GraphQL query and mutation documents sent to Ghostwriter's Hasura endpoint,
grouped by table. See :mod:`grison.remote.ghostwriter.client` for the
:class:`~grison.remote.ghostwriter.client.GhostwriterClient` methods that send these.

Every module-level constant here whose name is private (leading ``_``) and ends in
``_QUERY``/``_MUTATION`` is treated by :mod:`grison.remote.compat` and
``tests/test_gw_schema_conformance.py`` as part of "the real CRUD surface grison
sends" and validated against the live schema — see :data:`_FINGERPRINT_PROBE`'s own
docstring for the one deliberate exception.
"""

from __future__ import annotations

# Deliberately NOT named `_..._QUERY`/`_..._MUTATION`: grison.remote.compat's
# `_module_level_operations()` (and tests/test_gw_schema_conformance.py, which
# shares that exact extraction) treat every module-level constant matching that
# naming convention as "an operation grison sends" and validate it as part of the
# real CRUD surface — the fingerprint probe is a compat-check implementation
# detail, not one of those operations, so it is named to fall outside that scan.
_FINGERPRINT_PROBE = """
query {
  queryRoot: __type(name: "query_root") { fields { name } }
  mutationRoot: __type(name: "mutation_root") { fields { name args { name } } }
  evidence: __type(name: "evidence") { fields { name } }
  finding: __type(name: "finding") { fields { name } }
  reportedFinding: __type(name: "reportedFinding") { fields { name } }
  report: __type(name: "report") { fields { name } }
}
"""

_FINDING_QUERY = """
query {
  finding {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_REPORTED_FINDING_QUERY = """
query {
  reportedFinding {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
    position
  }
}
"""

_EVIDENCE_QUERY = """
query {
  evidence {
    id
    reportId
    document
    caption
    friendlyName
    description
  }
}
"""

_EVIDENCE_BY_PK_QUERY = """
query($id: bigint!) {
  evidence_by_pk(id: $id) {
    id
    reportId
    document
    caption
    friendlyName
    description
  }
}
"""

_FINDING_BY_PK_QUERY = """
query($id: bigint!) {
  finding_by_pk(id: $id) {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_REPORTED_FINDING_BY_PK_QUERY = """
query($id: bigint!) {
  reportedFinding_by_pk(id: $id) {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
    position
  }
}
"""

_FINDING_SEVERITY_QUERY = """
query {
  findingSeverity {
    id
    severity
    weight
  }
}
"""

_FINDING_TYPE_LOOKUP_QUERY = """
query {
  findingType {
    id
    findingType
  }
}
"""

_CONTENT_TYPES_QUERY = """
query {
  djangoContentType(
    where: {appLabel: {_eq: "reporting"}, model: {_in: ["finding", "reportfindinglink"]}}
  ) {
    id
    model
  }
}
"""

_TAGGED_ITEM_QUERY = """
query($content_type_ids: [Int!]) {
  taggedItem(where: {content_type_id: {_in: $content_type_ids}}, order_by: {id: asc}) {
    content_type_id
    object_id
    tag {
      name
    }
  }
}
"""

_TAGGED_ITEM_FOR_QUERY = """
query($content_type_id: Int!, $object_id: Int!) {
  taggedItem(
    where: {content_type_id: {_eq: $content_type_id}, object_id: {_eq: $object_id}}
    order_by: {id: asc}
  ) {
    tag {
      name
    }
  }
}
"""

_SET_TAGS_MUTATION = """
mutation($id: bigint!, $model: String!, $tags: [String!]!) {
  setTags(id: $id, model: $model, tags: $tags) {
    tags
  }
}
"""

_REPORT_QUERY = """
query {
  report {
    id
    title
    complete
    archived
    delivered
    creation
    last_update
    extraFields
    project {
      id
      startDate
      endDate
      codename
      collab_note
      client {
        id
        name
        shortName
      }
      scopes {
        name
        scope
        description
        disallowed
        requiresCaution
      }
      objectives {
        objective
        description
        complete
        deadline
        position
        result
        markedComplete
        objectiveStatus {
          objectiveStatus
        }
        objectivePriority {
          priority
        }
      }
      targets {
        hostname
        ipAddress
        description
        compromised
      }
      whitecards {
        title
        issued
        description
      }
      comments {
        id
        note
        timestamp
        operatorId
        user {
          name
          username
        }
      }
    }
  }
}
"""

_REPORT_BY_PK_QUERY = """
query($id: bigint!) {
  report_by_pk(id: $id) {
    id
    extraFields
    last_update
  }
}
"""

_EXTRA_FIELD_SPEC_QUERY = """
query($target_model: String!) {
  extraFieldSpec(
    where: {targetModel: {_eq: $target_model}}
    order_by: {position: asc}
  ) {
    id
    internalName
    displayName
    position
  }
}
"""

_PROJECT_NOTE_BY_PK_QUERY = """
query($id: bigint!) {
  projectNote_by_pk(id: $id) {
    id
    projectId
    note
    operatorId
    timestamp
    user {
      name
      username
    }
  }
}
"""

_DELETE_PROJECT_NOTE_MUTATION = """
mutation($id: bigint!) {
  delete_projectNote_by_pk(id: $id) {
    id
  }
}
"""

_WHOAMI_QUERY = """
query {
  whoami {
    username
    role
    expires
  }
}
"""

_USER_ID_BY_USERNAME_QUERY = """
query($username: String!) {
  user(where: {username: {_eq: $username}}) {
    id
  }
}
"""

_INSERT_PROJECT_NOTE_MUTATION = """
mutation($obj: projectNote_insert_input!) {
  insert_projectNote_one(object: $obj) {
    id
  }
}
"""

_UPDATE_REPORT_MUTATION = """
mutation($id: bigint!, $set: report_set_input!) {
  update_report_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
  }
}
"""

_DOWNLOAD_EVIDENCE_QUERY = """
query($id: Int!) {
  downloadEvidence(evidenceId: $id) {
    fileBase64
    filename
  }
}
"""

# The insert/update mutations below return the exact field set _FINDING_QUERY /
# _REPORTED_FINDING_QUERY select (the sync engine rebuilds the canonical local finding
# from this ``returning`` row instead of the pre-push local object — the "returning" set
# governed by the manager role's SELECT permission, verified to cover every field here).
_INSERT_FINDING_MUTATION = """
mutation($obj: finding_insert_input!) {
  insert_finding_one(object: $obj) {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_UPDATE_FINDING_MUTATION = """
mutation($id: bigint!, $set: finding_set_input!) {
  update_finding_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
  }
}
"""

_INSERT_REPORTED_FINDING_MUTATION = """
mutation($obj: reportedFinding_insert_input!) {
  insert_reportedFinding_one(object: $obj) {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
    position
  }
}
"""

_UPDATE_REPORTED_FINDING_MUTATION = """
mutation($id: bigint!, $set: reportedFinding_set_input!) {
  update_reportedFinding_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
    reportId
    title
    severityId
    findingTypeId
    cvssScore
    cvssVector
    description
    impact
    mitigation
    references
    replication_steps
    affectedEntities
    position
  }
}
"""

# D1/BRIEF B: evidence belongs to a REPORT (never a finding) — the real >= 7.2
# schema's ``uploadEvidence`` has a ``report: Int!`` argument and no ``finding``
# argument at all (confirmed against ``tests/fixtures/gw-schema-7.2.6.graphql``).
_UPLOAD_EVIDENCE_MUTATION = """
mutation(
  $report: Int!
  $file_base64: String!
  $filename: String!
  $caption: String!
  $friendly_name: String!
  $description: String
) {
  uploadEvidence(
    report: $report
    file_base64: $file_base64
    filename: $filename
    caption: $caption
    friendly_name: $friendly_name
    description: $description
  ) {
    id
  }
}
"""

_UPDATE_EVIDENCE_MUTATION = """
mutation($id: bigint!, $set: evidence_set_input!) {
  update_evidence_by_pk(pk_columns: {id: $id}, _set: $set) {
    id
    caption
    friendlyName
    description
  }
}
"""

_DELETE_EVIDENCE_MUTATION = """
mutation($id: bigint!) {
  delete_evidence_by_pk(id: $id) {
    id
  }
}
"""

_DELETE_FINDING_MUTATION = """
mutation($id: bigint!) {
  delete_finding_by_pk(id: $id) {
    id
  }
}
"""

_DELETE_REPORTED_FINDING_MUTATION = """
mutation($id: bigint!) {
  delete_reportedFinding_by_pk(id: $id) {
    id
  }
}
"""
