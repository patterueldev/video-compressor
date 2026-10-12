# Security notes

Security guidance for running the controller and a worker fleet. This repository is public: all
examples use placeholders, and no token, hostname, or path here is real. Operational procedures
live in [install-runbook.md](install-runbook.md) and [worker-runbook.md](worker-runbook.md).

## Trust model

- The controller (hosted by the app, or `vc controller`) owns the queue and requires
  `Authorization: Bearer <token>` on every API request; the token is the only access control.
- Workers authenticate with that token and reach the archive and scratch through local paths.
- Remote installs use SSH from the operator machine to the node; the node never needs inbound
  access beyond SSH.
- v1 assumes a trusted local network or VPN. The controller serves plain HTTP; do not expose it
  to untrusted networks.

## SSH access (remote bootstrap)

- **Key-based only.** `vc node` uses the system OpenSSH client; there is no password flow. Pass
  the private key with `--identity` or rely on your SSH agent and `~/.ssh/config`.
- Treat the private key as a credential: passphrase-protected, agent-loaded, never committed,
  never copied into the repository or a container image.
- **Host keys.** The default is `accept-new`: the first contact pins the node's key, and a later
  change fails the connection. For pre-provisioned fleets, seed `known_hosts` and pass
  `--strict-host-keys` so an unknown host is rejected outright.
- The remote install stays inside the SSH user's home (`~/.videocompressor/`, `worker.toml` at
  mode 0600) and the service runs as that user — no sudo is required, so do not grant it.
- Scope node SSH access to the accounts that operate the fleet; the node does not need inbound
  ports other than SSH.

## Controller access token

- The token is generated once and stored in the per-user config (`[controller]` section). Read it
  on the controller machine with `vc controller --print-token`.
- It travels out of band only: the worker's environment (`VC_WORKER_TOKEN`), `vc node
  --token-env` / `--token-file`, or the container's untracked `.env`. Never commit it, paste it
  into issues or PRs, or place it in a checked-in `worker.toml`.
- The service installers materialize the resolved token into user-only files (a 0600
  `EnvironmentFile` on Linux, `EnvironmentVariables` in the 0600 plist on macOS); the value is
  never printed. Rotating the token requires re-running `install-service` on every node (and
  updating the container `.env`); a stale token fails with `controller rejected the token`.
- On a manual install, `worker.toml` holds the token or names the environment variable that
  does — keep it out of the repository and readable only by the worker's user.

## Controller bind address and firewall

- The default bind is `127.0.0.1:8765`: reachable only on the controller machine. Keep it while
  every worker is local.
- Remote workers need the controller on an address they can reach: bind the LAN interface
  (`0.0.0.0:8765`, or a specific private address) in the `[controller]` config, or for one run
  with `vc controller --host`.
- With a network bind the bearer token is the only protection and it is sent over plain HTTP:
  - prefer a private network or VPN between the controller and its nodes (for example a mesh VPN
    such as Tailscale), or
  - restrict the port with the host firewall to the worker hosts' addresses.
- Never port-forward or otherwise expose the controller to the internet.
- Check reachability from a node before starting a fleet; the unreachable-controller row in the
  [worker-runbook troubleshooting table](worker-runbook.md#troubleshooting) lists what to look at.

## Scratch and disk space

- Scratch sits on a local disk, never on the archive or a network share; the archive path is
  writable only where replacement/placement needs it (the verified swap and destination
  placement).
- Size per job for the source copy, the temporary output, the pass logs (256 MB allowance), and a
 1 GB margin. A shortage fails the job with `scratch space: …` instead of writing a partial
  output, and the whole `jobs/` tree is swept at worker startup.

## Repository hygiene

- Docs, issues, PRs, and commits use placeholder hostnames (`<controller-host>`,
  `node.example.com`), placeholder paths, and no credentials.
- Historical pilot notes carry operator environment identifiers; sanitizing them is a recorded
  candidate follow-up (see [v1-readiness.md](v1-readiness.md) deferrals), not fixed here.
- Before posting logs or reports anywhere, strip tokens, hostnames, archive paths, and clip
  names.
