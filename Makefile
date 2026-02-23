.PHONY: test lint typecheck quality ci deploy deploy-ui

# ── Run all tests ──────────────────────────────────────────────────────────────
test:
	@echo "=== Python tests ==="
	python3 -m pytest services/cw-decoder/test_cw_roundtrip.py services/rtl-bridge/test_rtl_bridge.py services/sstv-decoder/test_sstv_decoder.py -v
	@echo ""
	@echo "=== TypeScript tests ==="
	cd ui && npx vitest run

# ── Lint ───────────────────────────────────────────────────────────────────────
lint:
	@echo "=== Ruff (Python) ==="
	ruff check services/ scripts/
	@echo ""
	@echo "=== Biome (TypeScript) ==="
	cd ui && npx biome check .

# ── Type checks ────────────────────────────────────────────────────────────────
typecheck:
	@echo "=== mypy (Python) ==="
	mypy services/ scripts/
	@echo ""
	@echo "=== tsc (TypeScript) ==="
	cd ui && npx tsc --noEmit

# ── Full quality gate (CI equivalent) ─────────────────────────────────────────
quality: lint typecheck test
	@echo ""
	@echo "✓ All quality checks passed"

ci: quality

# ── Deploy ─────────────────────────────────────────────────────────────────────
deploy:
	./scripts/deploy.sh

deploy-ui:
	./scripts/deploy.sh ui
