# zombiescan — Claude Code plugin

Find the Azure resources nobody is using, from inside Claude Code.

## Install

```
/plugin marketplace add xbill9/zombiescan-azure
/plugin install zombiescan@zombiescan
```

## What you get

- **`/zombiescan [subscription ...]`** — scan the subscriptions these
  credentials can reach and report what the waste costs.
- **`/zombie-cleanup <report path>`** — show exactly what cleaning that report
  up would do, without changing anything.
- **A skill** that loads itself whenever the conversation turns to Azure spend,
  unused resources, or a finding you want explained.
- **An MCP server** with five read-only tools: `list_checks`,
  `scan_subscription`, `estimate_savings`, `explain_finding` and
  `plan_cleanup`.

## Requirements

The Azure CLI, signed in — run `az login` once — and
[uv](https://docs.astral.sh/uv/), which the MCP server is started with.
zombiescan takes its token from `az` and talks to Azure Resource Manager
directly, so there is no Azure SDK to install and no service principal to
create.

The server runs out of this repository: `.mcp.json` starts it with
`uv run --project ${CLAUDE_PLUGIN_ROOT}/.. zombiescan-mcp`, which resolves to
the checkout the plugin was installed from. With zombiescan installed globally
(`uv tool install git+https://github.com/xbill9/zombiescan-azure`),
`zombiescan-mcp` on its own works just as well; edit `.mcp.json` to use it.

## Read-only

Every tool here makes read calls only. Nothing in this plugin deletes an Azure
resource, and nothing in it runs a remediation command. `plan_cleanup` plans a
cleanup and stops. Applying one is `zombiescan clean --apply`, run by a person
at a terminal, where it prompts before each resource and warns about steps that
cannot be undone.

Costs are estimates from a bundled list-price table, not from your bill. They
exclude reservations, savings plans, Azure Hybrid Benefit, dev/test rates and
enterprise agreement pricing.
