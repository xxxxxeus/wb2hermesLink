# Changelog

## v0.1.0

- Draft preparation, not a published release. [Source](https://github.com/xxxxxeus/wb2hermesLink) / [Releases](https://github.com/xxxxxeus/wb2hermesLink/releases).
- macOS CLI `w2hlink` for native WorkBuddy named-provider installation and removal.
- Profile-local ownership receipt, drift refusal and interrupted transaction recovery.
- Literal model ID and context management; new installs contain only `hy3` (262144).
- Explicit login with private saved authentication reuse, offline config check and one-request model check.
- Reproducible allowlisted ZIP, no bundled Python, Hermes, client, proxy or account material.
- Tested locally with Hermes v0.21.3 (2026.9.14), macOS 26.3.1 arm64.
- Native Windows is unsupported. Intel and other macOS versions are unverified.
- Independent acceptance: all 33 offline tests passed, including the extracted-ZIP workflow. One real `hy3` check from the ZIP entrypoint reused saved authentication and returned nonempty final text with normal completion; no fresh browser login was performed. Automated tests use synthetic accounts only.
