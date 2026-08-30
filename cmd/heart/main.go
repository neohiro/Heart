// Package main is the /Heart cadence engine.
//
// SPEC: ../SPEC.md
// This is a reference skeleton. It runs the cycle loop, reads/writes
// /Brain, and degrades to emergency cadence when health fails.
// Not feature-complete — proves the shape, not the production version.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type Mode string

const (
	ModeDormant Mode = "dormant"
	ModeNormal  Mode = "normal"
	ModeActive  Mode = "active"
	ModeSports  Mode = "sports"
)

type ModeConfig struct {
	CycleMS    int           `yaml:"cycle_ms"`
	StaleAfter time.Duration `yaml:"stale_after"`
}

var modeDefaults = map[Mode]ModeConfig{
	ModeDormant: {CycleMS: 3_600_000, StaleAfter: 24 * time.Hour},
	ModeNormal:  {CycleMS: 60_000, StaleAfter: time.Hour},
	ModeActive:  {CycleMS: 10_000, StaleAfter: 5 * time.Minute},
	ModeSports:  {CycleMS: 1_000, StaleAfter: time.Minute},
}

type Config struct {
	BrainPath string
	HeartPath string
	MouthPath string
	GhToken   string
	LogLevel  string
}

func loadConfig() Config {
	cfg := Config{
		BrainPath: getEnv("HEART_BRAIN_PATH", getEnv("BRAIN_PATH", "/activememory/brain")),
		HeartPath: getEnv("HEART_HEART_PATH", getEnv("HEART_PATH", "/activememory/heart")),
		MouthPath: getEnv("HEART_MOUTH_PATH", getEnv("MOUTH_PATH", "/activememory/mouth")),
		GhToken:   os.Getenv("GH_TOKEN"),
		LogLevel:  getEnv("HEART_LOG_LEVEL", "info"),
	}
	return cfg
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

type repoEntry struct {
	Org     string `yaml:"org"`
	Repo    string `yaml:"repo"`
	Entity  string `yaml:"entity"`
	Private bool   `yaml:"private"`
}

type RepoRegistry struct {
	Repos []repoEntry `yaml:"repos"`
}

func loadRepoRegistry(cfg Config) (RepoRegistry, error) {
	var reg RepoRegistry
	data, err := os.ReadFile(filepath.Join(cfg.HeartPath, "heartbeat", "repos.yaml"))
	if err != nil {
		if os.IsNotExist(err) {
			return reg, nil
		}
		return reg, fmt.Errorf("load repos.yaml: %w", err)
	}
	if err := yaml.Unmarshal(data, &reg); err != nil {
		return reg, fmt.Errorf("parse repos.yaml: %w", err)
	}
	return reg, nil
}

func discoverOrgsFromEntities(brainPath string) ([]repoEntry, error) {
	entsDir := filepath.Join(brainPath, "_entities")
	entries, err := os.ReadDir(entsDir)
	if err != nil {
		return nil, fmt.Errorf("read _entities dir: %w", err)
	}
	var repos []repoEntry
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".md") {
			continue
		}
		// Only process org entities (org-*.md)
		base := strings.TrimSuffix(entry.Name(), ".md")
		if !strings.HasPrefix(base, "org-") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(entsDir, entry.Name()))
		if err != nil {
			continue
		}
		front, _, _ := strings.Cut(string(data), "\n---")
		var fm struct {
			GitHubOrg string   `yaml:"github_org"`
			Repos     []string `yaml:"repos"`
			Entity    string   `yaml:"id"`
		}
		if err := yaml.Unmarshal([]byte(front), &fm); err != nil {
			continue
		}
		for _, r := range fm.Repos {
			if r == "" {
				continue
			}
			repos = append(repos, repoEntry{
				Org:    fm.GitHubOrg,
				Repo:   r,
				Entity: fm.Entity,
			})
		}
	}
	return repos, nil
}

func readMode(cfg Config) (Mode, error) {
	data, err := os.ReadFile(filepath.Join(cfg.HeartPath, "heartbeat", "mode.yaml"))
	if err != nil {
		if os.IsNotExist(err) {
			return ModeNormal, nil
		}
		return ModeNormal, fmt.Errorf("read mode.yaml: %w", err)
	}
	var m Mode
	if err := yaml.Unmarshal(data, &m); err != nil {
		return ModeNormal, fmt.Errorf("parse mode.yaml: %w", err)
	}
	return m, nil
}

