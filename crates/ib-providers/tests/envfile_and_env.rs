use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;

use ib_providers::env::paired_api_key_env;
use ib_providers::envfile::{
    env_name_allowed, is_live_command, load_env_file_into, load_env_file_map, parse_env,
};
use tempfile::TempDir;

#[test]
fn parses_comments_blank_lines_export_and_quotes() {
    let text = [
        "# a comment",
        "",
        "PLAIN=value",
        "  SPACED  =  padded value  ",
        "export EXPORTED=yes",
        "SINGLE='literal $HOME \\n # not a comment'",
        r#"DOUBLE="line\nbreak \"quoted\" back\\slash \$x""#,
        "INLINE=abc # trailing comment",
        "HASH_IN_VALUE=abc#def",
        "QUOTED_THEN_COMMENT='kept' # comment",
        "EQUALS=a=b=c",
    ]
    .join("\n");

    let parsed = parse_env(&text);
    assert!(parsed.problems.is_empty());
    let expected = BTreeMap::from([
        ("PLAIN".to_string(), "value".to_string()),
        ("SPACED".to_string(), "padded value".to_string()),
        ("EXPORTED".to_string(), "yes".to_string()),
        (
            "SINGLE".to_string(),
            "literal $HOME \\n # not a comment".to_string(),
        ),
        (
            "DOUBLE".to_string(),
            "line\nbreak \"quoted\" back\\slash $x".to_string(),
        ),
        ("INLINE".to_string(), "abc".to_string()),
        ("HASH_IN_VALUE".to_string(), "abc#def".to_string()),
        ("QUOTED_THEN_COMMENT".to_string(), "kept".to_string()),
        ("EQUALS".to_string(), "a=b=c".to_string()),
    ]);
    assert_eq!(parsed.values, expected);
}

#[test]
fn empty_values_are_unfilled_but_quoted_empty_is_kept() {
    let parsed = parse_env("UNSET=\nCOMMENT_ONLY= # nothing yet\nHASH=#nothing\nEMPTY=\"\"\n");
    assert_eq!(
        parsed.values,
        BTreeMap::from([("EMPTY".to_string(), String::new())])
    );
    assert!(parsed.problems.is_empty());
}

#[test]
fn later_assignments_win_and_empty_unsets() {
    assert_eq!(
        parse_env("A=1\nA=2\n").values,
        BTreeMap::from([("A".to_string(), "2".to_string())])
    );
    assert!(parse_env("A=1\nA=\n").values.is_empty());
}

#[test]
fn problems_name_lines_and_keys_never_values() {
    let secret = "placeholder-secret-for-tests";
    let text = [
        "GOOD=fine".to_string(),
        format!("not a pair {secret}"),
        format!("1BAD={secret}"),
        format!("OPEN='{secret}"),
        format!("DQ=\"{secret}"),
        format!("TRAIL='{secret}' extra"),
    ]
    .join("\n");
    let parsed = parse_env(&text);
    assert_eq!(
        parsed.values,
        BTreeMap::from([("GOOD".to_string(), "fine".to_string())])
    );
    assert_eq!(parsed.problems.len(), 5);
    assert!(parsed.problems[0].starts_with("line 2:"));
    assert!(parsed.problems[2].contains("OPEN") && parsed.problems[2].contains("unterminated"));
    assert!(parsed.problems[4].contains("text after the closing quote"));
    assert!(
        parsed
            .problems
            .iter()
            .all(|p| p.ends_with("; line skipped"))
    );
    assert!(parsed.problems.iter().all(|p| !p.contains(secret)));
}

