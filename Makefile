.PHONY: test lint typecheck quality ci

# ── Run all tests ──────────────────────────────────────────────────────────────
test:
	@echo "=== Python tests ==="
	python3 -m pytest services/cw-decoder/test_cw_roundtrip.py tests/test_rtl_bridge.py -v
	@echo ""
	@echo "=== TypeScript tests ==="
	cd ui && npx vitest run

# ── Lint ───────────────────────────────────────────────────────────────────────
lint:
	@echo "=== Ruff (Python) ==="
	ruff check services/ tests/
	@echo ""
	@echo "=== Biome (TypeScript) ==="
	cd ui && npx biome check .

# ── Type checks ────────────────────────────────────────────────────────────────
typecheck:
	@echo "=== mypy (Python) ==="
	mypy services/ tests/
	@echo ""
	@echo "=== tsc (TypeScript) ==="
	cd ui && npx tsc --noEmit

# ── Full quality gate (CI equivalent) ─────────────────────────────────────────
quality: lint typecheck test
	@echo ""
	@echo "✓ All quality checks passed"

ci: quality
