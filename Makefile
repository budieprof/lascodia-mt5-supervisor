.PHONY: installer publish-installer publish-installer-dry test bundle bundle-macos-arm64 bundle-macos-x86_64 bundle-windows-x64 publish-bundle publish-bundle-dry e2e

PYTHON ?= python3

# Unit tests (fakes / temp dirs; the e2e test runs too when a bundle for this Mac is built).
test:
	$(PYTHON) -m pytest -q tests

# Relocatable bundles for every platform -> dist/bundles/ (C9).
bundle:
	tools/build_bundle.sh

bundle-macos-arm64 bundle-macos-x86_64 bundle-windows-x64:
	tools/build_bundle.sh $(@:bundle-%=%)

# One-line installer end to end: install.sh + the built bundle + stub engine + fake launchctl.
e2e:
	$(PYTHON) -m pytest -q tests/test_bootstrap_e2e.py

# Upload the bundles to the engine (LASCODIA_ADMIN_TOKEN, optional LASCODIA_API_BASE).
publish-bundle:
	tools/publish_bundle.sh

publish-bundle-dry:
	DRY_RUN=1 tools/publish_bundle.sh

# Double-click macOS installer (.pkg) -> dist/installers/ (API_BASE = the engine's public API base).
installer:
	tools/build_pkg.sh --api-base "$(API_BASE)"

# Build it from the installer script the engine serves and upload it (LASCODIA_ADMIN_TOKEN).
publish-installer:
	tools/publish_pkg.sh

publish-installer-dry:
	DRY_RUN=1 tools/publish_pkg.sh
