package trust

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strings"
)

// The kinds of Artifacts.
const (
	// Clean means that we built the compiled files ourselves, from the pinned source, with no files from other people.
	Clean = "clean"
	// Cached means that other people built the compiled files. A check on them shows what these files contain. It does not show that the pinned source builds.
	Cached = "cached"
)

// Artifacts tells where the compiled files of a check come from. A check records it in axioms.json, replay.json and extract.json, so that a reader can tell a check on cached files from a check on a clean build.
type Artifacts struct {
	Kind     string            `json:"kind"`               // Clean or Cached
	Source   string            `json:"source"`             // where the files come from, for example a URL
	Revision string            `json:"revision,omitempty"` // the revision of the source, for example a Git commit
	Pins     map[string]string `json:"pins,omitempty"`     // the SHA-256 of each pinned file, by name
	Note     string            `json:"note,omitempty"`
}

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Validate returns an error when a is not complete. A cached source must pin at least one file by its SHA-256.
func (a *Artifacts) Validate() error {
	switch a.Kind {
	case Clean, Cached:
	default:
		return fmt.Errorf("artifacts: the kind is %q, not %q or %q", a.Kind, Clean, Cached)
	}
	if strings.TrimSpace(a.Source) == "" {
		return fmt.Errorf("artifacts: the source is empty")
	}
	if a.Kind == Cached && len(a.Pins) == 0 {
		return fmt.Errorf("artifacts: a cached source needs at least one pin")
	}
	for name, h := range a.Pins {
		if !sha256Hex.MatchString(h) {
			return fmt.Errorf("artifacts: the pin of %s is not a SHA-256 in lower-case hex", name)
		}
	}
	return nil
}

// IsCached reports whether a is a cached source. A nil value is not cached.
func (a *Artifacts) IsCached() bool {
	return a != nil && a.Kind == Cached
}

// Describe returns one sentence that names the source, for the notes of the trust report.
func (a *Artifacts) Describe() string {
	if a == nil {
		return ""
	}
	s := a.Source
	if a.Revision != "" {
		s += " at " + a.Revision
	}
	if a.Kind == Cached {
		return fmt.Sprintf("The check ran on cached build files from %s. These files were built by other people. This is not an independent clean build.", s)
	}
	return fmt.Sprintf("The check ran on a clean build from %s.", s)
}

// ReadArtifacts reads and validates the file at path. An empty path gives nil and no error.
func ReadArtifacts(path string) (*Artifacts, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path) // #nosec G304 -- a path from the flags of rh
	if err != nil {
		return nil, err
	}
	var a Artifacts
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&a); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if err := a.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &a, nil
}
