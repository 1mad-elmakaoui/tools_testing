/**
 * The decision engine, in JavaScript, for the static page.
 *
 * This mirrors `rules.py` and `engine.py`. It is a second implementation of the same
 * rules, which is a drift risk, so `tests/test_web_parity.py` runs a corpus of workloads
 * through both and fails the build if the two disagree on any decision.
 *
 * It reads the very same numbers as the CLI: the page is generated from `limits.yaml`, so
 * neither a limit nor a preference weight is written down twice.
 */

export const OPTIONS = ["real_time", "serverless", "async", "batch_transform"];

export const TRAFFIC_PATTERNS = ["steady", "bursty-idle", "scheduled-batch"];

const MB_PER_GB = 1024;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;

/* ------------------------------------------------------------------ units ---- */

export function num(value) {
  return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(6)));
}

export function megabytes(value) {
  if (value === null || value === undefined) return "no documented limit";
  if (value >= MB_PER_GB && value % MB_PER_GB === 0) {
    return `${num(value)} MB (${num(value / MB_PER_GB)} GB)`;
  }
  return `${num(value)} MB`;
}

function durationGloss(value) {
  if (value >= SECONDS_PER_HOUR && value % SECONDS_PER_HOUR === 0) {
    const hours = value / SECONDS_PER_HOUR;
    return hours === 1 ? `${num(hours)} hour` : `${num(hours)} hours`;
  }
  if (value >= 2 * SECONDS_PER_MINUTE && value % SECONDS_PER_MINUTE === 0) {
    return `${num(value / SECONDS_PER_MINUTE)} minutes`;
  }
  return null;
}

export function seconds(value) {
  if (value === null || value === undefined) return "no documented limit";
  const gloss = durationGloss(value);
  return gloss === null ? `${num(value)} s` : `${num(value)} s (${gloss})`;
}

export function milliseconds(value) {
  return `${num(value)} ms`;
}

/* ------------------------------------------------------- limits accessors ---- */

const optionOf = (limits, option) => limits.options[option];
const limitOf = (limits, option, name) => optionOf(limits, option).limits[name];
const capabilityOf = (limits, option, name) => optionOf(limits, option).capabilities[name];
const factOf = (limits, option, name) => optionOf(limits, option).facts[name];
const displayName = (limits, option) => optionOf(limits, option).display_name;

function heuristic(limits, name) {
  const entry = limits.heuristics[name];
  if (entry === undefined) throw new Error(`unknown heuristic ${name}`);
  return entry.value;
}

function withNote(message, note) {
  return note ? `${message} ${note}` : message;
}

export function requiresInlineResponse(workload) {
  return workload.immediateResponse || workload.latencyP99Ms !== null;
}

/* ----------------------------------------------------- hard constraints ---- */

function overLimit(limits, option, workload, { id, limitName, actual, requirement, sentence }) {
  const cap = limitOf(limits, option, limitName);
  if (cap.value === null || actual <= cap.value) return null;
  return {
    option,
    constraint_id: id,
    requirement: requirement(actual),
    message: withNote(sentence(displayName(limits, option), cap.value), cap.note),
    source: cap.source,
    limit: limitName === "max_processing_seconds" ? seconds(cap.value) : megabytes(cap.value),
    actual: limitName === "max_processing_seconds" ? seconds(actual) : megabytes(actual),
  };
}

function missingCapability(limits, option, { id, name, requirement, sentence }) {
  const capability = capabilityOf(limits, option, name);
  if (capability.value) return null;
  return {
    option,
    constraint_id: id,
    requirement,
    message: withNote(sentence(displayName(limits, option)), capability.note),
    source: capability.source,
    limit: null,
    actual: null,
  };
}

