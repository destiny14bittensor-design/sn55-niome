const path = require("path");

const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv", "bin", "python");
const requiredEnv = (name) => {
  const value = (process.env[name] || "").trim();
  if (!value) {
    throw new Error(`Missing required environment variable ${name}`);
  }
  return value;
};
const externalIp = requiredEnv("NIOME_EXTERNAL_IP");
const walletPath = requiredEnv("NIOME_WALLET_PATH");
const scoreCalibrationModel = requiredEnv("NIOME_SCORE_CALIBRATION_MODEL");
const validatorWalletPath =
  process.env.NIOME_SEED_VALIDATOR_WALLET_PATH ||
  "/home/administrator/.bittensor/wallets";

const allLanes = [
  {
    id: "tao1",
    wallet: requiredEnv("NIOME_TAO1_WALLET"),
    hotkey: requiredEnv("NIOME_TAO1_HOTKEY"),
    miner: "niome-tao1",
    bridge: "niome-seed-bridge-tao1",
    port: 8091,
    artifactRoot: path.join(root, "artifacts", "miners", "tao1"),
    builderPolicy: "champion-v1",
    explorationProfile:
      process.env.NIOME_TAO1_SS58 ||
      "5GbhpWKt2SYHaZMHNy2WAsm5pzkGL5sRa7DFnYu9zJY3qYGC",
  },
  {
    id: "tao2",
    wallet: requiredEnv("NIOME_TAO2_WALLET"),
    hotkey: requiredEnv("NIOME_TAO2_HOTKEY"),
    miner: "niome-tao2",
    bridge: "niome-seed-bridge-tao2",
    port: 8092,
    artifactRoot: path.join(root, "artifacts", "miners", "tao2"),
    builderPolicy: "champion-reservoir003-cas65-v3",
    explorationProfile:
      process.env.NIOME_TAO2_SS58 ||
      "5Ehx52VbhGyvmZVcvaRF2dG8JVJUMHHreRBLRsGMkJ695zih",
  },
  {
    id: "won1",
    wallet: requiredEnv("NIOME_WON1_WALLET"),
    hotkey: requiredEnv("NIOME_WON1_HOTKEY"),
    miner: "niome-won1",
    bridge: "niome-seed-bridge-won1",
    port: 8093,
    artifactRoot: path.join(root, "artifacts", "miners", "won1"),
    builderPolicy: "champion-reservoir003-cas65-v3",
    explorationProfile:
      process.env.NIOME_WON1_SS58 ||
      "5CPsx7spR4FCr786VBTqxuNGaoWnMfe91fcQKEYig3eVbTbi",
  },
  {
    id: "won2",
    wallet: requiredEnv("NIOME_WON2_WALLET"),
    hotkey: requiredEnv("NIOME_WON2_HOTKEY"),
    miner: "niome-won2",
    bridge: "niome-seed-bridge-won2",
    port: 8094,
    artifactRoot: path.join(root, "artifacts", "miners", "won2"),
    builderPolicy: "champion-reservoir005-v3",
    explorationProfile:
      process.env.NIOME_WON2_SS58 ||
      "5E6ttv44E9Ko8NAsYerT4atZummaYxYXGzkTNHNUgXmXEvb4",
  },
];

const requestedLaneIds = (
  process.env.NIOME_FLEET_LANES || "tao1,tao2,won1,won2"
)
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);
const knownLaneIds = new Set(allLanes.map((lane) => lane.id));
const unknownLaneIds = requestedLaneIds.filter((lane) => !knownLaneIds.has(lane));
if (unknownLaneIds.length) {
  throw new Error(`Unknown NIOME_FLEET_LANES: ${unknownLaneIds.join(",")}`);
}
if (!requestedLaneIds.length) {
  throw new Error("NIOME_FLEET_LANES must select at least one lane");
}
const lanes = requestedLaneIds.map(
  (laneId) => allLanes.find((lane) => lane.id === laneId),
);
const enableSeedBridges = process.env.NIOME_ENABLE_SEED_BRIDGES === "true";
const enableLegacySeedResearch =
  process.env.NIOME_ENABLE_LEGACY_SEED_RESEARCH === "true";

