# Midas Research Template

This repository is a minimal starting point for research projects that use an AI coding agent without exposing protected data.

The workflow has three parts:

1. **Midas directory**: this repository. It is safe for the AI coding agent to inspect.
2. **GitHub bridge**: reviewed code is pushed here so it can be pulled into the vault.
3. **Vault directory**: the protected environment where real data are stored and analyzed without AI-agent access.

Everything in this repository should be treated as visible to the AI-agent provider. Do not put real data, real outputs, credentials, logs, screenshots, notebooks with real observations, or unsanitized error messages here.

## How to Use

1. Fill in `PROJECT_BRIEF.md` with safe project context.
2. In the vault, run one data-shape report script from `tools/`.
3. Review the generated report manually.
4. Copy only safe structural details into `PROJECT_BRIEF.md`.
5. Start the agent in this directory through Docker Sandboxes.
6. Ask the agent to create or edit code in `scripts/`.
7. Review all changes before pushing them to GitHub.
8. Pull reviewed code into the vault directory and run it there.

The template is intentionally small. Add structure only when your project needs it.

## Optional Vault-Side Data Shape Report

The scripts in `tools/` are meant to be copied to or run inside the vault environment. They create conservative structural reports that can help you describe your data to Midas without sharing observations.

Python:

```bash
python3 tools/make_data_shape_report.py \
  --input /vault/data \
  --output data_shape_report.md
```

R:

```bash
Rscript tools/make_data_shape_report.R \
  --input /vault/data \
  --output data_shape_report.md
```

Before copying anything into `PROJECT_BRIEF.md`, inspect the report yourself. The report includes base file names so you can match sections to your files, but it never prints full paths. Remove or generalize file names if they contain sensitive details. Also remove raw rows, exact values, rare categories, identifying column names, free text, timestamps, file paths, logs, screenshots, and real outputs.
