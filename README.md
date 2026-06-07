# OpenVitals

Open-source, self-hosted connector layer that unifies consumer health data sources behind one standardized `Record` output.

**Status:** private / v0.1 alpha — in active development, API may change.

## What it is

OpenVitals is a Python library that handles the auth + fetch plumbing for consumer health APIs (WHOOP, Strava, Withings, Hevy, Cronometer) and emits a standardized `Record` envelope for each data point. The library is a thin connector layer — it surfaces observations, never interpretations. No cloud, no phone-home, bring your own keys.

## Getting started

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Import check
uv run python -c "import openvitals; print('ok')"
```

## Credential storage

Credentials are encrypted with Fernet (AES-128 CBC). Generate a key and set the env var before use:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export OPENVITALS_ENCRYPTION_KEY=<key>
```

Credentials are stored at `~/.openvitals/credentials.json` (mode 0600) by default. This path is gitignored — never commit it.

## Scope

M0 foundation: schema, base connector, credential store, error taxonomy, library skeleton. Connector implementations (WHOOP, Strava, Withings, Hevy, Cronometer) are built in subsequent milestones.
