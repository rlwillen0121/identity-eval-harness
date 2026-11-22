#!/usr/bin/env node
/**
 * Import a v5.1 run's publishable artifacts into reports/v51/runs/<name>/.
 *
 * Copies summary.json, report.md, and manifest.json verbatim and writes a compact
 * episodes.json (per-slot status, turns, score, and usage). Committed configs,
 * transcripts, tool traces, and commit notes are NOT copied; they stay with the raw run.
 *
 * Usage: node scripts/import-v51-run.mjs <raw_run_dir> [<name>]
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const [runDir, nameArg] = process.argv.slice(2);
if (!runDir) {
  console.error('usage: node scripts/import-v51-run.mjs <raw_run_dir> [<name>]');
  process.exit(2);
}
const name = nameArg || path.basename(path.resolve(runDir));
if (!/^[a-z0-9][a-z0-9_.-]*$/.test(name)) throw new Error(`invalid run name: ${name}`);

const summary = JSON.parse(fs.readFileSync(path.join(runDir, 'summary.json'), 'utf8'));
if (summary.spec_version !== 'SPEC-v5.1') throw new Error(`not a v5.1 run: ${summary.spec_version}`);

const pick = (score) => score && {
  g_srr: score.g_srr,
  state_accuracy: score.state_accuracy,
  passed_assertions: score.passed_assertions,
  total_assertions: score.total_assertions,
  category_pass: score.category_pass,
  nhi_custody_pass: score.nhi_custody_pass,
  agent_delegation_pass: score.agent_delegation_pass,
  iiq_to_isc_pass: score.iiq_to_isc_pass ?? null,
  overgrant_count: score.overgrant_count,
  availability_breach_count: score.availability_breach_count,
};

const episodes = fs.readFileSync(path.join(runDir, 'episodes.jsonl'), 'utf8')
  .split('\n')
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line))
  .map((r) => ({
    episode_id: r.episode_id,
    family_id: r.family_id,
    repeat_index: r.repeat_index,
    attempt_status: r.attempt_status,
    termination_reason: r.termination_reason,
    turns_used: r.turns_used,
    dry_runs_used: r.dry_runs_used,
    committed: r.committed,
    score: pick(r.score) ?? null,
    input_tokens: r.input_tokens,
    cached_input_tokens: r.cached_input_tokens,
    output_tokens: r.output_tokens,
    reasoning_tokens: r.reasoning_tokens,
    cost_usd_micros: r.cost_usd_micros,
    elapsed_wall_ms: r.elapsed_wall_ms,
  }))
  .sort((a, b) => a.episode_id.localeCompare(b.episode_id) || a.repeat_index - b.repeat_index);

const out = path.join(ROOT, 'reports/v51/runs', name);
fs.mkdirSync(out, { recursive: true });
for (const file of ['summary.json', 'report.md', 'manifest.json']) {
  const src = path.join(runDir, file);
  if (fs.existsSync(src)) fs.copyFileSync(src, path.join(out, file));
}
fs.writeFileSync(path.join(out, 'episodes.json'), `${JSON.stringify(episodes, null, 1)}\n`);
console.log(`imported ${name}: ${summary.observed_slots}/${summary.planned_slots} observed, complete=${summary.is_complete}`);
