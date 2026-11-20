package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestVersion(t *testing.T) {
	var out, errb bytes.Buffer
	if code := run([]string{"version"}, &out, &errb); code != 0 {
		t.Fatalf("exit code %d, stderr %q", code, errb.String())
	}
	if got := out.String(); got != "rh dev\n" {
		t.Fatalf("version output %q", got)
	}
}

func TestHelpListsEachCommand(t *testing.T) {
	var out, errb bytes.Buffer
	if code := run(nil, &out, &errb); code != 0 {
		t.Fatalf("exit code %d", code)
	}
	for _, n := range names() {
		if !strings.Contains(out.String(), "  "+n+" ") {
			t.Errorf("the help text does not list %s", n)
		}
	}
}

func TestUnwrittenCommandExitsWith3(t *testing.T) {
	for _, c := range commands() {
		if c.run != nil {
			continue
		}
		var out, errb bytes.Buffer
		if code := run([]string{c.name}, &out, &errb); code != 3 {
			t.Errorf("rh %s: exit code %d, want 3", c.name, code)
		}
		if !strings.Contains(errb.String(), "Milestone "+c.milestone) {
			t.Errorf("rh %s: the message %q does not name milestone %s", c.name, errb.String(), c.milestone)
		}
	}
}

func TestUnknownCommandExitsWith2(t *testing.T) {
	var out, errb bytes.Buffer
	if code := run([]string{"frobnicate"}, &out, &errb); code != 2 {
		t.Fatalf("exit code %d, want 2", code)
	}
}
