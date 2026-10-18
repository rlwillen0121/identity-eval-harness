# Connector Resilience Edge Cases (`CONN-002-v3`)

| Scenario | Required Action | Rule ID |
| :--- | :--- | :--- |
| Multi-page SCIM response with `next_cursor` | Loop until `next_cursor` is null and verify `records_rejected == 0` | `CONN-001-v3` |
| Target account modified concurrently (`etag` mismatch) | Supply `If-Match` header; abort and refresh on `412 Precondition Failed` | `CONN-002-v3` |
| Target API lacks `If-Match` support during concurrent write | Refuse write with `BLOCKED_UNSAFE_CONCURRENCY` | `CONN-002-v3` |