const bridgeApps = lanes.map((lane) => ({
  name: lane.bridge,
  cwd: root,
  script: "tools/live_seed_bridge.py",
  interpreter: python,
  autorestart: true,
  watch: false,
  max_memory_restart: "1200M",
  kill_timeout: 10000,
  env: {
    NIOME_ARTIFACT_ROOT: lane.artifactRoot,
    NIOME_ENABLE_UNVERIFIED_SAME_ROUND_OVERWRITE: "false",
    NIOME_SEED_VALIDATOR_WALLET_NAME:
      process.env.NIOME_SEED_VALIDATOR_WALLET_NAME || "main",
    NIOME_SEED_VALIDATOR_WALLET_HOTKEY:
      process.env.NIOME_SEED_VALIDATOR_WALLET_HOTKEY || "dollar2",
    NIOME_SEED_VALIDATOR_WALLET_PATH: validatorWalletPath,
    ...(lane.explorationProfile
      ? { NIOME_EXPLORATION_PROFILE: lane.explorationProfile }
      : {}),
  },
}));

const minerApps = lanes.map((lane) => ({
  name: lane.miner,
  cwd: root,
  script: python,
  interpreter: "none",
  args: [
    "neurons/miner.py",
    "--netuid 55",
    "--network finney",
    `--wallet ${lane.wallet}`,
    `--wallet-hotkey ${lane.hotkey}`,
    `--wallet-path ${walletPath}`,
    `--axon.port ${lane.port}`,
    `--axon.external-ip ${externalIp}`,
    "--blacklist.force_validator_permit",
  ].join(" "),
  autorestart: true,
  watch: false,
  max_memory_restart: "1200M",
  kill_timeout: 15000,
  env: {
    PYTHONPATH: root,
    NIOME_ARTIFACT_ROOT: lane.artifactRoot,
    NIOME_BUILDER_POLICY: lane.builderPolicy,
  },
}));

const dashboard = {
  name: "niome-dashboard",
  cwd: root,
  script: "tools/niome_dashboard.py",
  interpreter: python,
  autorestart: true,
  watch: false,
  max_memory_restart: "450M",
  kill_timeout: 10000,
  env: {
    NIOME_DASH_HOST: "0.0.0.0",
    NIOME_DASH_PORT: "8111",
    NIOME_DASH_NETWORK: "finney",
    NIOME_TARGET_RANK: "30",
    NIOME_DASH_GUIDE_VARIANTS: "72",
    NIOME_DASH_PRIMARY_CAS_SHARE: "0.60",
    NIOME_DASH_TARGET_RANK:
      process.env.NIOME_DASH_TARGET_RANK || process.env.NIOME_TARGET_RANK || "30",
    NIOME_FLEET_LANES: requestedLaneIds.join(","),
    NIOME_FEDERATION_LOCAL_ID: "tao-won-local",
    NIOME_FEDERATION_LOCAL_LABEL:
      process.env.NIOME_FEDERATION_LOCAL_LABEL || "Tao / Won Local",
    NIOME_FEDERATION_REMOTE_SOURCES: "[]",
    NIOME_SCORE_CALIBRATION_MODEL: scoreCalibrationModel,
    NIOME_SEED_RESEARCH_STATE: path.join(root, "artifacts", "research", "seed_research_state.json"),
  },
};

const seedProbeSupervisor = {
  name: "niome-seed-probe-supervisor",
  cwd: root,
  script: "tools/seed_probe_supervisor.py",
  interpreter: python,
  args: [
    "artifacts/live",
    "--miner-hotkey 5Ge44ZvzfkxGYXwnbPmnCGfzatGUtfiXSXUcTWGNcv6DMLXz",
    "--output-root artifacts/research/probe_rounds",
    "--chromosome data/chr11.fa",
    "--workers 2",
    "--seed-min 100",
    "--seed-max 999",
    "--poll-interval 5",
    "--score-timeout 10800",
  ].join(" "),
  autorestart: true,
  watch: false,
  max_memory_restart: "3500M",
  kill_timeout: 15000,
};

