use std::sync::Arc;

use async_trait::async_trait;
use ib_providers::base::{ModelRequest, ModelResponse, Provider, ProviderCapabilities, Usage};
use ib_v5_accounting::{
    ProviderRoleV5, USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5, UsageAccountingEvidenceV5,
    provider_fingerprint_sha256_v5,
};
use ib_v5_runner::{
    RunConfigV5,
    execution::{RunEpisodeOptionsV5, run_v5_agent_episode_with_options},
};
use ib_v5_schemas::{EpisodeV5, SealedEpisodeOracleV5};

struct AccountingProvider {
    reports_usage: bool,
    usage_includes_retries: bool,
}

#[async_trait]
impl Provider for AccountingProvider {
    fn kind(&self) -> &str {
        "offline-accounting"
    }

    fn model(&self) -> &str {
        "accounting-fixture"
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        (
            "identitybench.providers.openai_compat",
            "OpenAICompatProvider",
        )
    }

    fn local_retry_limit(&self) -> Option<u32> {
        Some(0)
    }

    fn usage_includes_retries(&self) -> bool {
        self.usage_includes_retries
    }

    fn endpoint_identity(&self) -> String {
        "offline-accounting-fixture".to_owned()
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            native_structured_output: true,
            native_tools: true,
            supports_temperature: true,
            reports_usage: self.reports_usage,
        }
    }

    async fn complete(&self, _request: &ModelRequest) -> ModelResponse {
        ModelResponse {
            usage: Usage {
                input_tokens: 17,
                output_tokens: 9,
                ..Usage::default()
            },
            ..ModelResponse::default()
        }
    }
}

fn fixture_pair() -> (EpisodeV5, SealedEpisodeOracleV5) {
    let episode: EpisodeV5 = serde_json::from_str(
        include_str!("../../../datasets/v5_dev/episodes.jsonl")
            .lines()
            .next()
            .expect("frozen v5_dev episode fixture"),
    )
    .expect("valid fixture episode");
    let oracle: SealedEpisodeOracleV5 = serde_json::from_str(
        include_str!("../../../datasets/v5_dev/oracles.jsonl")
            .lines()
            .next()
            .expect("frozen v5_dev oracle fixture"),
    )
    .expect("valid fixture oracle");
    (episode, oracle)
}

fn attestation() -> UsageAccountingEvidenceV5 {
    UsageAccountingEvidenceV5 {
        role: ProviderRoleV5::Unified,
        provider_kind: "offline-accounting".to_owned(),
        model_id: "accounting-fixture".to_owned(),
        provider_fingerprint_sha256: provider_fingerprint_sha256_v5(
            "unified",
            "offline-accounting",
            "accounting-fixture",
            "offline-accounting-fixture",
        ),
        aggregate_usage_scope: USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5.to_owned(),
        evidence_sha256: "b".repeat(64),
        adapter_retry_limit: 0,
    }
}

async fn run_accounting_case(
    reports_usage: bool,
    usage_includes_retries: bool,
    with_evidence: bool,
) -> ib_v5_scoring::EpisodeExecutionV5 {
    let (episode, oracle) = fixture_pair();
    let mut config = RunConfigV5 {
        max_turns: Some(1),
        ..RunConfigV5::default()
    };
    if with_evidence {
        config.usage_accounting_evidence = vec![attestation()];
    }
    let provider: Arc<dyn Provider> = Arc::new(AccountingProvider {
        reports_usage,
        usage_includes_retries,
    });
    run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider,
        RunEpisodeOptionsV5 {
            run_config: config,
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("offline accounting run")
}

#[tokio::test]
async fn provider_usage_accounting_declared_attested_unknown_and_unavailable_match_python() {
    let declared = run_accounting_case(true, true, false).await;
    assert_eq!(
        declared.usage_accounting_status.as_str(),
        "PROVIDER_DECLARED"
    );
    assert!(declared.usage_provenance_complete);
    assert_eq!(
        declared.provider_accounting[0].basis,
        "provider_declared_usage_includes_retries"
    );

    let attested = run_accounting_case(true, false, true).await;
    assert_eq!(attested.usage_accounting_status.as_str(), "ATTESTED");
    assert!(attested.usage_provenance_complete);
    assert_eq!(attested.provider_accounting[0].local_retry_limit, Some(0));
    assert_eq!(
        attested.provider_accounting[0]
            .local_retry_policy_id
            .as_deref(),
        Some("identitybench-v5-sdk_max_retries-max-retries-v1")
    );

    let unknown = run_accounting_case(true, false, false).await;
    assert_eq!(unknown.usage_accounting_status.as_str(), "UNKNOWN");
    assert!(!unknown.usage_provenance_complete);

    let unavailable = run_accounting_case(false, false, true).await;
    assert_eq!(unavailable.usage_accounting_status.as_str(), "UNKNOWN");
    assert!(!unavailable.usage_provenance_complete);
    assert_eq!(
        unavailable.provider_accounting[0].basis,
        "provider_does_not_report_usage"
    );
    assert_eq!(unavailable.input_tokens_actual, None);
    assert_eq!(unavailable.output_tokens_actual, None);
}
