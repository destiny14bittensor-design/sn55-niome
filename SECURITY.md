# Security and repository hygiene

This is a public repository. Do not commit:

- Bittensor coldkeys, hotkeys, mnemonics, or wallet directories;
- AWS credentials, API tokens, cookies, or `.env` files;
- `artifacts/` or any `request_envelope.json` file;
- presigned S3 URLs or their query signatures;
- PM2 logs, process dumps, host credentials, or SSH keys.

Before pushing operational changes, inspect the staged file list and run a
secret scanner. If a credential is committed, revoke it first and then remove
it from the complete Git history; deleting it in a later commit is not enough.

The dashboard is read-only and must not gain restart, upload, delete, wallet,
or arbitrary file-reading endpoints.