const seedResearchOrchestrator = {
  name: "niome-seed-research",
  cwd: root,
  script: "tools/seed_research_orchestrator.py",
  interpreter: python,
  args: [
    "--output artifacts/research/seed_research_state.json",
    "--predictions artifacts/research/seed_shadow_predictions.json",
    "--prng-report artifacts/research/seed_prng_fingerprint.json",
    "--probe-root artifacts/research",
    "--probe-root artifacts/research/probe_rounds",
    "--supervisor-state artifacts/research/probe_rounds/seed_probe_supervisor.json",
    "--generator-report artifacts/research/preseed_generator_state.json",
    "--generator-ledger artifacts/research/preseed_predictions.json",
    "--extended-generator-report artifacts/research/preseed_extended_search.json",
    "--extended-generator-report artifacts/research/preseed_drand_search.json",
    "--extended-generator-report artifacts/research/preseed_collective_flip_search.json",
    "--extended-generator-report artifacts/research/preseed_stateful_prng_search.json",
    "--extended-generator-report artifacts/research/preseed_stateful_prelude_search.json",
    "--extended-generator-report artifacts/research/preseed_numpy_interleaved_search.json",
    "--extended-generator-report artifacts/research/preseed_numpy_prelude_search.json",
    "--extended-generator-report artifacts/research/preseed_validation_time_search.json",
    "--extended-generator-report artifacts/research/preseed_v8_state_recovery.json",
    "--extended-generator-report artifacts/research/preseed_seedsequence_search.json",
    "--extended-generator-report artifacts/research/preseed_runtime_identity_search.json",
    "--extended-generator-report artifacts/research/preseed_event_clock_search.json",
    "--extended-generator-report artifacts/research/preseed_event_clock_independent_seed_log.json",
    "--extended-generator-report artifacts/research/preseed_event_clock_independent_validation.json",
    "--extended-generator-report artifacts/research/preseed_previous_seed_recurrence.json",
    "--extended-generator-report artifacts/research/preseed_numpy_uint32_exhaustive.json",
    "--extended-generator-report artifacts/research/preseed_python_uint32_exhaustive.json",
    "--extended-generator-report artifacts/research/preseed_numpy_uint32_hidden_gap.json",
    "--extended-generator-report artifacts/research/preseed_python_uint32_hidden_gap.json",
    "--extended-generator-report artifacts/research/preseed_numpy_uint32_first_shuffle.json",
    "--extended-generator-report artifacts/research/preseed_python_uint32_first_target.json",
    "--extended-generator-report artifacts/research/preseed_python_uint32_first_target_hidden_gap.json",
    "--extended-generator-report artifacts/research/preseed_python_float_uint32_first_target_hidden_gap.json",
    "--extended-generator-report artifacts/research/preseed_python_uint32_path_candidate_3538549647.json",
    "--extended-generator-report artifacts/research/preseed_v8_cnf_recovery.json",
    "--extended-generator-report artifacts/research/preseed_lcg_state_recovery.json",
    "--extended-generator-report artifacts/research/preseed_python_uint32_round_reseed.json",
    "--extended-generator-report artifacts/research/preseed_numpy_uint32_round_reseed.json",
    "--extended-generator-report artifacts/research/preseed_wandb_runtime_identity_search.json",
    "--extended-generator-report artifacts/research/preseed_pcg64_uint32_persistent.json",
    "--extended-generator-report artifacts/research/preseed_pcg64_uint32_persistent_choice.json",
    "--extended-generator-report artifacts/research/preseed_pcg64dxsm_uint32_persistent.json",
    "--extended-generator-report artifacts/research/preseed_pcg64dxsm_uint32_persistent_choice.json",
    "--extended-generator-report artifacts/research/preseed_sfc64_uint32_persistent.json",
    "--extended-generator-report artifacts/research/preseed_sfc64_uint32_persistent_choice.json",
    "--extended-generator-report artifacts/research/preseed_philox_uint32_persistent.json",
    "--extended-generator-report artifacts/research/preseed_philox_uint32_persistent_choice.json",
    "--extended-generator-report artifacts/research/preseed_pcg64_uint32_persistent_float.json",
    "--extended-generator-report artifacts/research/preseed_pcg64dxsm_uint32_persistent_float.json",
    "--extended-generator-report artifacts/research/preseed_sfc64_uint32_persistent_float.json",
    "--extended-generator-report artifacts/research/preseed_philox_uint32_persistent_float.json",
    "--extended-generator-report artifacts/research/preseed_modern_uint32_fixed_gap.json",
    "--extended-generator-report artifacts/research/preseed_modern_uint32_fixed_gap_choice.json",
    "--extended-generator-report artifacts/research/preseed_modern_uint32_fixed_gap_float.json",
    "--extended-generator-report artifacts/research/preseed_block_initializer_stream_search.json",
    "--extended-generator-report artifacts/research/preseed_composite_prng_search.json",
    "--shuffle-leak-report artifacts/research/preseed_shuffle_leak_audit.json",
    "--shuffle-alignment-report artifacts/research/preseed_shuffle_metagraph_alignment.json",
    "--shuffle-constraints-report artifacts/research/preseed_numpy_shuffle_constraints.json",
    "--fisher-yates-report artifacts/research/preseed_fisher_yates_model.json",
    "--shuffle-prefix-report artifacts/research/preseed_shuffle_prefix_domains.json",
    "--shuffle-prefix-tuple-report artifacts/research/preseed_shuffle_prefix_tuples.json",
    "--mt-prefix-cpsat-report artifacts/research/preseed_mt_prefix_cpsat.json",
    "--mt-joint-cpsat-report artifacts/research/preseed_mt_cpsat_joint.json",
    "--mt-z3-joint-report artifacts/research/preseed_mt_z3_joint_norej.json",
    "--mt-xorsat-joint-report artifacts/research/preseed_mt_xorsat_joint_integer_20real_trace4_sealed.json",
    "--mt-fixed-profile-report artifacts/research/preseed_mt_fixed_profile_corridor1_round1_network_forecast.json",
    "--mt-incremental-label-report artifacts/research/preseed_mt_incremental_labels_corridor1_r8_from4_checkpoint.json",
    "--postgres-double-report artifacts/research/preseed_postgres_cnf_recovery.json",
    "--postgres-range-report artifacts/research/preseed_postgres_range_cnf.json",
    "--identifiability-report artifacts/research/preseed_identifiability_gate.json",
    "--mt-rank-audit-report artifacts/research/preseed_mt19937_rank_audit.json",
    "--early-score-root artifacts/research/early_score_rounds",
    "--early-score-supervisor-state artifacts/research/early_score_supervisor.json",
    `--signal-root ${path.join(root, "artifacts", "miners", "won2")}`,
    "--poll-interval 10",
    "--max-pages 3",
  ].join(" "),
  autorestart: true,
  watch: false,
  max_memory_restart: "600M",
  kill_timeout: 10000,
};

