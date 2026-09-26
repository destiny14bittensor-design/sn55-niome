module.exports = {
  apps: [
    {
      name: "niome-dashboard",
      cwd: "/home/administrator/workspace/subnet-niome",
      script: "tools/niome_dashboard.py",
      interpreter: "/home/administrator/workspace/subnet-niome/.venv/bin/python",
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
        NIOME_FEDERATION_LOCAL_ID: "bitcoin-hype-fleet",
        NIOME_FEDERATION_LOCAL_LABEL: "Bitcoin / Hype Fleet",
        NIOME_FEDERATION_REMOTE_SOURCES: JSON.stringify([
          {
            id: "tao-fleet",
            label: "Tao / Won Fleet",
            url: "http://108.181.196.26:8111/api/fleet/state",
          },
        ]),
        NIOME_FEDERATION_POLL_SECONDS: "2",
        NIOME_FEDERATION_TIMEOUT_SECONDS: "2",
      },
    },
  ],
};