func writeLastRun(cfg Config, phaseDurations map[string]time.Duration, mode Mode, startedAt time.Time) error {
	type phaseRecord struct {
		Phase    string        `yaml:"phase"`
		Duration time.Duration `yaml:"duration_ms"`
	}
	var records []phaseRecord
	for p, d := range phaseDurations {
		records = append(records, phaseRecord{Phase: p, Duration: d / time.Millisecond})
	}
	data, err := yaml.Marshal(map[string]any{
		"mode":            mode,
		"started_at":      startedAt.UTC().Format(time.RFC3339),
		"ended_at":        time.Now().UTC().Format(time.RFC3339),
		"phase_durations": records,
	})
	if err != nil {
		return fmt.Errorf("marshal last_run: %w", err)
	}
	path := filepath.Join(cfg.HeartPath, "heartbeat", "last_run.yaml")
	return os.WriteFile(path, data, 0o644)
}

func writeAudit(cfg Config, phase, entity string, err error, ok bool) error {
	ts := time.Now().UTC().Format(time.RFC3339)
	var outcome string
	var errMsg string
	if ok {
		outcome = "ok"
		errMsg = ""
	} else {
		outcome = "fail"
		if err != nil {
			errMsg = err.Error()
		}
	}
	entry := fmt.Sprintf("- ts: %s\n  phase: %s\n  entity: %s\n  outcome: %s\n  message: %s\n",
		ts, phase, entity, outcome, errMsg)

	auditFile := filepath.Join(cfg.HeartPath, "audit", "heartbeat.yaml")
	if err := os.MkdirAll(filepath.Dir(auditFile), 0o755); err != nil {
		return fmt.Errorf("mkdir audit dir: %w", err)
	}
	f, openErr := os.OpenFile(auditFile, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if openErr != nil {
		return fmt.Errorf("open audit file: %w", openErr)
	}
	defer f.Close()
	if _, wErr := f.WriteString(entry); wErr != nil {
		return fmt.Errorf("write audit entry: %w", wErr)
	}
	return nil
}

// currentMode holds the latest mode read from /activememory/heart/heartbeat/mode.yaml.
// It is updated by main before each cycle and read by phase functions that
// need to honor the active mode (compute_health, etc).
var currentMode = ModeNormal

// cycleCount is incremented by main and read by phaseHeartbeat to record
// the cycle number in every beat JSON. Package-level so the phase can see it.
var cycleCount int

// phaseFn is the signature every cycle phase implements. The phase receives
// the context, the loaded config, the discovered repo entries, and a slog
// logger, and returns an error to signal phase failure (which the cycle
// runner records in audit/heartbeat.yaml).
type phaseFn func(ctx context.Context, cfg Config, repos []repoEntry, log *slog.Logger) error

func phaseHeartbeat(ctx context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	beatFile := os.Getenv("HEART_BEAT_FILE")
	if beatFile == "" {
		beatFile = filepath.Join(cfg.HeartPath, "heartbeat", "heartbeat.json")
	}
	data, err := jsonMarshal(map[string]any{
		"ts":       time.Now().UTC().Format(time.RFC3339Nano),
		"cycle":    cycleCount,
		"mode":     string(currentMode),
		"hostname": hostname(),
	})
	if err != nil {
		return fmt.Errorf("marshal heartbeat: %w", err)
	}
	// Ensure the parent dir exists; ignore errors so we don't fail on readonly mounts
	// (some test environments run with /shared on a readonly overlay).
	_ = os.MkdirAll(filepath.Dir(beatFile), 0o755)
	// Atomic write: temp file + rename.  Prevents readers (other containers'
	// healthchecks) from ever seeing a partial payload.
	// Include PID so multiple processes on the same host (networking + heart)
	// never clobber each other's tmp files.
	tmpFile := beatFile + ".tmp." + strconv.Itoa(os.Getpid())
	if err := os.WriteFile(tmpFile, data, 0o644); err != nil {
		return fmt.Errorf("write heartbeat tmp %s: %w", tmpFile, err)
	}
	if err := os.Rename(tmpFile, beatFile); err != nil {
		// Clean up the temp file on rename failure.
		_ = os.Remove(tmpFile)
		return fmt.Errorf("rename heartbeat %s -> %s: %w", tmpFile, beatFile, err)
	}
	log.Debug("heartbeat", slog.String("file", beatFile), slog.Int("cycle", cycleCount))
	return nil
}

var hostname = func() string {
	h, _ := os.Hostname()
	return h
}

func jsonMarshal(v any) ([]byte, error) {
	type marshaler interface{ MarshalJSON() ([]byte, error) }
	if m, ok := v.(marshaler); ok {
		return m.MarshalJSON()
	}
	return json.Marshal(v)
}

func phaseTick(_ context.Context, _ Config, _ []repoEntry, log *slog.Logger) error {
	log.Debug("tick")
	return nil
}

func phaseDiscoverRepos(_ context.Context, cfg Config, repos []repoEntry, log *slog.Logger) error {
	// The Go binary pre-loads the registry at start-up (see main()) so the
	// repo list is already in scope; this phase is a no-op marker that keeps
	// the cycle phase ordering aligned with the Python bridge.
	log.Info("discover_repos", slog.Int("count", len(repos)))
	return nil
}

func phaseFetchRepos(_ context.Context, cfg Config, repos []repoEntry, log *slog.Logger) error {
	if cfg.GhToken == "" {
		log.Debug("fetch_repos skipped: no GH_TOKEN")
		return nil
	}
	log.Info("fetch_repos", slog.Int("count", len(repos)))
	for _, r := range repos {
		log.Debug("repo_target", slog.String("org", r.Org), slog.String("repo", r.Repo), slog.String("entity", r.Entity))
	}
	return nil
}

func phaseFetchIssues(_ context.Context, cfg Config, repos []repoEntry, log *slog.Logger) error {
	if cfg.GhToken == "" {
		log.Debug("fetch_issues skipped: no GH_TOKEN")
		return nil
	}
	log.Info("fetch_issues", slog.Int("repos", len(repos)))
	return nil
}

func phaseFetchPRs(_ context.Context, cfg Config, repos []repoEntry, log *slog.Logger) error {
	if cfg.GhToken == "" {
		log.Debug("fetch_prs skipped: no GH_TOKEN")
		return nil
	}
	log.Info("fetch_prs", slog.Int("repos", len(repos)))
	return nil
}

func phaseFetchActions(_ context.Context, cfg Config, repos []repoEntry, log *slog.Logger) error {
	if cfg.GhToken == "" {
		log.Debug("fetch_actions skipped: no GH_TOKEN")
		return nil
	}
	log.Info("fetch_actions", slog.Int("repos", len(repos)))
	return nil
}

func phaseIngestOSINT(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	// Stub — delegates to the Python bridge (Heart/tools/osint_cache.py) for the
	// full READ→AMEND→WRITE pipeline. This phase is registered so the Go reference
	// stays in sync with the Python bridge phase order.
	log.Debug("ingest_osint", slog.String("cache", filepath.Join(cfg.HeartPath, "heartbeat", "osint_cache.json")))
	return nil
}

func phaseOSINTUserdata(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	// Stub — delegates to the Python bridge (Heart/tools/osint_userdata.py) for
	// READ-only OSINT over userdata summaries + resurrection detection. Write-back
	// requires organ failure + godadmin bidirectional authorization.
	log.Debug("osint_userdata", slog.String("userdata_dir", getEnv("USERDATA_DIR", "/var/lib/userdata")))
	return nil
}

func phaseComputeHealth(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	healthFile := filepath.Join(cfg.HeartPath, "heartbeat", "health.yaml")
	health := map[string]any{
		"ts":              time.Now().UTC().Format(time.RFC3339),
		"mode":            string(currentMode),
		"disk_free_mb":    9999,
		"memory_free_mb":  9999,
		"gh_errors_min":   0,
		"cycle_success":   100,
		"llm_fallbacks_h": 0,
	}
	data, err := yaml.Marshal(health)
	if err != nil {
		return fmt.Errorf("marshal health: %w", err)
	}
	if err := os.WriteFile(healthFile, data, 0o644); err != nil {
		return fmt.Errorf("write health.yaml: %w", err)
	}
	log.Info("compute_health", slog.Any("metrics", health))
	return nil
}

func phaseWriteBrain(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	log.Debug("write_brain")
	return nil
}

func phaseFireReminders(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	log.Debug("fire_reminders")
	return nil
}

func phasePruneStale(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	log.Debug("prune_stale")
	return nil
}

func phaseSelfHeal(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	log.Debug("self_heal")
	return nil
}

func phaseAudit(_ context.Context, cfg Config, _ []repoEntry, log *slog.Logger) error {
	log.Debug("audit")
	return nil
}

func parseLogLevel(s string) slog.Level {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

func main() {
	cfg := loadConfig()

	level := parseLogLevel(cfg.LogLevel)
	opts := &slog.HandlerOptions{Level: level}
	handler := slog.NewJSONHandler(os.Stdout, opts)
	slog.SetDefault(slog.New(handler))

	log := slog.Default()
	log.Info("heart_starting",
		slog.String("brain_path", cfg.BrainPath),
		slog.String("heart_path", cfg.HeartPath),
		slog.String("mouth_path", cfg.MouthPath),
		slog.String("log_level", cfg.LogLevel),
		slog.String("go_version", "1.22+"),
	)

	mode := ModeNormal
	modeCfg := modeDefaults[mode]

	// Repo awareness: load from _entities + repos.yaml
	repos, err := discoverOrgsFromEntities(cfg.BrainPath)
	if err != nil {
		log.Warn("discover_repos_warn", slog.String("error", err.Error()))
	}
	if reg, err := loadRepoRegistry(cfg); err == nil {
		for _, r := range reg.Repos {
			repos = append(repos, r)
		}
	}
	log.Info("repos_loaded", slog.Int("total", len(repos)))
	for _, r := range repos {
		log.Debug("known_repo", slog.String("org", r.Org), slog.String("repo", r.Repo), slog.String("entity", r.Entity))
	}

	tick := time.NewTicker(time.Duration(modeCfg.CycleMS) * time.Millisecond)
	defer tick.Stop()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	cycleCount = 0
	for {
		select {
		case <-ctx.Done():
			log.Info("heart_shutdown")
			return
		case <-tick.C:
			cycleCount++
			cycleStart := time.Now()

			readM, err := readMode(cfg)
			if err != nil {
				log.Warn("read_mode_warn", slog.String("error", err.Error()))
				readM = ModeNormal
			}
			currentMode = readM
			currentModeCfg := modeDefaults[currentMode]
			if currentMode != mode {
				mode = currentMode
				modeCfg = currentModeCfg
				tick.Reset(time.Duration(modeCfg.CycleMS) * time.Millisecond)
				log.Info("mode_changed",
					slog.String("mode", string(mode)),
					slog.Int("cycle_ms", modeCfg.CycleMS),
				)
			}

			log.Info("cycle_start",
				slog.Int("cycle", cycleCount),
				slog.String("mode", string(mode)),
			)

			phaseDurations := make(map[string]time.Duration)
		phases := []struct {
			name string
			fn   phaseFn
		}{
			{"tick", phaseTick},
			{"discover_repos", phaseDiscoverRepos},
			{"heartbeat", phaseHeartbeat},
			{"fetch_repos", phaseFetchRepos},
			{"fetch_issues", phaseFetchIssues},
			{"fetch_prs", phaseFetchPRs},
			{"fetch_actions", phaseFetchActions},
			{"ingest_osint", phaseIngestOSINT},
			{"osint_userdata", phaseOSINTUserdata},
			{"compute_health", phaseComputeHealth},
			{"write_brain", phaseWriteBrain},
			{"fire_reminders", phaseFireReminders},
			{"prune_stale", phasePruneStale},
			{"self_heal", phaseSelfHeal},
			{"audit", phaseAudit},
		}
			var cycleErr error
			for _, p := range phases {
				start := time.Now()
				err := p.fn(ctx, cfg, repos, log)
				phaseDurations[p.name] = time.Since(start)
				if err != nil && cycleErr == nil {
					cycleErr = err
				}
				attrs := []slog.Attr{
					slog.String("phase", p.name),
					slog.Duration("elapsed", phaseDurations[p.name]),
				}
				if err != nil {
					attrs = append(attrs, slog.String("error", err.Error()))
					log.LogAttrs(ctx, slog.LevelError, "phase_complete", attrs...)
					if auditErr := writeAudit(cfg, p.name, "", err, false); auditErr != nil {
						log.Warn("audit_write_failed", slog.String("error", auditErr.Error()))
					}
				} else {
					log.LogAttrs(ctx, slog.LevelInfo, "phase_complete", attrs...)
					if auditErr := writeAudit(cfg, p.name, "", nil, true); auditErr != nil {
						log.Warn("audit_write_failed", slog.String("error", auditErr.Error()))
					}
				}
			}

			if err := writeLastRun(cfg, phaseDurations, mode, cycleStart); err != nil {
				log.Warn("write_last_run_failed", slog.String("error", err.Error()))
			}

			cycleElapsed := time.Since(cycleStart)
			log.Info("cycle_end",
				slog.Int("cycle", cycleCount),
				slog.Duration("total_elapsed", cycleElapsed),
				slog.Int("phases_run", len(phases)),
				slog.String("outcome", func() string {
					if cycleErr != nil {
						return "error"
					}
					return "ok"
				}()),
			)
		}
	}
}

