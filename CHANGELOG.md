# Changelog

All notable changes to the Claude Commander × ZCode Executor Kit are recorded here.

## 0.1.1 — 2026-09-26

Documentation baseline release.

### Added

- `SPEC-RFD.md` documenting the origin, goals, architecture, responsibility split, acceptance model, prior art, Windows constraints, validation criteria, phased roadmap, and design decisions.
- `CHANGELOG.md` to preserve version-level decisions and changes.

### Changed

- Updated `README.md` to point to the SPEC/RFD as the design baseline.
- Updated `manifest.json` version to `0.1.1`.

### Implementation behavior

- No intended runtime behavior change from `0.1.0`.
- The next milestone remains real Windows end-to-end validation.

## 0.1.0 — 2026-09-26

Initial prototype.

### Added

- Windows-aware ZCode bridge launcher.
- Claude Commander policy.
- Installer, diagnostics, and uninstall utilities.
- Worker Contract example.
- `coder-mcp-bridge` reuse as the MCP control plane.
- Initial research notes.
