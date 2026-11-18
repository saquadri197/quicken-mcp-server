# Metabase dashboards

Dashboards for personal and HM Syed Enterprise LLC finances, built on the same
Quicken/Simplifi CSV exports the MCP server reads.

```
accountant (Chrome export) ──> \\192.168.3.136\quicken-data ──(read-only)──> metabase VM
                                                                  loader ──> Postgres ──> Metabase :3000
```

## The VM (ESXi)

| Setting | Value |
|---|---|
| Name / IP | `metabase.syedaq.local`, a free address on VLAN 3 (e.g. 192.168.3.138), DHCP reservation + DNS record |
| OS | Debian 12 or 13 (or Ubuntu Server 24.04 LTS) |
| vCPU / RAM / disk | 4 / 8 GB / 40 GB thin |
| Network | VLAN 3 port group, VMXNET3 |
| Autostart | On (Host → Manage → System → Autostart) |

## Install

On the VM, as root:

```bash
SHARE_USER=<samba user> bash setup-vm.sh <url of this repo>
```

It installs Docker, mounts the share **read-only**, writes random database
passwords to `.env`, and starts three containers: `postgres`, `loader`, `metabase`.

Then open `http://metabase.syedaq.local:3000`, create the admin login, and add a
PostgreSQL database: host `postgres`, port `5432`, database `finance`, user
`finance_reader`, password `PG_READER_PASSWORD` from `.env`.

## What's in the database (schema `finance`)

| Object | What it is |
|---|---|
| `transactions` | The newest export, replaced on each load |
| `ledger` | Every transaction with the corrections applied: `kind` (spend / credit / income / ignore), `spend` (positive = money out), `scope` (Personal / Business), `budget_line`, `is_one_off` |
| `budget_vs_actual` | Personal spending by month and budget line against `budget_lines` (one-offs left out) |
| `monthly_summary` | One row per month: personal spending, one-offs, payroll, business spending and income |
| `payee_rules` | Payee overrides (Zelle to people = spending, savings moves ignored, Amex points = credit). Edit freely |
| `one_offs` | Specific transactions to report separately (repairs, the Permit to Carry course). Add rows as they come up |
| `budget_lines` | The $6,450 budget by line. Change amounts here |
| `load_log` | Which export was loaded and when |

The loader re-applies `sql/schema.sql` on every load. It never overwrites rows
you add or change in the rule tables. Metabase connects read-only, so edit the
rule tables with psql:

```bash
cd /opt/quicken-mcp-server/deploy/metabase
docker compose exec postgres psql -U finance_admin -d finance
-- e.g. INSERT INTO finance.one_offs VALUES ('2026-12-02','Hyundai%',450.00,'Brake job');
```