export function hardConstraints(workload, limits, option) {
  const found = [];

  found.push(
    overLimit(limits, option, workload, {
      id: "request_payload_over_limit",
      limitName: "max_request_payload_mb",
      actual: workload.payloadMb,
      requirement: (v) => `request payload of ${megabytes(v)}`,
      sentence: (name, cap) => `${name} accepts at most ${megabytes(cap)} per request.`,
    }),
  );
  found.push(
    overLimit(limits, option, workload, {
      id: "response_payload_over_limit",
      limitName: "max_response_payload_mb",
      actual: workload.responseMb,
      requirement: (v) => `response payload of ${megabytes(v)}`,
      sentence: (name, cap) => `${name} returns at most ${megabytes(cap)} per response.`,
    }),
  );
  found.push(
    overLimit(limits, option, workload, {
      id: "processing_time_over_limit",
      limitName: "max_processing_seconds",
      actual: workload.processingSeconds,
      requirement: (v) => `processing time of ${seconds(v)} per request`,
      sentence: (name, cap) => `${name} allows at most ${seconds(cap)} per invocation.`,
    }),
  );

  if (workload.gpuRequired) {
    found.push(
      missingCapability(limits, option, {
        id: "gpu_not_supported",
        name: "gpu_supported",
        requirement: "a GPU is required",
        sentence: (name) => `${name} does not support GPU instances.`,
      }),
    );
  }
  if (workload.zeroIdleCost) {
    found.push(
      missingCapability(limits, option, {
        id: "cannot_scale_to_zero",
        name: "scales_to_zero",
        requirement: "must cost nothing when idle",
        sentence: (name) =>
          `${name} keeps at least one instance running between requests, so it bills while idle.`,
      }),
    );
  }
  if (requiresInlineResponse(workload)) {
    const requirement = workload.immediateResponse
      ? "the caller needs an immediate response"
      : `a p99 latency target of ${milliseconds(workload.latencyP99Ms)} implies the caller ` +
        `waits for the result`;
    found.push(
      missingCapability(limits, option, {
        id: "no_inline_response",
        name: "returns_inline_response",
        requirement,
        sentence: (name) => `${name} never returns the prediction to the caller.`,
      }),
    );
  }
  if (workload.needsNotification) {
    found.push(
      missingCapability(limits, option, {
        id: "no_native_completion_notification",
        name: "native_completion_notification",
        requirement: "a completion notification is required",
        sentence: (name) => `${name} has no native completion notification.`,
      }),
    );
  }

  return found.filter((item) => item !== null);
}

/* ---------------------------------------------------------- preferences ---- */

export function preferences(workload, limits) {
  const found = [];
  const add = (option, ruleId, message) =>
    found.push({ option, rule_id: ruleId, weight: heuristic(limits, ruleId), message });

  if (workload.traffic === "steady") {
    add(
      "real_time",
      "steady_traffic_favours_real_time",
      "Traffic is steady, which keeps provisioned instances busy — the one case where " +
        "paying for always-on capacity is efficient.",
    );
  } else if (workload.traffic === "bursty-idle") {
    add(
      "serverless",
      "bursty_traffic_favours_serverless",
      "Traffic is bursty with idle periods, so paying per request beats paying for " +
        "instances that sit idle between bursts.",
    );
    add(
      "async",
      "bursty_traffic_favours_async",
      "Traffic is bursty with idle periods, and an asynchronous endpoint scales to zero " +
        "between bursts too, adding queueing and an Amazon S3 round trip.",
    );
  } else if (workload.traffic === "scheduled-batch") {
    add(
      "batch_transform",
      "scheduled_batch_favours_batch_transform",
      "The work is a known dataset on a schedule, which is what a transform job is: " +
        "instances exist only while the job runs.",
    );
    add(
      "async",
      "scheduled_batch_favours_async",
      "An asynchronous endpoint can drain a scheduled backlog, but leaves an endpoint to " +
        "operate that a transform job does not.",
    );
  }

  if (workload.latencyP99Ms !== null) {
    const target = milliseconds(workload.latencyP99Ms);
    if (workload.latencyP99Ms < heuristic(limits, "tight_latency_p99_ms")) {
      add(
        "real_time",
        "tight_latency_favours_real_time",
        `A p99 target of ${target} needs warm instances, which a real-time endpoint keeps ` +
          `warm by construction.`,
      );
      const coldStart = factOf(limits, "serverless", "worst_case_cold_start_seconds");
      add(
        "serverless",
        "tight_latency_penalises_serverless",
        `A serverless cold start can add up to ${seconds(coldStart.value)}, which would ` +
          `miss a p99 target of ${target} by a wide margin unless you add provisioned ` +
          `concurrency.`,
      );
    }
  }

  if (!requiresInlineResponse(workload)) {
    add(
      "async",
      "offline_result_favours_async",
      "Nobody is waiting on the response, so queueing the request costs nothing in " +
        "user-visible latency.",
    );
    add(
      "batch_transform",
      "offline_result_favours_batch_transform",
      "Nobody is waiting on the response, so a job with no endpoint to operate is the " +
        "cheapest thing to run.",
    );
  }

  if (workload.modelCount >= heuristic(limits, "multi_model_endpoint_min_models")) {
    add(
      "real_time",
      "many_models_favour_real_time",
      `With ${workload.modelCount} models, it matters that real-time is the only one of ` +
        `the four options supporting multi-model endpoints and inference components.`,
    );
  }

  return found;
}

