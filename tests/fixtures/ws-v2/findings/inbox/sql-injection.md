---
severity: high
finding_type: web
cwe:
- CWE-89
tags:
- CVE-2023-1234
- injection
affected_entities: 'https://example.com/login.php?id=1

  https://other.example.com/admin/users.php?id=1'
---

# SQL Injection

## Description

SQL injection vulnerability detected in login parameter.

## Impact

Data exfiltration, authentication bypass, remote code execution.

## Mitigation

Use parameterized queries or prepared statements.

## Replication Steps

## References

- CVE-2023-1234
