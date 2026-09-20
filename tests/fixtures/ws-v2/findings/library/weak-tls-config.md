---
severity: medium
finding_type: network
cvss:
  vector: CVSS:3.1/AV:A/AC:L/PR:L/UI:R/S:U/C:L/I:L/A:N
cwe:
  - CWE-326
tags:
  - tls
  - crypto
---
# Weak TLS Configuration

## Description

The server negotiates deprecated TLS 1.0/1.1 and weak cipher suites.

## Impact

Traffic may be susceptible to downgrade and cryptographic attacks.

## Mitigation

Disable TLS 1.0/1.1 and weak ciphers; require TLS 1.2 or higher.

## Replication Steps

1. Run `nmap --script ssl-enum-ciphers -p 443 <host>`.
2. Review the reported protocol versions and cipher suites.

## References

- https://www.ssllabs.com/ssltest/