/* --------------------------------------------------------------- advice ---- */

export function advice(workload, limits, winner, eliminations) {
  const found = [];
  const pattern = (key) => {
    const entry = limits.patterns[key];
    if (entry === undefined) throw new Error(`unknown pattern ${key}`);
    return entry;
  };

  if (winner === "serverless" && workload.latencyP99Ms !== null) {
    const item = pattern("provisioned_concurrency");
    const coldStart = factOf(limits, "serverless", "worst_case_cold_start_seconds");
    found.push({
      id: "provisioned_concurrency",
      title: item.display_name,
      message:
        `You set a p99 target of ${milliseconds(workload.latencyP99Ms)}, but a serverless ` +
        `cold start can reach ${seconds(coldStart.value)}. ${item.note}`,
      source: item.source,
    });
  }

  if (winner === "real_time") {
    const mmeMinimum = heuristic(limits, "multi_model_endpoint_min_models");
    if (
      workload.modelCount >= mmeMinimum &&
      workload.traffic === "bursty-idle" &&
      capabilityOf(limits, "real_time", "supports_multi_model_endpoint").value
    ) {
      const item = pattern("multi_model_endpoint");
      found.push({
        id: "multi_model_endpoint",
        title: item.display_name,
        message:
          `${workload.modelCount} models that are mostly idle is the case a multi-model ` +
          `endpoint is built for: one set of instances, each model loaded on demand. ` +
          `${item.note}`,
        source: item.source,
      });
    }
    if (workload.modelCount >= heuristic(limits, "inference_components_min_models")) {
      const item = pattern("inference_components");
      found.push({
        id: "inference_components",
        title: item.display_name,
        message:
          `With ${workload.modelCount} models on one endpoint, inference components let ` +
          `each get its own resources and scale separately. ${item.note}`,
        source: item.source,
      });
    }
  }

  const pushedOffline =
    winner !== null && !capabilityOf(limits, winner, "returns_inline_response").value;
  if (workload.zeroIdleCost && (winner === null || pushedOffline)) {
    const blockers = new Set(
      eliminations.filter((e) => e.option === "real_time").map((e) => e.constraint_id),
    );
    if (blockers.size === 1 && blockers.has("cannot_scale_to_zero")) {
      const item = pattern("inference_components");
      found.push({
        id: "inference_components_scale_to_zero",
        title: `${item.display_name} (scale to zero)`,
        message:
          "A real-time endpoint was ruled out only because it bills while idle. That is " +
          "the one constraint inference components can lift: an inference-component " +
          "endpoint can scale to zero instances, so it is worth evaluating if the rest of " +
          `real-time suits you. ${item.note}`,
        source: item.source,
      });
    }
  }

  return found;
}

/* ------------------------------------------------------------ conflicts ---- */

export function conflicts(eliminations) {
  const order = [];
  const requirements = new Map();
  const eliminated = new Map();
  for (const item of eliminations) {
    if (!requirements.has(item.constraint_id)) {
      order.push(item.constraint_id);
      requirements.set(item.constraint_id, item.requirement);
      eliminated.set(item.constraint_id, []);
    }
    const options = eliminated.get(item.constraint_id);
    if (!options.includes(item.option)) options.push(item.option);
  }
  const built = order.map((id) => ({
    constraint_id: id,
    requirement: requirements.get(id),
    eliminated: eliminated.get(id),
  }));
  built.sort((a, b) => {
    if (a.eliminated.length !== b.eliminated.length) {
      return b.eliminated.length - a.eliminated.length;
    }
    return a.constraint_id < b.constraint_id ? -1 : a.constraint_id > b.constraint_id ? 1 : 0;
  });
  return built;
}

