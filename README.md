# Genesis

Evidence-led AML investigation with identity filtering, deterministic risk scores,
Gemini analysis, immutable analyst reviews and operator-controlled A0 cutoff learning.
All supplied customer and transaction data is synthetic. Model suggestions and
reference labels remain separate from genuine human decisions.

## Contents

- `frontend/`: React interface and shared CSS.
- `backend/`: FastAPI, Firestore persistence, sequential worker and evaluation logic.
- `config/`: non-secret deployment and model configuration examples.
- `Dockerfile`: separate `web` and `worker` image targets.
- `release-manifest.json`: exact file hashes and source fingerprint.

## Local setup

Install Node 22+, Python 3.12, uv and Java 21+ for Firebase emulators.
Run commands from this folder. Python reads process environment variables;
`.env.example` is a template rather than an automatically loaded configuration.

```sh
npm ci --ignore-scripts
uv sync --frozen --python 3.12
npm run emulators
```

In another terminal, set the variables in `.env.example`, then initialize the
synthetic source and active 3,000-case baseline in the local emulators:

```sh
npm run seed
npm run bulk:generate
npm run bulk:import
uv run python -m backend.seeded_history prepare
uv run python -m backend.seeded_baseline prepare
uv run python scripts/prepare_baseline_workspace.py --dry-run
uv run python scripts/prepare_baseline_workspace.py --apply
npm run api
```

In a third terminal run `npm run dev` and open http://127.0.0.1:5173.
The local-only operator is `operator@aml-insight.test` with password
`local-demo-only`; it exists only in the Auth emulator. Cloud users authenticate
through their own configured Firebase project and server-side UID allowlists.

The baseline has 2,000 reference/advisory cases and 1,000 reserved cases.
Source generation retains the frozen underlying corpus. Fresh setup creates no
Gemini advisories, imported drafts, human verdicts or successful learning claims.
Existing private evidence remains in its configured Firestore database and is
deliberately excluded from source control. Do not reseed an existing cloud database.

## Web and worker images

Build independently from this folder when authorized:

```sh
docker build --target web -t genesis-web .
docker build --target worker -t genesis-worker .
```

The web image serves the compiled UI and API. The worker accepts authenticated
Cloud Tasks delivery as a separate service. Supply configuration and secrets at
runtime; keep model execution and task dispatch disabled until their configuration
and spending coverage are verified. The stored-login shortcut defaults off and
must remain off for a public deployment. No browser or API background model jobs
replace the separate worker.

Production requires Firebase Authentication, a Firestore database with the supplied
rules/indexes, service identities and an authenticated task queue/worker connection.
Model access requires its verified model/location, accounting configuration,
unexpired authorization and daily admin controls. Configuration examples are
templates, not a record that those resources have been deployed or connected.

## Data and learning boundaries

Python owns risk scores; A0 produces heuristic pre-labels, not probabilities.
A5 summarizes frozen genuine reviews. Cutoff preparation, prospective validation,
approval, application and rollback remain separate actions. Insufficient evidence
is a valid outcome. Synthetic references and generated advisories do not fill
human-review gates. Public reads expose only permitted source data and explicitly
published aggregates.
