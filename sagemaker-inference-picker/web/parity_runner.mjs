/**
 * Runs workloads through the JavaScript engine and prints the decisions as JSON.
 *
 * Used by `tests/test_web_parity.py`, which feeds it the same scenarios the Python engine
 * is tested against and compares the two sets of decisions.
 *
 * Usage: node web/parity_runner.mjs <limits.json> <workloads.json>
 */

import { readFileSync } from "node:fs";

import { recommend } from "./engine.mjs";

const [limitsPath, workloadsPath] = process.argv.slice(2);
if (!limitsPath || !workloadsPath) {
  console.error("usage: node web/parity_runner.mjs <limits.json> <workloads.json>");
  process.exit(2);
}

const limits = JSON.parse(readFileSync(limitsPath, "utf8"));
const workloads = JSON.parse(readFileSync(workloadsPath, "utf8"));

const decisions = workloads.map((workload) => {
  const result = recommend(workload, limits);
  return {
    recommended: result.recommended,
    resolved: result.resolved,
    ranked: result.ranked.map((item) => [item.option, item.score]),
    eliminations: result.eliminations.map((item) => [item.option, item.constraint_id]),
    advice: result.advice.map((item) => item.id),
    conflicts: result.conflicts.map((item) => [item.constraint_id, item.eliminated]),
  };
});

process.stdout.write(JSON.stringify(decisions));