#[test]
fn load_sets_only_missing_variables_and_returns_sorted_names() {
    let tmp = TempDir::new().unwrap();
    let env_path = tmp.path().join(".env");
    fs::write(
        &env_path,
        "B_KEY=secret-b\nEXISTING=from-file\nA_KEY=secret-a\nbroken line\n",
    )
    .unwrap();
    let mut environ = BTreeMap::from([("EXISTING".to_string(), "from-environment".to_string())]);
    let (loaded, problems) = load_env_file_map(&env_path, &mut environ);
    assert_eq!(loaded, vec!["A_KEY", "B_KEY"]);
    assert_eq!(problems, vec!["line 4: expected KEY=VALUE; line skipped"]);
    assert_eq!(environ["EXISTING"], "from-environment");
    assert_eq!(environ["A_KEY"], "secret-a");
    assert_eq!(environ["B_KEY"], "secret-b");
}

#[test]
fn missing_file_and_utf8_bom_and_invalid_utf8_and_permissions() {
    let tmp = TempDir::new().unwrap();
    let mut environ = BTreeMap::new();
    assert_eq!(
        load_env_file_map(tmp.path().join(".env"), &mut environ),
        (vec![], vec![])
    );

    let bom_file = tmp.path().join(".env.bom");
    fs::write(&bom_file, b"\xef\xbb\xbfFIRST=1\n").unwrap();
    let (loaded, problems) = load_env_file_map(&bom_file, &mut environ);
    assert_eq!(loaded, vec!["FIRST"]);
    assert!(problems.is_empty());
    assert_eq!(environ["FIRST"], "1");

    let bad_utf8 = tmp.path().join(".env.bad");
    fs::write(&bad_utf8, b"GOOD=1\nSECRET=caf\xe9-placeholder\n").unwrap();
    let mut environ2 = BTreeMap::new();
    let (loaded2, probs2) = load_env_file_map(&bad_utf8, &mut environ2);
    assert!(loaded2.is_empty());
    assert!(environ2.is_empty());
    assert_eq!(probs2, vec!["not valid UTF-8 text; file skipped"]);

    // Unreadable permissions (note: skipped when running as root)
    let locked = tmp.path().join(".env.locked");
    fs::write(&locked, "A=1\n").unwrap();
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&locked).is_err() {
        let mut environ3 = BTreeMap::new();
        let (loaded3, probs3) = load_env_file_map(&locked, &mut environ3);
        assert!(loaded3.is_empty());
        assert_eq!(probs3, vec!["unreadable (PermissionError); file skipped"]);
    }
    let _ = fs::set_permissions(&locked, fs::Permissions::from_mode(0o600));
}

#[test]
fn env_scope_and_paired_api_key_env() {
    assert!(is_live_command(&["run"]));
    assert!(is_live_command(&["audit", "panel"]));
    assert!(is_live_command(&["v41", "run"]));
    assert!(!is_live_command(&["v41", "score"]));
    assert!(!is_live_command(&["v41"]));

    let mut env = BTreeMap::new();
    let tmp = TempDir::new().unwrap();
    let env_path = tmp.path().join(".env");
    fs::write(
        &env_path,
        "IDENTITYBENCH_PRICING=p.yaml\nOPENAI_API_KEY=sk-1234567890\n",
    )
    .unwrap();
    let (loaded, _) = load_env_file_into(&env_path, &mut env, |k| {
        env_name_allowed(&["v41", "score"], k)
    });
    assert_eq!(loaded, vec!["IDENTITYBENCH_PRICING"]);
    assert!(!env.contains_key("OPENAI_API_KEY"));

    assert_eq!(
        paired_api_key_env("LOCAL_LLM_BASE_URL").as_deref(),
        Some("LOCAL_LLM_API_KEY")
    );
    assert_eq!(
        paired_api_key_env("GATEWAY_URL").as_deref(),
        Some("GATEWAY_API_KEY")
    );
    assert_eq!(paired_api_key_env("OPENAI_BASE_URL"), None);
    assert_eq!(paired_api_key_env("ANTHROPIC_BASE_URL"), None);
    assert_eq!(paired_api_key_env("OPENAI_URL"), None);
    assert_eq!(paired_api_key_env("CUSTOM_ENDPOINT"), None);
    assert_eq!(paired_api_key_env("_URL"), None);
}
