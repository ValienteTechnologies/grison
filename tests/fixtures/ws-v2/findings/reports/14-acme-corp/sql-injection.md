---
severity: high
finding_type: web
cvss:
  vector: CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:L/A:N
tags:
  - injection
affected_entities: |
  https://app.acme.test/login
---
# SQL Injection in the login form

## Description

The `username` field is concatenated directly into a SQL query.

## Impact

An attacker can bypass authentication or exfiltrate the user database.

## Mitigation

Use parameterized queries for all database access.

## Replication Steps

1. Submit `' OR '1'='1' -- ` in the username field.

   ![Successful authentication bypass](evidence/bypass-attempt.png "captured during testing")

2. Observe the request succeeds without valid credentials.

## References

- https://owasp.org/www-community/attacks/SQL_Injection
