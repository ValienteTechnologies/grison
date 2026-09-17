---
severity: low
finding_type: web
cvss:
  vector: CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N
tags:
  - auth
---
# Session Token Predictable

## Description

Session tokens increment sequentially rather than using a CSPRNG.

## Impact

An attacker with physical proximity and high privilege could predict a token.

## Mitigation

Generate session tokens with a cryptographically secure random source.

## Replication Steps

1. Log in twice in a row and compare the two issued session tokens.

## References

- https://owasp.org/www-community/vulnerabilities/Insufficient_Session-ID_Length
