//! Deterministic Multi-Cloud CIEM (`AWS IAM`, `Entra`/`Azure RBAC`, `GCP IAM`/`WIF`)
//! evaluation, escalation path detection, and required-operation preservation checks.

use std::collections::BTreeSet;

use ib_v52_schemas::{
    CloudEscalationVectorV52, CloudPrincipalRecordV52, CloudPrincipalRemediationV52,
    CloudProviderV52, CloudRemediationActionV52, EnterpriseBrownfieldWorldV52,
    IgaDeploymentConfigV52,
};
use indexmap::IndexMap;

/// Detailed deterministic audit result for a single cloud principal's CIEM remediation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloudPrincipalEvalV52 {
    pub principal_id: String,
    pub remediation: CloudPrincipalRemediationV52,
    pub detected_escalation_vector: CloudEscalationVectorV52,
    pub preserved_required_ops: bool,
    pub missing_required_ops: Vec<String>,
    pub eliminated_escalation_path: bool,
    pub remaining_escalation_permissions: Vec<String>,
}

/// Evaluate `config.cloud_ciem` deterministically against `world.cloud_principals`,
/// normalizing `retained_permissions` (trimmed, deduplicated, sorted).
#[must_use]
pub fn evaluate_cloud_ciem(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
) -> IndexMap<String, CloudPrincipalRemediationV52> {
    let mut outcomes = IndexMap::new();
    let mut sorted_principals: Vec<&CloudPrincipalRecordV52> =
        world.cloud_principals.iter().collect();
    sorted_principals.sort_by(|a, b| a.principal_id.cmp(&b.principal_id));

    for principal in sorted_principals {
        if let Some(rem) = config.cloud_ciem.principals.get(&principal.principal_id) {
            let retained_permissions: Vec<String> = rem
                .retained_permissions
                .iter()
                .map(|s| s.trim())
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect();
            outcomes.insert(
                principal.principal_id.clone(),
                CloudPrincipalRemediationV52 {
                    escalation_vector: rem.escalation_vector,
                    action: rem.action,
                    retained_permissions,
                },
            );
        }
    }

    outcomes
}

/// Inspect a single cloud principal's remediation for `preserved_required_ops` and
/// `eliminated_escalation_path`.
#[must_use]
pub fn inspect_cloud_principal_remediation(
    principal: &CloudPrincipalRecordV52,
    remediation: &CloudPrincipalRemediationV52,
) -> CloudPrincipalEvalV52 {
    let detected_escalation_vector = detect_escalation_vector(principal);
    let required_set: BTreeSet<&str> = principal
        .required_operations
        .iter()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();
    let retained_set: BTreeSet<&str> = remediation
        .retained_permissions
        .iter()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();

    // 1. Check required_operations preservation.
    let missing_required_ops: Vec<String> =
        if remediation.action == CloudRemediationActionV52::RevokeOrphanPrincipal {
            if principal.is_orphan_unused {
                Vec::new()
            } else {
                required_set.iter().map(|s| (*s).to_owned()).collect()
            }
        } else {
            required_set
                .iter()
                .filter(|op| !retained_set.contains(*op))
                .map(|s| (*s).to_owned())
                .collect()
        };
    let preserved_required_ops = missing_required_ops.is_empty()
        && !(remediation.action == CloudRemediationActionV52::RevokeOrphanPrincipal
            && !principal.is_orphan_unused);

    // 2. Check whether any escalation permissions remain in `retained_permissions`.
    let remaining_escalation_permissions: Vec<String> = retained_set
        .iter()
        .filter(|perm| {
            !required_set.contains(*perm) && is_escalation_permission(principal.provider, perm)
        })
        .map(|s| (*s).to_owned())
        .collect();

    // 3. Determine whether the escalation path (or orphan exposure) was actually eliminated.
    let eliminated_escalation_path = if principal.is_orphan_unused {
        remediation.action == CloudRemediationActionV52::RevokeOrphanPrincipal
            && retained_set.is_empty()
    } else {
        let action_remediates_vector = match detected_escalation_vector {
            CloudEscalationVectorV52::None => true,
            CloudEscalationVectorV52::GcpWifUnscopedRepo => matches!(
                remediation.action,
                CloudRemediationActionV52::PinWifRepoCondition
                    | CloudRemediationActionV52::RevokeOrphanPrincipal
            ),
            CloudEscalationVectorV52::AwsPassRoleCompute
            | CloudEscalationVectorV52::AwsAssumeRoleChain
            | CloudEscalationVectorV52::AzureRbacOwnerOrRoleWrite
            | CloudEscalationVectorV52::EntraDirectoryPrivRoleAdmin
            | CloudEscalationVectorV52::GcpSaImpersonation => {
                remediation.action != CloudRemediationActionV52::KeepLeastPrivilege
            }
        };
        action_remediates_vector && remaining_escalation_permissions.is_empty()
    };

    CloudPrincipalEvalV52 {
        principal_id: principal.principal_id.clone(),
        remediation: remediation.clone(),
        detected_escalation_vector,
        preserved_required_ops,
        missing_required_ops,
        eliminated_escalation_path,
        remaining_escalation_permissions,
    }
}