/* --------------------------------------------------------------- engine ---- */

export function recommend(workload, limits) {
  const eliminations = OPTIONS.flatMap((option) => hardConstraints(workload, limits, option));
  const eliminatedOptions = new Set(eliminations.map((item) => item.option));
  const survivors = OPTIONS.filter((option) => !eliminatedOptions.has(option));

  const allPreferences = preferences(workload, limits);
  const tieBreak = new Map(limits.tie_break_order.map((option, index) => [option, index]));

  const ranked = survivors
    .map((option) => {
      const reasons = allPreferences.filter((item) => item.option === option);
      const score = reasons.reduce((total, item) => total + item.weight, 0);
      return { option, score: Math.round(score * 1e6) / 1e6, reasons };
    })
    .sort((a, b) =>
      a.score !== b.score ? b.score - a.score : tieBreak.get(a.option) - tieBreak.get(b.option),
    );

  const recommended = ranked.length > 0 ? ranked[0].option : null;

  return {
    recommended,
    resolved: recommended !== null,
    ranked,
    eliminations,
    advice: advice(workload, limits, recommended, eliminations),
    conflicts: recommended === null ? conflicts(eliminations) : [],
  };
}

/* -------------------------------------------------------------- explain ---- */

export function headline(result, limits) {
  if (result.recommended === null) {
    return "No SageMaker inference option satisfies all of these requirements.";
  }
  const name = displayName(limits, result.recommended);
  if (result.ranked.length === 1) {
    return `Use ${name} — the only option that satisfies every hard constraint.`;
  }
  if (result.ranked[0].score === result.ranked[1].score) {
    const runnerUp = displayName(limits, result.ranked[1].option);
    return (
      `Use ${name}, narrowly — it scores level with ${runnerUp} and wins the tie on ` +
      `having less always-on infrastructure to run.`
    );
  }
  return `Use ${name}.`;
}

export function decidingFactors(result, limits) {
  if (result.recommended === null || result.ranked.length === 0) return [];
  const top = result.ranked[0];
  const factors = top.reasons.filter((item) => item.weight > 0).map((item) => item.message);
  if (result.ranked.length === 1) {
    factors.push("Every other option was ruled out by a hard constraint, listed below.");
  } else if (factors.length === 0) {
    factors.push(
      "Nothing about this workload favours one surviving option over another, so the " +
        `tie-break decided it: ${limits.tie_break_rationale}`,
    );
  }
  return factors;
}

export function caveats(result) {
  if (result.ranked.length === 0) return [];
  return result.ranked[0].reasons.filter((item) => item.weight < 0).map((item) => item.message);
}

export function runnersUp(result, limits) {
  if (result.recommended === null || result.ranked.length < 2) return [];
  const winner = result.ranked[0];
  return result.ranked.slice(1).map(
    (item) =>
      `${displayName(limits, item.option)} also satisfies every hard constraint, but fits ` +
      `this workload less well (scored ${num(item.score)} against ${num(winner.score)}).`,
  );
}

export function rejections(result, limits) {
  const built = [];
  for (const option of OPTIONS) {
    const items = result.eliminations.filter((item) => item.option === option);
    if (items.length === 0) continue;
    const sources = [];
    for (const item of items) if (!sources.includes(item.source)) sources.push(item.source);
    built.push({
      option,
      display_name: displayName(limits, option),
      reasons: items.map((item) => item.message),
      sources,
    });
  }
  return built;
}

function joinNames(items) {
  if (items.length === 1) return items[0];
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

export function conflictSummary(result, limits) {
  if (result.recommended !== null || result.conflicts.length === 0) return [];
  const lines = result.conflicts.map((conflict) => {
    const names = conflict.eliminated.map((option) => displayName(limits, option));
    const requirement = conflict.requirement;
    const capitalised = requirement.charAt(0).toUpperCase() + requirement.slice(1);
    return `${capitalised} — rules out ${joinNames(names)}.`;
  });
  lines.push(
    "No single SageMaker option covers all of these at once. Drop or relax one of them, or " +
      "split the workload so that different requirements are served by different options.",
  );
  return lines;
}
