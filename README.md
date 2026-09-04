# deepeval-actions

The automation behind Confident AI's one-click tracing setup. When you connect a
repository during onboarding, it opens a pull request that adds
[deepeval](https://github.com/confident-ai/deepeval) tracing to your app — so you can
start seeing traces in Confident AI without instrumenting anything by hand.

## What it does

When you connect your repo, this automation:

1. Accesses it with a **short-lived token scoped to just that one repository**.
2. Detects your framework and model setup and adds deepeval tracing — a native
   integration where one exists, or the `@observe` decorator otherwise.
3. Opens a **pull request** with the change for you to review and merge.

Your code is processed only for the duration of that run and is **never stored**, and
nothing is added to your repository except the pull request.

## Run it on your own repo (self-serve)

Prefer to run it yourself instead of the hosted onboarding flow? This repo also ships a
self-serve workflow, `add-tracing.md`, that runs the same agent against **your** repo and
opens a tracing PR — no GitHub App and no callback, just the built-in `GITHUB_TOKEN`. All
you need is [`gh-aw`](https://github.com/github/gh-aw) and an `ANTHROPIC_API_KEY`; it runs
on GitHub Actions.

```bash
gh extension install github/gh-aw
gh aw add https://github.com/confident-ai/deepeval-actions/blob/main/.github/workflows/add-tracing.md
gh aw secrets set ANTHROPIC_API_KEY --value "sk-ant-..."
gh aw run add-tracing
```

Or try it against your repo in a throwaway sandbox first (nothing installed, no changes made):

```bash
gh aw trial https://github.com/confident-ai/deepeval-actions/blob/main/.github/workflows/add-tracing.md --clone-repo your-org/your-repo --dry-run
```

## PR Eval Gate action

`actions/eval-gate` is the composite action behind Confident's PR Eval Gate: on every
pull request it replays the gates configured for the repository (metric evals over a
pinned dataset and/or a frozen red-team attack suite) through your `confident_eval.py`,
submits the outputs, and Confident scores them and posts the check-runs. Confident's
setup flow (Integrations → PR Eval Gate) writes the workflow that uses it.

### Code scan (optional)

When the risk gate regresses and the action's step `env:` carries a `CONFIDENT_SCAN_API_KEY`
(the setup PR wires it there, so the key is not exposed to your install or app steps), the
action waits for Confident's verdict, scans the PR's diff with
[DeepTeam](https://github.com/confident-ai/deepteam) for the vulnerability types that
regressed, and Confident posts the findings as inline review comments plus a
"Problematic code" section on the risk-gate check. Without the secret the step exits
immediately and the gate behaves exactly as before.

- Fail-open: the scan step always exits 0 and never changes the gate's result.
- DeepTeam is installed into an isolated virtualenv under the runner's temp directory,
  never into your app's environment. Needs Python 3.10–3.13 on the runner.
- Only findings leave the runner; your code does not.

`env:` variables the step reads (set them on the action's step, or at job level):

| Variable | Default | Description |
|---|---|---|
| `CONFIDENT_SCAN_API_KEY` | — | Enables the scan. An OpenAI key by default. Read only from this name, so your app's own `OPENAI_API_KEY` is never spent on scanning. |
| `CONFIDENT_SCAN_PROVIDER` | `deepeval` | `deepeval` (DeepTeam's built-in OpenAI judge), `codex`, `claude-code`, or `cursor`. The key above is passed to whichever provider you pick. |
| `CONFIDENT_SCAN_MODEL` | provider default | Model override for the chosen provider. |
| `CONFIDENT_SCAN_MIN_SEVERITY` | `low` | Report findings at or above this severity. |
| `CONFIDENT_SCAN_MAX_WAIT_MINUTES` | `30` | How long to wait for the verdict before giving up. |
