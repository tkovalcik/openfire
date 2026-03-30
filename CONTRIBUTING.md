# Contributing to OpenFire

Thanks for contributing to OpenFire! This document outlines our branching strategy, naming conventions, and pull request workflow to keep the codebase clean and collaborative.

---

## Branching Strategy

We use a **feature branch workflow**. All work happens on branches — never directly on `main`.

### Branch hierarchy

- **`main`** — Production-ready code. Always deployable. Protected — no direct pushes allowed.
- **`dev`** — Integration branch. Feature branches merge here first for testing and validation before being promoted to `main`.
- **Task branches** — Short-lived branches for individual tasks. Branch off `dev`, merge back into `dev` via pull request.

### Branch flow

```
feature/your-task  →  PR  →  dev  →  PR  →  main  →  deployment
```

---

## Branch Naming Conventions

Use the following prefixes to categorize your work:

| Prefix      | Use for                                         | Example                          |
|-------------|--------------------------------------------------|----------------------------------|
| `feature/`  | New functionality or capabilities                | `feature/leaflet-map`            |
| `fix/`      | Bug fixes                                        | `fix/cloud-run-timeout`          |
| `data/`     | Data pipeline, EDA, feature engineering          | `data/eda`                       |
| `infra/`    | MLOps infrastructure (Docker, CI/CD, Cloud Run)  | `infra/github-actions-ci`        |
| `model/`    | Model training, tuning, evaluation               | `model/xgboost-baseline`         |
| `docs/`     | Documentation only                               | `docs/update-readme`             |

### Naming tips

- Keep names short but descriptive
- Use lowercase and hyphens (no spaces or underscores)
- The prefix is literally part of the branch name — you just type it when creating the branch:

```bash
git checkout -b data/eda
git checkout -b feature/risk-dashboard
git checkout -b infra/mlflow-setup
```

---

## Workflow: Start to Finish

### 1. Create your branch

Always branch off `dev`:

```bash
git checkout dev
git pull origin dev
git checkout -b feature/your-task
```

### 2. Do your work

Make commits with clear, concise messages:

```bash
git add .
git commit -m "Add Sentinel-2 median compositing for 30-day windows"
```

Keep commits focused — one logical change per commit when possible.

### 3. Push and open a pull request

```bash
git push origin feature/your-task
```

Then open a pull request on GitHub targeting `dev`. In your PR description:

- Briefly describe **what** the change does
- Note **why** (link to an issue or task if applicable)
- Flag anything you want the reviewer to pay special attention to

### 4. Code review

- All PRs to `dev` and `main` require at least **1 approval** from a teammate.
- Reviewers: leave constructive comments. If something needs to change, use GitHub's "Request changes" option.
- Authors: address all comments before merging. Use "Resolve conversation" as you fix things.

### 5. Merge

Once approved and all checks pass:

- Use **"Squash and merge"** for feature branches into `dev` (keeps the history clean)
- Delete the branch after merging (GitHub will prompt you)

### 6. Promoting `dev` to `main`

When `dev` is stable and tested:

- Open a PR from `dev` → `main`
- At least 1 teammate reviews and approves
- Merge triggers deployment to Cloud Run

---

## What Not to Do

- **Don't push directly to `main` or `dev`.** Branch protection rules will block this anyway.
- **Don't let branches go stale.** If a branch is open for more than a week without activity, either finish it, rebase, or close it.
- **Don't merge your own PR without a review** (unless it's a trivial docs fix on `dev`).

---

## Environment and Setup

- **Python version**: 3.10+
- **Package management**: `pip` with `requirements.txt`
- **Cloud**: GCP (Cloud Run, Earth Engine)
- **ML tracking**: MLFlow
- **Monitoring**: Evidently
- **Frontend**: Leaflet.js
- **CI/CD**: GitHub Actions

Detailed setup instructions are in the [README](README.md).

---

## Questions?

If you're unsure where something belongs or how to name a branch, just ask in the team chat. When in doubt, pick the closest prefix and keep moving — we can always rename things later.
