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
      },
    },
  ],
};
