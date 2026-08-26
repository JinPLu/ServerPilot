## What changes and why

<!-- The outcome, not the file list. If it fixes an issue, link it. -->

## Verification

<!-- What you ran, and what it showed. Not "tests pass" but which tests. -->

- [ ] `uv run --extra dev pytest`
- [ ] `uv run --extra dev ruff check .`
- [ ] Desktop change: `zsh desktop/build-macos-app.sh` then `zsh desktop/verify-macos-app.sh`
- [ ] Persistence or public behaviour change: migration, docs, and tests updated together

## Boundaries

<!-- Delete the lines that do not apply; keep the ones you had to think about. -->

- [ ] No credentials, private keys, internal hostnames, or built app bundles are included
- [ ] Does not add a second path for an existing behaviour; the superseded one is deleted
- [ ] Does not widen what ServerPilot may do to a remote machine
- [ ] User-visible change is recorded under `Unreleased` in `CHANGELOG.en.md` and `CHANGELOG.md`
