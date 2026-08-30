# Heart Makefile
#
# Targets:
#   make build        — compile Go binary (requires Go 1.22+)
#   make run          — run Go binary (once, then exit)
#   make loop         — run Go binary in continuous mode
#   make py-run       — run Python bridge (one cycle)
#   make py-loop      — run Python bridge (continuous)
#   make ctl          — run heartctl (requires BRAIN_PATH)
#   make test-ctl     — run heartctl doctor checks
#   make vet          — go vet on Go code
#   make clean        — remove build artifacts
#
# Variables:
#   BRAIN_PATH   — default /brain (override with env)
#   GH_TOKEN     — GitHub PAT (optional)
#   LOG_LEVEL    — debug|info|warn|error (default: info)

.PHONY: build run loop py-run py-loop ctl test-ctl vet clean help

GO      := $(shell command -v go 2>/dev/null || echo "not-found")
PY      := $(shell command -v python 2>/dev/null || command -v python3 2>/dev/null || echo "not-found")
BP      ?= /brain
LL      ?= info
TOOLS   := ./tools

help:
	@echo "Heart Makefile"
	@echo "  make build       — build Go binary"
	@echo "  make run         — run Go (one cycle)"
	@echo "  make loop        — run Go (continuous)"
	@echo "  make py-run      — run Python bridge (one cycle)"
	@echo "  make py-loop     — run Python bridge (continuous)"
	@echo "  make ctl CMD=... — run heartctl subcommand"
	@echo "  make test-ctl    — run heartctl doctor"
	@echo "  make vet         — go vet"
	@echo "  make clean       — remove build artifacts"
	@echo ""
	@echo "Variables:"
	@echo "  BRAIN_PATH=$(BP)"
	@echo "  LOG_LEVEL=$(LL)"
	@echo "  GH_TOKEN=<set>"

build:
ifneq ($(GO),not-found)
	cd cmd/heart && go build -o ../../heart-bin .
	@echo "Built: heart-bin"
else
	@echo "Go not found — skip build"
endif

run: build
ifneq ($(GO),not-found)
	BRAIN_PATH=$(BP) GH_TOKEN=$(GH_TOKEN) HEART_LOG_LEVEL=$(LL) ./heart-bin
endif

loop: build
ifneq ($(GO),not-found)
	BRAIN_PATH=$(BP) GH_TOKEN=$(GH_TOKEN) HEART_LOG_LEVEL=$(LL) ./heart-bin --continuous --cycle-ms 60000
endif

py-run:
ifneq ($(PY),not-found)
	$(PY) $(TOOLS)/heart.py --once --brain-path $(BP) --log-level $(LL)
else
	@echo "Python not found"
endif

py-loop:
ifneq ($(PY),not-found)
	$(PY) $(TOOLS)/heart.py --continuous --brain-path $(BP) --log-level $(LL)
else
	@echo "Python not found"
endif

ctl:
	@if [ -z "$(CMD)" ]; then \
		$(PY) $(TOOLS)/heartctl.py --help; \
	else \
		$(PY) $(TOOLS)/heartctl.py --brain-path $(BP) $(CMD) $(ARGS); \
	fi

test-ctl:
	$(PY) $(TOOLS)/heartctl.py --brain-path $(BP) doctor

vet:
ifneq ($(GO),not-found)
	cd cmd/heart && go vet ./...
else
	@echo "Go not found — skip vet"
endif

clean:
	rm -f heart-bin
	rm -f cmd/heart/heart-bin
	rm -f heart-test.exe
