<div align="center">

# AutoBiz OS

**An ontology-driven business operating system for one-person companies**

[中文](README.md) | **English** | [日本語](README.ja.md)

Knowledge → Content acquisition → Customers → Presales → Delivery → back to Knowledge — five domains in one connected graph.

</div>

---

## What is this

The complete back office for running a consulting / service business with one person plus AI agents. Not a Notion template — a **database system with a business rule engine**:

- **The ontology is the single source of truth** — customers, pain points, cases, products and payments are all objects defined in one place (`ontology/*.yaml`); views, actions and AI tools are all compiled from it
- **The Action engine is the only write path** — every write is previewed first and logged; **human gates are enforced by the engine** — AI cannot confirm a deal or close a project by itself; approval codes must be typed by you in a terminal
- **Models write, code searches and computes** — AI-drafted proposals may only cite confirmed knowledge supplied by the system, and every amount is computed by code — **fabrication is prevented by mechanism, not by prompt**
- **Quote grounding** — knowledge extracted by AI must carry a verbatim quote from the source; anything fictional is discarded deterministically

```
$ autobiz q overview
$ autobiz req "Customer name" "Customers buy once and never come back" wechat
$ autobiz act analyze_requirement '{"target":"org_xxx"}'
$ autobiz approve K7Q2PX        ← human gate: approved by you, in your terminal
```

## Get started in 15 minutes

```bash
git clone https://github.com/laotie567/autobiz-os.git && cd autobiz-os
bash scripts/install.sh         # Docker is all you need
```

The installer imports a fictional "neighborhood bakery" sample industry map. Walk through the
[Quick start](docs/QUICKSTART.md): browse the map → drop a requirement → AI analysis → draft a proposal → close the deal → get paid.

> 📖 The docs under `docs/` are currently written in Chinese (中文). Translations welcome — PRs appreciated.

| Want to | Go to |
|---|---|
| Install (three paths) | [docs/INSTALL.md](docs/INSTALL.md) |
| Full walkthrough in 15 min | [docs/QUICKSTART.md](docs/QUICKSTART.md) |
| Adapt it to your industry | [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md) |
| Connect Doubao / Claude Code / Cursor | [docs/AGENTS-SETUP.md](docs/AGENTS-SETUP.md) |
| Upgrade / troubleshoot / back up | [docs/UPGRADE.md](docs/UPGRADE.md) · [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) · [docs/backup.md](docs/backup.md) |
| Design decisions (ADRs) | [docs/adr/](docs/adr/) |

## Design principles (why it looks like this)

- **Human gates enforced by the engine** — the AI assistant runs with minimal privilege by default; preview discipline, approval codes, protected lifecycle fields — trust, but verify
- **Kernel / workspace separation** — everything personal (profile, industry fields, prompt tweaks) lives in the workspace, so kernel upgrades never conflict
- **Extensible ontology** — add your industry fields with an `extend: true` overlay, without touching a line of kernel code
- Privacy & data flows: [PRIVACY.md](PRIVACY.md) · Security model: [SECURITY.md](SECURITY.md)

## Community & support

- **Bugs & feature requests** — open an issue (include the output of `autobiz doctor` — it doubles the fix rate)
- **Industry pack co-creation** — PR your industry overlay into `ontology/packs/`, credited in the README
- **Guided setup / early access to industry packs / regular teardowns** — knowledge-planet access coming soon <!-- TODO: replace with your link -->
- **Deep coaching / custom development** — contact channel coming soon <!-- TODO: replace with your link -->

## License

[Apache-2.0](LICENSE) © 2026 the AutoBiz OS author. **"AutoBiz OS" and its Chinese name are trademarks reserved by the project author** —
use your own product name for redistribution (see [NOTICE](NOTICE)). Review AI-generated content (proposals etc.) before publishing it externally.
