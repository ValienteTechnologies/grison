---
severity: critical
finding_type: web
cvss:
  vector: CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H
cwe:
  - CWE-79
tags:
  - injection
  - xss
affected_entities: |
  https://app.acme.test/search
---
# Reflected XSS in the search field

## Description

The `q` parameter is reflected into the page without encoding.

![Alert firing in the browser](evidence/xss-alert.png "captured during testing")

## Impact

An attacker can run arbitrary JavaScript in a victim's session.

## Mitigation

HTML-encode all reflected user input.

## Replication Steps

1. Browse to `/search?q=<script>alert(1)</script>`.
2. Observe the alert fires.

## References

- https://owasp.org/www-community/attacks/xss/
- See [the alert screenshot](evidence/xss-alert.png) for proof.
