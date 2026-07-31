# Deploying to a VM

The service is one container that accepts payment and builds APKs, plus the
Redis that makes both durable. This is the runbook for getting it onto a fresh
Linux box and proving it works before any buyer can reach it.

Two things are worth knowing before starting.

**This has been done once, on a 4 vCPU / 8 GB Hetzner box running Ubuntu
26.04.** The image built on the first attempt and the deployment sold a real
build for a real (testnet) payment. Ubuntu 26.04 is newer than the 24.04 below
and needed no special handling: the container carries its own toolchain, and
Docker's own installer supported the release.

**Finished builds are pruned automatically.** A build produces 2.0 GB of Gradle
output for a 144 MB APK; the artifact is kept and the tree dropped the moment the
build finishes, so a completed sale costs 144 MB rather than 2.0 GB. Whole job
directories are swept at seven days, matching the job records' TTL. Set
`BUILD_PRUNE=0` to keep everything while debugging a deployment.

## 1. The machine

| | | Why |
| --- | --- | --- |
| Architecture | **amd64** | The Flutter Linux SDK is x64-only and the Android build-tools are x86_64 ELF. An arm64 box (Graviton, Ampere — the cheap default at most providers) cannot run either. The image refuses to build on one, with a message saying so. |
| vCPU | 4 minimum, 8 comfortable | A build is Gradle plus Dart analysis, both CPU-bound, and one container runs one build at a time. 4 vCPU makes a build take a couple of minutes; 8 keeps a queue from forming. |
| RAM | 8 GB minimum | The Gradle daemon and the Dart analysis server together will not fit comfortably in 4 GB, and an OOM kill mid-build costs a build somebody has paid for. |
| Disk | **100 GB minimum** | Measured: **7.71 GB** of image, plus **144 MB per completed sale** once pruning has run (2.0 GB transiently, while the build is in progress). A 150 GB box holds roughly 790 sold builds; before pruning it was 58. |
| OS | Ubuntu 24.04 LTS or Debian 12 | Anything with a current Docker Engine. The image carries its own toolchain, so the host distribution barely matters. |

Hetzner CPX41 (8 vCPU / 16 GB / 240 GB) is the obvious price/performance pick and
is amd64; DigitalOcean, Vultr and Linode all have equivalents. Check current
pricing rather than trusting a number written here.

Add swap even with 16 GB — Gradle's memory use is spiky, and swap turns a spike
into a slow build rather than a killed one:

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Firewall

Open **22** (ideally only from your own address) and, later, **443**. Do **not**
open 8000.

The compose file publishes the API on `127.0.0.1:8000` for a specific reason:
Docker implements published ports as nat rules that sit *ahead* of ufw, so
`ufw deny 8000` does not close a port Docker has published to `0.0.0.0`. The
service would be reachable from the internet over plain HTTP — carrying signed
payment authorizations and the buyer's PRD in the clear — while the firewall
reported it closed. Binding to loopback means the only ways in are an SSH tunnel
and a reverse proxy on the same host.

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
docker compose version
```

If the provider gives a small root disk and a separate large volume, move
Docker's data root onto the large one *before* the first build — `/var/lib/docker`
is where the image, the Gradle cache and every build volume live.

## 4. Getting the code there

The repository is private on GitHub, so cloning on the box needs a credential
there. Copying the working tree avoids that entirely and is what the live
deployment used:

```bash
# from the development machine
git archive --format=tar.gz -o /tmp/appgen.tar.gz HEAD
scp /tmp/appgen.tar.gz user@host:~
ssh user@host 'mkdir -p appgen && tar -xzf appgen.tar.gz -C appgen'
```

`git archive` ships only committed files, which is the point: `.env` and
`.x402-testnet.json` are gitignored, hold the Anthropic key and a funded private
key respectively, and must not reach the box this way. `.dockerignore` keeps them
out of the image as well.

## 5. Configure

```bash
cp .env.deploy.example .env.deploy
```

Two blanks to fill:

- `X402_PAY_TO` — the address that receives payment. There is deliberately no
  default: a wrong value sends every buyer's money somewhere you do not control
  and looks exactly like working. An empty one makes the service refuse payment,
  which is the safe failure.
- `ANTHROPIC_API_KEY` — the service defaults to the Claude generator, so without
  this it takes payment and then fails to build.

For a first deployment, set `SUPERVISOR_GENERATOR=template`. It is the
deterministic generator: no API key, no spend, and it still exercises the whole
pipeline through to an APK. Switch to `claude` once the box has proven itself.

## 6. Build and start

```bash
docker compose --env-file .env.deploy up -d --build
```

Expect 15–25 minutes: 1.5 GB of Flutter, 143 MB of Android command-line tools,
then the SDK packages. Watch it with `docker compose logs -f api`.

## 7. Prove it before exposing it

From the development machine, tunnel to the loopback-bound port:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@host
```

