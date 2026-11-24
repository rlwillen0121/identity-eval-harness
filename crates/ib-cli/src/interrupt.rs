//! Ctrl-C / SIGTERM cleanup for the commands that spawn provider processes (`v52 run`,
//! `v52 probe`).
//!
//! Subscription-CLI calls run in their own process groups inside private temporary call
//! directories, so the default signal disposition would leave both behind. The handler kills every
//! live provider process group ([`kill_all_live`]), removes the live call directories
//! ([`remove_live_call_dirs`]) and exits with the conventional `128 + signal` status. A run stays
//! resumable: checkpoints are rewritten atomically after every completed turn.

use anyhow::Result;
use ib_providers::cli::remove_live_call_dirs;
use ib_providers::subprocess::kill_all_live;

/// Install the handler on the current Tokio runtime. It stays armed for the life of the process.
#[cfg(unix)]
pub fn install_cleanup_handler() -> Result<()> {
    use anyhow::Context as _;
    use tokio::signal::unix::{SignalKind, signal};

    let mut interrupt =
        signal(SignalKind::interrupt()).context("cannot install the SIGINT handler")?;
    let mut terminate =
        signal(SignalKind::terminate()).context("cannot install the SIGTERM handler")?;
    tokio::spawn(async move {
        let code = tokio::select! {
            _ = interrupt.recv() => 130,
            _ = terminate.recv() => 143,
        };
        let groups = kill_all_live();
        let dirs = remove_live_call_dirs();
        eprintln!(
            "interrupted: killed {groups} provider process group(s) and removed {dirs} call directory(ies)"
        );
        std::process::exit(code);
    });
    Ok(())
}

/// Install the handler on the current Tokio runtime (Ctrl-C only on non-Unix targets).
#[cfg(not(unix))]
pub fn install_cleanup_handler() -> Result<()> {
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            kill_all_live();
            remove_live_call_dirs();
            std::process::exit(130);
        }
    });
    Ok(())
}
