# Hosting the chatbot on Oracle Cloud (free)

The chatbot runs in Docker on one always-on VM, behind [Caddy](https://caddyserver.com), which
gets and renews the HTTPS certificate. A shared password (`CHATBOT_PASSWORD`) keeps strangers
from spending the Anthropic key: the browser asks for it once (any username works).

It suits one VM rather than serverless hosting. Conversations live in memory, the registers and
the places index take a couple of minutes to build at start, and the 3D tiles and registers are
cached on disk (a Docker volume).

Files: [`compose.yaml`](../compose.yaml) (the app and Caddy),
[`chatbot.Dockerfile`](chatbot.Dockerfile), [`Caddyfile`](Caddyfile) and
[`setup-server.sh`](setup-server.sh) (one-shot VM setup).

## 1. Create the VM

Oracle's Always Free tier includes Ampere (ARM) VMs with up to 2 CPUs and 12 GB of memory in
total, free with no time limit.

1. Sign up at https://www.oracle.com/cloud/free/. The **home region** can't be changed later and
   free VMs only run there; pick one in the EU (for example Frankfurt, Milan, Marseille or
   Amsterdam). Signing up asks for a card to verify you; Always Free use isn't charged.
2. Recommended: upgrade the account to **Pay As You Go** (Billing → Upgrade). Always Free
   resources stay free, but Oracle no longer reclaims VMs it considers idle, which a quiet
   chatbot would be. Add a budget alert (Billing → Budgets) at, say, $1 to catch mistakes.
3. **Compute → Instances → Create instance**:
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: **Ampere → VM.Standard.A1.Flex**, 2 OCPUs and 12 GB (1 OCPU and 6 GB is plenty too).
     If it says "out of capacity", try another availability domain or try again later.
   - Networking: create a new VCN with a public subnet, and assign a public IPv4 address.
   - SSH keys: paste your public key (`~/.ssh/id_ed25519.pub`).
4. Open the web ports: **Networking → Virtual cloud networks →** your VCN **→** its public subnet
   **→ Default security list → Add ingress rules**: source `0.0.0.0/0`, TCP, destination ports
   `80,443`. (Optionally UDP `443` for HTTP/3.)

## 2. Set up the VM

```bash
ssh ubuntu@<public-ip> 'bash -s' < deploy/setup-server.sh
```

This opens ports 80 and 443 in the VM's firewall, installs Docker, clones this repo into
`~/smart-nicosia` and writes a `.env` there for you to fill in. Then:

```bash
ssh ubuntu@<public-ip>
nano ~/smart-nicosia/.env
```

Set `ANTHROPIC_API_KEY`, `CHATBOT_PASSWORD` and `DOMAIN`. `DOMAIN` starts as
`<public-ip>.sslip.io`, which works with no DNS setup. To use your own name instead, add a DNS
`A` record pointing at the VM (in Cloudflare, set it to "DNS only", the grey cloud) and put
that name in `DOMAIN`. Then run the script again, which builds and starts everything:

```bash
bash ~/smart-nicosia/deploy/setup-server.sh
```

The site is up at `https://<DOMAIN>` once Caddy has its certificate, usually within a minute.
The map works right away; the registers and the 3D links take about 2 minutes to build.

## Day to day

On the VM, in `~/smart-nicosia`:

| | |
|---|---|
| Update to the latest `main` | `git pull && sudo docker compose up -d --build` |
| Follow the logs | `sudo docker compose logs -f chatbot` |
| Restart | `sudo docker compose restart chatbot` |
| Change the password or key | edit `.env`, then `sudo docker compose up -d` |
| Stop | `sudo docker compose down` (the caches and certificate are kept) |
