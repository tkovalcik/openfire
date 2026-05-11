# Deploying the OpenFire SoCal UI — quick guide for the team

The UI is already live and **publicly accessible** at:

**https://openfire-ui-socal-deckgl-ivdriimizq-uc.a.run.app**

Send this URL to anyone — no login required. That's the user-facing piece. The rest of this doc is only for teammates who need to *deploy a new version*.

---

## Who can deploy

Anyone with the three things below can ship a new revision to Cloud Run in ~5 minutes. GCP auth is handled inside the workflow (Workload Identity Federation), so **you do not need a `gcloud` login or a Google Cloud account to deploy** — only to inspect things in the GCP console afterward.

## The three things a teammate needs

### 1. Write access to `tkovalcik/openfire` on GitHub

Ask Tomasko to add your GitHub username as a collaborator with `Write` (or higher) permission on the repo. Confirm with:

```bash
gh repo view tkovalcik/openfire --json viewerPermission
# Expect: {"viewerPermission":"WRITE"} (or MAINTAIN / ADMIN)
```

### 2. A GitHub CLI session with the `workflow` scope

Install [`gh`](https://cli.github.com/) if you don't have it, then:

```bash
gh auth login                    # web flow, pick your GitHub account
gh auth refresh -s workflow      # add the workflow scope (required for workflow_dispatch)
gh auth status                   # confirm "Token scopes" includes 'workflow'
```

### 3. The one-line deploy command

From any branch you've pushed to `origin`:

```bash
gh workflow run ui_socal_deckgl.yml \
  -R tkovalcik/openfire \
  --ref <your-branch-name>
```

Then watch it:

```bash
sleep 3
gh run list --workflow=ui_socal_deckgl.yml -R tkovalcik/openfire --limit 1
gh run watch -R tkovalcik/openfire \
  $(gh run list --workflow=ui_socal_deckgl.yml -R tkovalcik/openfire --limit 1 --json databaseId -q '.[0].databaseId')
```

The workflow takes ~5 minutes. When it finishes, the deployed Cloud Run revision is at the same public URL above (`openfire-ui-socal-deckgl-ivdriimizq-uc.a.run.app`).

---

## What this deploys

- Docker image built from `docker/Dockerfile.ui_socal_deckgl`
- Pushed to Artifact Registry: `us-central1-docker.pkg.dev/msds603-mlops-project/openfire/ui-socal-deckgl:<commit-sha>`
- Deployed as Cloud Run service `openfire-ui-socal-deckgl` (project `msds603-mlops-project`, region `us-central1`)

Full deploy spec lives in [`.github/workflows/ui_socal_deckgl.yml`](../.github/workflows/ui_socal_deckgl.yml).

## Smoke-test after deploy

```bash
URL=https://openfire-ui-socal-deckgl-ivdriimizq-uc.a.run.app

curl -fsS "$URL/" -o /dev/null -w 'index.html: %{http_code}\n'
curl -fsS "$URL/runtime-config.js" | head -3       # confirms the deployed git SHA
curl -fsS "$URL/data/manifest.json" | head -5      # confirms GCS proxy
```

Then open `$URL` in a browser and confirm the heatmap renders.

## If you also want to inspect things in GCP

Optional — only needed for looking at logs, BigQuery rows, etc. (not for deploying).

```bash
gcloud auth login                                 # use the Google account that has access to the project
gcloud config set project msds603-mlops-project
gcloud run services describe openfire-ui-socal-deckgl --region us-central1
```

If your `gcloud` keeps auto-routing to the wrong Google account (common when you're signed into a school SSO), the easy fix is: go to `accounts.google.com` in your browser, click your avatar → "Add another account" → sign in with the account that has GCP access. Don't log out the other one. Then re-run `gcloud auth login` and pick the right account from the picker.