const publicChainEntropyCollector = {
  name: "niome-public-chain-entropy",
  cwd: root,
  script: "tools/public_chain_entropy_collector.py",
  interpreter: python,
  args: [
    "--output artifacts/research/public_chain_headers.jsonl",
    "--state artifacts/research/public_chain_collector_state.json",
    "--network finney",
    "--poll-interval 6",
    "--initial-backfill 120",
  ].join(" "),
  autorestart: true,
  watch: false,
  max_memory_restart: "450M",
  kill_timeout: 10000,
};

const preseedGeneratorSupervisor = {
  name: "niome-preseed-generator",
  cwd: root,
  script: "tools/preseed_generator_supervisor.py",
  interpreter: python,
  args: [
    "--report artifacts/research/preseed_generator_state.json",
    "--ledger artifacts/research/preseed_predictions.json",
    "--cache artifacts/research/preseed_chain_context.json",
    "--dataset-root artifacts/research/preseed_datasets",
    "--network finney",
    "--poll-interval 30",
    "--max-pages 3",
    "--max-age-blocks 50000",
  ].join(" "),
  autorestart: true,
  watch: false,
  max_memory_restart: "700M",
  kill_timeout: 15000,
};

const preseedShuffleSupervisor = {
  name: "niome-preseed-shuffle",
  cwd: root,
  script: "tools/preseed_shuffle_supervisor.py",
  interpreter: python,
  args: [
    "--root .",
    "--discovery artifacts/research/preseed_datasets/discovery.json",
    "--dataset artifacts/research/preseed_datasets/discovery.json",
    "--dataset artifacts/research/preseed_datasets/holdout.json",
    "--dataset artifacts/research/preseed_datasets/prospective.json",
    "--poll-seconds 120",
    "--minimum-observations 200",
    "--prefix-report artifacts/research/preseed_shuffle_prefix_domains.json",
    "--prefix-tuple-report artifacts/research/preseed_shuffle_prefix_tuples.json",
  ].join(" "),
  autorestart: true,
  watch: false,
  max_memory_restart: "1200M",
  kill_timeout: 10000,
};

module.exports = {
  apps: [
    ...minerApps,
    dashboard,
    ...(enableSeedBridges ? bridgeApps : []),
    ...(enableLegacySeedResearch
      ? [
          seedProbeSupervisor,
          seedResearchOrchestrator,
          publicChainEntropyCollector,
          preseedGeneratorSupervisor,
          preseedShuffleSupervisor,
        ]
      : []),
  ],
};
