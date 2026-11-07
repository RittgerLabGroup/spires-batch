# Routine NRT operations

Use the same unified JSON that registered the historical stream. After checking
the requested date and current annual assets, submit a fresh cycle plan:

```bash
spires-batch workflow cycle workflow.json --job continue_nrt --through 2026-05-19 --output cycle-20260519-plan.json
spires-batch workflow status cycle-20260519-plan.json
```

This submits only the NRT update, not another historical run. The date is
illustrative; verify source availability. Each update needs a new plan destination.
Neither command installs cron. Deploy recurring submissions only after validating
the environment and preventing overlapping updates to the same stream.

For first-time setup, follow the [complete workflow](../examples/workflow.md).
For standalone execution, see the [cycle walkthrough](../examples/cycle.md).
Missing or incompatible history needs [diagnosis](troubleshooting.md), not
automatic re-registration of the entire historical campaign.

## Routine daily checklist

1. Check the last published date and generation; do not start overlapping cycles.
2. Confirm credentials, expected new dates, archive/scratch capacity, annual
   assets and unchanged scientific context. Use the approved environment pins.
3. Submit the configured cycle allocation explicitly; archive its job ID, config,
   acquisition receipt, log and revision plan.
4. Inspect scheduler exit and scientific receipt. Resolve the domain's
   `current.json` once, then inspect every tile/date product from that generation.
5. Check expected first/final cutoff, explicit missing days, QA and source-code
   distributions, observation ages, and plausible snow/property/albedo maps.
6. Apply only reviewed [retention plans](reference/storage.md). Do not delete the current checkpoint,
   unresolved inputs, leased workspaces or annual assets needed for revisions.

Standard replacements can revise prior dates and final products. On October 1,
add the next annual assets and retrieval bindings for every tile; retain the
previous year for late replacements. Do not reset the stream or register history
again solely because a WY changed.
