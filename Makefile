.PHONY: test bundle bundle-macos-arm64 bundle-macos-x86_64 bundle-windows-x64 publish-bundle publish-bundle-dry e2e

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
