const path = require("path");

const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv", "bin", "python");
const externalIp = process.env.NIOME_EXTERNAL_IP || "69.30.204.53";

const lanes = [
  {
    id: "dollar1",
    miner: "niome-dollar1",
    bridge: "niome-seed-bridge",
    port: 8091,
    artifactRoot: path.join(root, "artifacts", "live"),
    explorationProfile: null,
  },
  {
    id: "dollar2",
    miner: "niome-dollar2",
    bridge: "niome-seed-bridge-dollar2",
    port: 8092,
    artifactRoot: path.join(root, "artifacts", "miners", "dollar2"),
    explorationProfile: "5Cd42XsDyg9QGovQKCVffbd2nk6cpfQbsCLZo4FF2fhLoveS",
  },
  {
    id: "dollar3",
    miner: "niome-dollar3",
    bridge: "niome-seed-bridge-dollar3",
    port: 8093,
    artifactRoot: path.join(root, "artifacts", "miners", "dollar3"),
    explorationProfile: "5EFDuGe2nXZfb3cRG1K8KTs6ihePcMMUCsSJdcn9fLaaW6mT",
  },
  {
    id: "dollar4",
    miner: "niome-dollar4",
    bridge: "niome-seed-bridge-dollar4",
    port: 8094,
    artifactRoot: path.join(root, "artifacts", "miners", "dollar4"),
    explorationProfile: "5ES1fyTQdvQjtmGokDSsb3fw2MyvzygAQ1oiRKiy9MDJwqKU",
  },
];

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
    "--wallet main",
    `--wallet-hotkey ${lane.id}`,
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
    NIOME_DASH_GUIDE_VARIANTS: "72",
    NIOME_DASH_PRIMARY_CAS_SHARE: "0.60",
  },
};

module.exports = { apps: [...bridgeApps, ...minerApps, dashboard] };
