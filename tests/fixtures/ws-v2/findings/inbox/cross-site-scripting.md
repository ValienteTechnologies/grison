---
severity: medium
finding_type: web
cwe:
- CWE-79
affected_entities: https://example.com/search?q=test
---

# Cross-Site Scripting

## Description

Reflected XSS detected in search parameter.

## Impact

Session hijacking, credential theft.

## Mitigation

Encode all user-supplied output. Use a Content Security Policy.

## Replication Steps

## References
