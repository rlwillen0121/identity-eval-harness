//! Tombstoned (non-recyclable) handle archive and reserved aliases.

use ib_v52_schemas::TombstoneHandleRecordV52;

use super::WorldCtx;
use super::names::sanitize_sam_token;

/// Build the tombstone archive, always including `tomb_base_sam`.
pub(super) fn build_tombstone_archive(
    ctx: &WorldCtx<'_>,
    first_pool: &[&str],
    last_pool: &[&str],
    tomb_base_sam: String,
    long_base_sam: String,
) -> Vec<TombstoneHandleRecordV52> {
    let WorldCtx {
        spec,
        client,
        is_multi_or_full,
        ..
    } = *ctx;
    // Build tombstone archive (22 / 30 / 38 records, ALWAYS including `tomb_base_sam`!).
    // On MultiSystem & FullStack, plant the full `cstart..=9` collision chain on `long_base_sam`
    // so `longname_collision_newhire` must roll over from `...9` to `...10` (testing `18 + 2 = 20` chars!).
    let mut tombstone_archive = Vec::with_capacity(spec.tier.tombstone_count());
    tombstone_archive.push(TombstoneHandleRecordV52 {
        archive_id: "TOMB-001".to_owned(),
        sam_account_name: tomb_base_sam.clone(),
        upn: format!("{tomb_base_sam}@{}", client.primary_domain),
        terminated_date: "2024-06-18".to_owned(),
        reason: "Voluntary separation (7-year SOX/audit non-recycling hold)".to_owned(),
    });
    if is_multi_or_full {
        let long_prefix_19: String = long_base_sam.chars().take(19).collect();
        for c in spec.collision_counter_start..=9 {
            let idx = tombstone_archive.len();
            let handle = format!("{long_prefix_19}{c}");
            tombstone_archive.push(TombstoneHandleRecordV52 {
                archive_id: format!("TOMB-{:03}", idx + 1),
                sam_account_name: handle.clone(),
                upn: format!("{handle}@{}", client.primary_domain),
                terminated_date: format!(
                    "202{}-{:02}-{:02}",
                    1 + (idx % 5),
                    1 + (idx % 12),
                    10 + (idx % 15)
                ),
                reason: "Former employee archive (7-year anti-recycling lock)".to_owned(),
            });
        }
    }
    while tombstone_archive.len() < spec.tier.tombstone_count() {
        let idx = tombstone_archive.len();
        let f = first_pool[(idx + 3) % first_pool.len()];
        let l = last_pool[(idx + 9) % last_pool.len()];
        let handle = format!(
            "{}{}x{}",
            sanitize_sam_token(f).chars().take(1).collect::<String>(),
            sanitize_sam_token(l).chars().take(10).collect::<String>(),
            idx + 10
        );
        tombstone_archive.push(TombstoneHandleRecordV52 {
            archive_id: format!("TOMB-{:03}", idx + 1),
            sam_account_name: handle.clone(),
            upn: format!("{handle}@{}", client.primary_domain),
            terminated_date: format!(
                "202{}-{:02}-{:02}",
                1 + (idx % 5),
                1 + (idx % 12),
                10 + (idx % 15)
            ),
            reason: if idx % 3 == 0 {
                "Involuntary termination (7-year anti-recycling lock)".to_owned()
            } else {
                "Former employee archive (7-year retention)".to_owned()
            },
        });
    }

    tombstone_archive
}

/// Reserved mailbox/role aliases no generated handle may take.
pub(super) fn reserved_aliases() -> Vec<String> {
    vec![
        "admin".to_owned(),
        "administrator".to_owned(),
        "root".to_owned(),
        "postmaster".to_owned(),
        "helpdesk".to_owned(),
        "security".to_owned(),
        "payroll".to_owned(),
    ]
}
