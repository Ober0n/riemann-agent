package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tamnd/riemann-agent/site"
	"github.com/tamnd/riemann-agent/trust"
)

func TestWriteTrust(t *testing.T) {
	dir := t.TempDir()
	ax := trust.Axioms{Date: "2026-11-21", Module: "Main", Lean: "Lean (version 4.33.1)", OK: true, Theorems: []trust.Theorem{{Name: "riemannHypothesis", Print: trust.Standard, Walk: trust.Standard, Match: true, Extra: []string{}}}}
	rp := &trust.Replay{Date: "2026-11-21", Tool: "lean4checker --fresh", Module: "Main", Seconds: 3600, Exit: 1}
	if err := writeTrust(dir, ax, rp); err != nil {
		t.Fatal(err)
	}
	var tr site.Trust
	data, err := os.ReadFile(filepath.Join(dir, "records", "trust.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &tr); err != nil {
		t.Fatal(err)
	}
	states := map[string]string{}
	for _, c := range tr.Checks {
		states[c.ID] = c.State
	}
	if states["Q2"] != "yes" || states["Q3"] != "no" || states["Q1"] != "not run" || len(tr.Checks) != 6 {
		t.Fatalf("got %v", states)
	}
	if tr.Updated != "2026-11-21" {
		t.Errorf("updated is %q", tr.Updated)
	}
	for _, f := range []string{"axioms.json", "replay.json"} {
		if _, err := os.Stat(filepath.Join(dir, "records", f)); err != nil {
			t.Error(err)
		}
	}

	// A second run keeps the other rows and changes Q2.
	ax.OK = false
	ax.Theorems[0].Extra = []string{"sorryAx"}
	if err := writeTrust(dir, ax, nil); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(filepath.Join(dir, "records", "trust.json"))
	if !strings.Contains(string(data), "riemannHypothesis uses sorryAx") || !strings.Contains(string(data), "lean4checker --fresh on Main exited with code 1") {
		t.Fatalf("got %s", data)
	}
}

func TestWriteTrustCached(t *testing.T) {
	dir := t.TempDir()
	arts := &trust.Artifacts{Kind: trust.Cached, Source: "https://rh-cache.example.org", Revision: "e50d098", Pins: map[string]string{"map": strings.Repeat("a", 64)}}
	ax := trust.Axioms{Date: "2026-11-22", Module: "Main", Lean: "Lean (version 4.33.1)", OK: true, Artifacts: arts, Theorems: []trust.Theorem{{Name: "riemannHypothesis", Print: trust.Standard, Walk: trust.Standard, Match: true, Extra: []string{}}}}
	rp := &trust.Replay{Date: "2026-11-22", Tool: "lean4checker --fresh", Module: "Main", Seconds: 60, Exit: 0, OK: true, Artifacts: arts}
	if err := writeTrust(dir, ax, rp); err != nil {
		t.Fatal(err)
	}
	var tr site.Trust
	data, err := os.ReadFile(filepath.Join(dir, "records", "trust.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &tr); err != nil {
		t.Fatal(err)
	}
	for _, c := range tr.Checks {
		switch c.ID {
		case "Q1":
			if c.State != "not run" || c.Artifacts != "" {
				t.Errorf("Q1 is %q (%q); cached files must not answer the build question", c.State, c.Artifacts)
			}
		case "Q2", "Q3":
			// The outcome and the provenance are separate fields.
			if c.State != "yes" || c.Class() != "yes" {
				t.Errorf("%s is %q with class %q, want the outcome yes", c.ID, c.State, c.Class())
			}
			if !c.Cached() || c.Artifacts != site.ArtifactsCached || c.Source != "https://rh-cache.example.org at e50d098" {
				t.Errorf("%s provenance: %q, %q", c.ID, c.Artifacts, c.Source)
			}
			if !strings.Contains(c.Note, "not an independent clean build") {
				t.Errorf("%s note: %q", c.ID, c.Note)
			}
		}
	}
	// Even with every check yes, cached files do not make a claim.
	all := tr
	all.Checks = append([]site.TrustCheck(nil), tr.Checks...)
	for i := range all.Checks {
		all.Checks[i].State = "yes"
	}
	if all.Claimed() {
		t.Error("a report with a check on cached files must not count as claimed")
	}
	for i := range all.Checks {
		if all.Checks[i].Cached() {
			all.Checks[i].Artifacts = site.ArtifactsClean
		}
	}
	if !all.Claimed() {
		t.Error("the same report on clean builds must count as claimed")
	}
	ax2, err := os.ReadFile(filepath.Join(dir, "records", "axioms.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(ax2), `"kind": "cached"`) {
		t.Errorf("axioms.json has no source: %s", ax2)
	}

	// A failure on cached files stays no.
	ax.OK = false
	ax.Theorems[0].Extra = []string{"sorryAx"}
	if err := writeTrust(dir, ax, nil); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(filepath.Join(dir, "records", "trust.json"))
	if err := json.Unmarshal(data, &tr); err != nil {
		t.Fatal(err)
	}
	for _, c := range tr.Checks {
		if c.ID == "Q2" && (c.State != "no" || !c.Cached()) {
			t.Errorf("Q2 is %q (%q) after a failure, want no on cached files", c.State, c.Artifacts)
		}
	}
}

func TestTrustPrint(t *testing.T) {
	var out, errb strings.Builder
	if code := run([]string{"trust", "-print", "-theorems", "x,y"}, &out, &errb); code != 0 {
		t.Fatalf("exit %d: %s", code, errb.String())
	}
	if !strings.Contains(out.String(), "#print axioms y") {
		t.Fatalf("got %s", out.String())
	}
	if code := run([]string{"trust"}, &out, &errb); code != 2 {
		t.Fatalf("exit %d without -lean, want 2", code)
	}
}