/// Detect the primary privilege-escalation vector present on a brownfield `CloudPrincipalRecordV52`.
#[must_use]
pub fn detect_escalation_vector(principal: &CloudPrincipalRecordV52) -> CloudEscalationVectorV52 {
    let required_set: BTreeSet<String> = principal
        .required_operations
        .iter()
        .map(|s| s.trim().to_ascii_lowercase())
        .collect();
    let effective_perms: Vec<String> = principal
        .direct_permissions
        .iter()
        .chain(principal.inherited_permissions.iter())
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .collect();

    match principal.provider {
        CloudProviderV52::Aws => {
            let has_wildcard_iam = effective_perms.iter().any(|p| {
                let low = p.to_ascii_lowercase();
                !required_set.contains(&low) && (low == "iam:*" || low == "*")
            });
            let has_pass_role = effective_perms.iter().any(|p| {
                let low = p.to_ascii_lowercase();
                !required_set.contains(&low) && low == "iam:passrole"
            });
            let has_compute_exec = effective_perms.iter().any(|p| {
                let low = p.to_ascii_lowercase();
                is_aws_compute_exec_perm(&low)
            });
            if has_wildcard_iam
                || (has_pass_role && (has_compute_exec || principal.is_orphan_unused))
            {
                return CloudEscalationVectorV52::AwsPassRoleCompute;
            }
            let has_assume_or_iam_priv = effective_perms.iter().any(|p| {
                let low = p.to_ascii_lowercase();
                !required_set.contains(&low) && is_aws_escalation_perm(&low)
            });
            if has_assume_or_iam_priv {
                return CloudEscalationVectorV52::AwsAssumeRoleChain;
            }
            CloudEscalationVectorV52::None
        }
        CloudProviderV52::AzureEntra => {
            let has_entra_priv = principal
                .entra_directory_roles
                .iter()
                .chain(effective_perms.iter())
                .any(|r| {
                    let low = r.to_ascii_lowercase();
                    !required_set.contains(&low) && is_entra_priv_role(&low)
                });
            if has_entra_priv {
                return CloudEscalationVectorV52::EntraDirectoryPrivRoleAdmin;
            }
            let has_azure_rbac_write = effective_perms.iter().any(|p| {
                let low = p.to_ascii_lowercase();
                !required_set.contains(&low) && is_azure_rbac_escalation_perm(&low)
            });
            if has_azure_rbac_write {
                return CloudEscalationVectorV52::AzureRbacOwnerOrRoleWrite;
            }
            CloudEscalationVectorV52::None
        }
        CloudProviderV52::Gcp => {
            if let Some(req_repo) = principal
                .required_wif_repository
                .as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty())
            {
                let single_quoted = format!("'{req_repo}'");
                let double_quoted = format!("\"{req_repo}\"");
                let pinned = principal
                    .wif_trust_condition
                    .as_deref()
                    .is_some_and(|cond| {
                        !cond.contains('*')
                            && (cond.contains(&single_quoted) || cond.contains(&double_quoted))
                            && !cond.contains(&format!("startsWith({single_quoted})"))
                            && !cond.contains(&format!("startsWith({double_quoted})"))
                            && !cond.contains(&format!("!= {single_quoted}"))
                            && !cond.contains(&format!("!= {double_quoted}"))
                    });
                if !pinned {
                    return CloudEscalationVectorV52::GcpWifUnscopedRepo;
                }
            }
            let has_sa_impersonation = effective_perms.iter().any(|p| {
                let low = p.to_ascii_lowercase();
                !required_set.contains(&low) && is_gcp_escalation_perm(&low)
            });
            if has_sa_impersonation {
                return CloudEscalationVectorV52::GcpSaImpersonation;
            }
            CloudEscalationVectorV52::None
        }
    }
}

