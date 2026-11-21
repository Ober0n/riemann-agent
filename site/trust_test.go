package site

import (
	"html/template"
	"strings"
	"testing"
)

func TestChecksShowCachedFiles(t *testing.T) {
	tmpl, err := template.New("").Funcs(template.FuncMap{"url": func(p string) string { return "/" + p }, "num": num}).ParseFS(templateFS, "templates/*.html")
	if err != nil {
		t.Fatal(err)
	}
	tr := Trust{Checks: []TrustCheck{
		{ID: "Q0", Question: "Is the source the same as the release?", State: "yes"},
		{ID: "Q2", Question: "Which axioms does riemannHypothesis use?", State: "yes", Artifacts: ArtifactsCached, Source: "https://cache.example.org at e50d098"},
	}}
	var b strings.Builder
	if err := tmpl.ExecuteTemplate(&b, "checks", tr); err != nil {
		t.Fatal(err)
	}
	s := b.String()
	// The outcome keeps its own mark, and the provenance gets a second one.
	if !strings.Contains(s, `<span class="mark yes">yes</span><br><span class="mark open" title="https://cache.example.org at e50d098">cached files</span>`) {
		t.Errorf("the cached check has no provenance mark:\n%s", s)
	}
	if n := strings.Count(s, "cached files"); n != 1 {
		t.Errorf("%d provenance marks, want 1", n)
	}
	if tr.Claimed() {
		t.Error("a report with a check on cached files must not count as claimed")
	}
	tr.Checks[1].Artifacts = ArtifactsClean
	if !tr.Claimed() {
		t.Error("the same report on a clean build must count as claimed")
	}
}
