package trust

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const pin = "fed2443b161bd8194d9507e5c9c0e555b865dbba6963a0d57172723a439e6941"

func TestReadArtifacts(t *testing.T) {
	dir := t.TempDir()
	write := func(name, body string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}

	if a, err := ReadArtifacts(""); a != nil || err != nil {
		t.Fatalf("empty path: got %v, %v", a, err)
	}

	good := write("good.json", `{"kind":"cached","source":"https://example.org/cache","revision":"e50d098","pins":{"map":"`+pin+`"}}`)
	a, err := ReadArtifacts(good)
	if err != nil {
		t.Fatal(err)
	}
	if !a.IsCached() || !strings.Contains(a.Describe(), "not an independent clean build") || !strings.Contains(a.Describe(), "at e50d098") {
		t.Fatalf("got %+v, %q", a, a.Describe())
	}

	clean, err := ReadArtifacts(write("clean.json", `{"kind":"clean","source":"lake build Main"}`))
	if err != nil {
		t.Fatal(err)
	}
	if clean.IsCached() || strings.Contains(clean.Describe(), "cached") {
		t.Fatalf("clean: got %q", clean.Describe())
	}

	bad := map[string]string{
		"kind":    `{"kind":"mirror","source":"x","pins":{"a":"` + pin + `"}}`,
		"source":  `{"kind":"cached","source":" ","pins":{"a":"` + pin + `"}}`,
		"no pins": `{"kind":"cached","source":"x"}`,
		"pin":     `{"kind":"cached","source":"x","pins":{"a":"ABC"}}`,
		"field":   `{"kind":"clean","source":"x","trusted":true}`,
	}
	for name, body := range bad {
		if _, err := ReadArtifacts(write(name+".json", body)); err == nil {
			t.Errorf("%s: no error", name)
		}
	}

	var none *Artifacts
	if none.IsCached() || none.Describe() != "" {
		t.Error("a nil value must not be cached and must have no description")
	}
}