/// Check whether a single permission/role string constitutes a privilege escalation permission
/// on `provider`.
#[must_use]
pub fn is_escalation_permission(provider: CloudProviderV52, permission: &str) -> bool {
    let low = permission.trim().to_ascii_lowercase();
    if low.is_empty() {
        return false;
    }
    match provider {
        CloudProviderV52::Aws => is_aws_escalation_perm(&low),
        CloudProviderV52::AzureEntra => {
            is_entra_priv_role(&low) || is_azure_rbac_escalation_perm(&low)
        }
        CloudProviderV52::Gcp => is_gcp_escalation_perm(&low),
    }
}

fn is_aws_compute_exec_perm(low: &str) -> bool {
    matches!(
        low,
        "lambda:*"
            | "lambda:createfunction"
            | "lambda:updatefunctioncode"
            | "lambda:updatefunctionconfiguration"
            | "lambda:invokefunction"
            | "ec2:*"
            | "ec2:runinstances"
            | "ec2:associateiaminstanceprofile"
            | "ec2:replaceiaminstanceprofileassociation"
            | "ecs:*"
            | "ecs:registertaskdefinition"
            | "ecs:runtask"
            | "ecs:createservice"
            | "ecs:updateservice"
            | "glue:*"
            | "glue:createjob"
            | "glue:updatejob"
            | "glue:createdevendpoint"
            | "glue:startjobrun"
            | "sagemaker:*"
            | "sagemaker:createnotebookinstance"
            | "sagemaker:createtrainingjob"
            | "sagemaker:createprocessingjob"
            | "sagemaker:createmodel"
            | "states:*"
            | "states:createstatemachine"
            | "states:updatestatemachine"
            | "states:startexecution"
            | "cloudformation:*"
            | "cloudformation:createstack"
            | "cloudformation:updatestack"
            | "codebuild:*"
            | "codebuild:createproject"
            | "codebuild:startbuild"
            | "datapipeline:*"
            | "datapipeline:createpipeline"
            | "batch:*"
            | "batch:submitjob"
            | "batch:registerjobdefinition"
    )
}

fn is_aws_escalation_perm(low: &str) -> bool {
    matches!(
        low,
        "*" | "iam:*"
            | "iam:passrole"
            | "sts:assumerole"
            | "sts:*"
            | "iam:attachrolepolicy"
            | "iam:attachuserpolicy"
            | "iam:putrolepolicy"
            | "iam:putuserpolicy"
            | "iam:createpolicyversion"
            | "iam:setdefaultpolicyversion"
            | "iam:updateassumerolepolicy"
            | "iam:createaccesskey"
            | "iam:addusertogroup"
            | "administratoraccess"
    )
}

fn is_entra_priv_role(low: &str) -> bool {
    low.contains("privileged role administrator")
        || low.contains("privileged_role_administrator")
        || low.contains("global administrator")
        || low.contains("global_administrator")
        || low.contains("rolemanagement.readwrite.directory")
        || low.contains("privileged authentication administrator")
        || low.contains("application administrator")
        || low.contains("cloud application administrator")
}

fn is_azure_rbac_escalation_perm(low: &str) -> bool {
    low == "*"
        || low == "*/write"
        || low == "owner"
        || low == "user access administrator"
        || low == "microsoft.authorization/*"
        || low == "microsoft.authorization/roleassignments/write"
        || low == "microsoft.authorization/roledefinitions/write"
        || low == "microsoft.authorization/elevateaccess/action"
}

fn is_gcp_escalation_perm(low: &str) -> bool {
    matches!(
        low,
        "*" | "roles/owner"
            | "roles/editor"
            | "roles/iam.serviceaccounttokencreator"
            | "roles/iam.serviceaccountuser"
            | "roles/iam.securityadmin"
            | "iam.serviceaccounts.actas"
            | "iam.serviceaccounts.getaccesstoken"
            | "iam.serviceaccounts.signblob"
            | "iam.serviceaccounts.signjwt"
            | "iam.serviceaccounts.implicitdelegation"
            | "iam.serviceaccountkeys.create"
            | "resourcemanager.projects.setiampolicy"
            | "resourcemanager.folders.setiampolicy"
            | "resourcemanager.organizations.setiampolicy"
    )
}
