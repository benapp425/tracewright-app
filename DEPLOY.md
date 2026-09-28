# Running Tracewright on a server

Tracewright can run on a small cloud server instead of your Mac: the app, KiCad's command-line tools
and Claude run there; you use it from any browser. Your own KiCad (for hands-on editing) gets the
files through GitHub or a download.

## What to rent

A small Linux VPS is the right shape: Claude's design sessions run for many minutes, KiCad renders
and the router want a few CPU cores, and projects need a disk that stays. Serverless platforms
(Lambda, Cloud Run) fit none of that.

| Host | Plan | Specs | Price (Sep 2026) | Notes |
|---|---|---|---|---|
| **Hetzner Cloud** (recommended) | CX33 | 4 vCPU, 8 GB RAM | about EUR 8.50 / month | cheapest by far; EU and US sites (plans vary by site; US prices differ) |
| Hetzner Cloud | CX23 | 2 vCPU, 4 GB | about EUR 5.50 / month | enough for small boards; renders are slower |
| AWS Lightsail | 4 GB | 2 vCPU, 4 GB, 80 GB SSD | USD 24 / month | if you want to stay in AWS |
| DigitalOcean | Basic 4 GB | 2 vCPU, 4 GB | USD 24 / month | pleasant, pricier |

Prices from the providers' September 2026 lists (Hetzner raised prices in June 2026); check before
you buy. Claude usage is separate: with your Claude plan's token it counts against your plan's limits,
with an API key it is billed per token.

## Setting it up (about 20 minutes)

1. **A token for Claude**, on your Mac: `claude setup-token`. It opens the browser, you approve, and it
   prints a token that uses your Claude Pro / Max plan. (Or use an Anthropic API key instead.)
2. **Optional: a GitHub token** (github.com > Settings > Developer settings > fine-grained tokens) with
   *Contents* and *Administration* read/write on your repositories, so every project can live in its own
   private repository.
3. **Create the server**: Ubuntu 24.04, the CX33, your SSH key. Firewall: allow only 22, 80, 443.
4. **Choose how you reach it**
   - *Private, simplest*: [Tailscale](https://tailscale.com) on the server and on your Mac. Nothing is
     exposed to the internet; you get an HTTPS address on your tailnet.
   - *Public*: point a domain (e.g. `tracewright.yourdomain.com`) at the server's IP. Caddy fetches a
     Let's Encrypt certificate by itself.
5. **On the server**:
   ```sh
   curl -fsSL https://get.docker.com | sh
   git clone <your copy of Tracewright> tracewright && cd tracewright/deploy
   cp .env.example .env && chmod 600 .env && nano .env      # password, Claude token, GitHub token, domain
   docker compose up -d --build                              # public, with Caddy
   # or, with Tailscale:  docker compose up -d --build tracewright && sudo tailscale serve --bg 8764
   ```
   The first build downloads KiCad's image (a few GB, with the 3D models).
6. Open your address and sign in with the password from `.env`.

Updates: `git pull && docker compose up -d --build`. Projects open after an update get the new toolkit
(after a checkpoint), as on the Mac.

## Working with it

- **New boards** start from a written brief as usual. **Existing designs**: upload a `.zip` of the KiCad
  project, or paste its GitHub URL (the project keeps its history and its link to the repository).
- **Opening a board in your own KiCad**: connect the project to GitHub (History tab), clone the
  repository on your Mac, and open it. When you have edited and pushed, press **Pull** in the History
  tab: Tracewright takes your changes and the board and schematic views update. Or use *Download the
  project (.zip)* from the Open menu.
- **Data sheets and references**: *Upload* in the Files tab.
- The **live KiCad link** (seeing Claude's placement in your KiCad window as it happens) needs KiCad
  and Tracewright on the same machine, so it is only available when Tracewright runs on your Mac. On a
  server you watch the work in Tracewright's own board and schematic views.

## Security

- A password is required whenever Tracewright is reached from beyond the machine it runs on; it
  refuses to start otherwise. Sessions are signed, HttpOnly, SameSite=Strict cookies (Secure over
  HTTPS); five wrong passwords lock an address out for ten minutes.
- Tokens live only in `deploy/.env` on the server (keep it `chmod 600`, never commit it) and in the
  app's settings file (owner-only). They are never sent to the browser.
- Claude runs its commands inside the container, with the same rules as on the Mac: it may edit the
  project's files and run the toolkit, KiCad and read-only commands; anything else is asked about.
- Back up the `tracewright-data` volume (your host's snapshots, or `docker run --rm -v
  tracewright-data:/d -v $PWD:/b alpine tar czf /b/tracewright.tgz /d`), and push projects to GitHub.

## Not yet verified

The container recipe has not been built on a server yet (this Mac has no Docker). What was tested
here: the server mode itself (sign-in, uploads, downloads, GitHub sync against a stand-in repository,
refusal to start without a password), on macOS. Expect to adjust the Dockerfile on the first build.
