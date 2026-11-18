# Daybreak Blue security follow-up

A defensive source review of the native v5 harness at `0bdd288` found issues in reading provider output, supervising child processes, and opening dataset artifacts. Daybreak Blue implemented the provider sidecar and version-probe fixes. The other implementation and review workers checked the subprocess and dataset paths. Verification used local fixtures and archived model results.

- AGY and Codex now cap auxiliary file reads at 16 MiB and verify file type and identity on Unix. AGY keeps a completed reply and its usage when its optional log is invalid. Codex can fall back to the streamed final message and retains reported usage if it must return an error.
- Timed-out provider processes retain bounded stdout and stderr captured before expiry. Cleanup signals the process group even when the direct child has already exited. The CLI version probe drains both pipes under its deadline with 64 KiB capture per stream.
- The model configuration and native provider constructors reject timeout values outside the timer's supported range before dispatch.
- Native dataset input is limited to 512 MiB across the three opened regular files. Valid source aliases within the dataset directory are supported. Existing copied leaves must be regular and byte-identical. New copies use atomic no-clobber publication. The Unix checks operate on opened file descriptors. Directory ownership and protection of ancestor directories remain operator responsibilities.

The rebuilt release executable regraded 24 archived configurations covering 3,456 recorded slots. All 24 JSON summaries and all 24 Markdown reports match the historical Python outputs byte for byte. The complete audit inventory differs from the original only in the executable hash and two timestamps. Eighteen configurations are protocol-complete and six are provider-incomplete. No live model evaluation was needed.

The [follow-up evidence](daybreak-evidence.json) records the release digest, the changes to its 151 compiled source bindings, the new inventory digest and the exact replacements needed to reconstruct it from the archived inventory. RustSec found zero advisories or warnings for 232 locked dependencies. A fresh Gitleaks scan of the changed implementation and test files reported one existing dummy value in a redaction test, already classified in the original audit. No actionable secrets were identified.

The PR records the hosted checks for the published commit. The original blind-receipt and live-evidence requirements still apply to benchmark publication.
