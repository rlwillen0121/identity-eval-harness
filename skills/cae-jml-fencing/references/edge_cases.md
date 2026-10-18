# CAE & JML Fencing Edge Cases (`CAE-001`)

| Scenario | Required Action | Rule ID |
| :--- | :--- | :--- |
| Involuntary termination or compromise (`t_reaction_start`) | Revoke sessions + rotate NHI secrets within $E_{\max} \le 60\text{s}$ | `CAE-001` |
| Returning contractor rehire (`person_lifecycle_gen` incremented) | Reject stale prior-generation staged writes (`FENCED_STALE_LIFECYCLE_GEN`) | `JML-002` |
| Unowned NHI credential rotation | Supply `(if_match_etag, account_gen)` without human lifecycle fields (`R2-04`) | `JML-003` |