```bash
poetry run python scripts/verify_deployment.py http://127.0.0.1:8000
```

This is the check worth running. A container can come up, answer HTTP and still
be unsellable — taking payments it never settles, losing paid builds on restart,
or falling back to the dev shared secret because a token variable was
misspelled. The script reads `/healthz`, refuses the deployment when it says any
of those, and names the fix. It was verified against five deliberately broken
deployments and caught all five.

Then buy a real build, which is the only check that proves the deployment can do
the thing it charges for:

```bash
poetry run python scripts/verify_deployment.py http://127.0.0.1:8000 --pay
```

It signs with the funded testnet payer, waits for the APK and downloads it.
Testnet USDC, so a mistake costs nothing real.

### The same check on mainnet

On a mainnet network `--pay` moves real USDC and nothing reverses it, so it
refuses twice by default: once until `--yes-spend-real-money` is passed, and
again if the key file is still the throwaway testnet one.

Make the payer file yourself — it holds a private key, it is gitignored by the
`.x402-*.json` glob, and it should never be copied to the server or pasted into
a chat:

```bash
poetry run python scripts/verify_deployment.py https://37-27-249-33.sslip.io --network base --pay --yes-spend-real-money --payer-keys .x402-mainnet.json
```

```json
{"payer": {"private_key": "0x…", "address": "0x…"}}
```

**Pay from the receiving wallet and the purchase costs nothing.** The verifier
checks only that the authorization is addressed to `X402_PAY_TO`; it places no
constraint on who sends it. With payer and recipient the same address the USDC
returns to where it started and the facilitator pays the gas, while the run still
exercises the whole path a stranger would take — EIP-712 signature, facilitator
submission, on-chain settlement, build, APK. It needs the price in USDC present
in the wallet to authorize against, and a few cents of ETH is not required
because the facilitator submits the transaction.

## 8. Only then, TLS and a public address

Caddy is in the compose stack for this. It terminates TLS on 443 and reaches
`api:8000` over the compose network, so the API keeps its loopback binding on the
host. Set `PUBLIC_HOSTNAME` in `.env.deploy` to a name that **already resolves to
the machine** — Let's Encrypt proves control by connecting back, so a name
pointing elsewhere fails the challenge and spends one of five weekly attempts.

Without a domain, `sslip.io` resolves a dashed IP in the hostname straight back
to that IP (`37-27-249-33.sslip.io`), and Let's Encrypt will issue for it. That
is what the live deployment uses.

Port 80 stays closed, so the config disables the HTTP-01 challenge and Caddy uses
TLS-ALPN-01 on 443 instead of spending an attempt discovering the same thing.

**Validate the Caddyfile before restarting anything with it.** A Caddyfile error
is not a degraded proxy: the process exits, the container restart-loops, and from
outside the box it is indistinguishable from a closed firewall port. That cost
real time here — `email {$ACME_EMAIL:}` looked like "optional with no default"
and is actually a parse error when the variable is unset, so an optional setting
broke the config by being absent.

```bash
docker run --rm -v /opt/appgen/deploy/Caddyfile:/etc/caddy/Caddyfile:ro -e PUBLIC_HOSTNAME=your.host caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

Note that `docker compose up -d` will not restart Caddy when only the mounted
Caddyfile changed — nothing in the service definition differs. Use
`docker compose restart caddy`.

## Operating it

- **Disk.** Pruning keeps a completed build at ~144 MB, but a build *in
  progress* still needs its 2.0 GB, and the Gradle cache grows. Watch it with
  `docker system df -v` and `du -sh /var/lib/docker/volumes/*`.
- **`/healthz` is the status source.** `durable_execution` false means a restart
  loses paid builds; `settlement: verification-only` means the money is not
  moving; `payment_mode` other than `x402-eip3009` means the gate is not the
  real one.
- **Redeploying** is `git pull && docker compose --env-file .env.deploy up -d --build`.
  The builds and Redis volumes survive it; that is what they are for.
- **Rerun `verify_deployment.py` after every redeploy.** Every failure it looks
  for is a configuration mistake, and configuration is what a redeploy changes.
