const path = require("path");

const root = process.env.NIOME_REPO_ROOT || path.resolve(__dirname, "..");
const python = path.join(root, ".venv", "bin", "python");

module.exports = {
  apps: [
    {
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
        NIOME_FEDERATION_LOCAL_ID: "tao-won-local",
        NIOME_FEDERATION_LOCAL_LABEL: "Tao / Won Local",
        NIOME_FEDERATION_REMOTE_SOURCES: "[]",
        NIOME_FEDERATION_POLL_SECONDS: "2",
        NIOME_FEDERATION_TIMEOUT_SECONDS: "2",
      },
    },
  ],
};
