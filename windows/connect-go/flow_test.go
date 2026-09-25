package main

import (
	"context"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestCallbackBoundary(t *testing.T) {
	result := make(chan string, 1)
	h := callbackHandler("expected-state", result)
	for _, tc := range []struct {
		method, state, token string
		status               int
	}{
		{"POST", "expected-state", "valid_token_123", 405},
		{"GET", "wrong", "valid_token_123", 400},
		{"GET", "expected-state", "x';Start-Process calc;#", 400},
		{"GET", "expected-state", strings.Repeat("x", 129), 400},
		{"GET", "expected-state", "", 400},
		{"GET", "expected-state", "valid_token_123", 200},
		{"GET", "expected-state", "other_token_123", 409},
	} {
		r := httptest.NewRequest(tc.method, "/callback?"+url.Values{"state": {tc.state}, "token": {tc.token}}.Encode(), nil)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Errorf("%s %q: got %d want %d", tc.method, tc.token, w.Code, tc.status)
		}
		if w.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("callback response is cacheable")
		}
		if strings.Contains(w.Body.String(), "valid_token_123") {
			t.Fatal("callback leaks token")
		}
	}
	if len(result) != 1 || <-result != "valid_token_123" {
		t.Fatal("accepted more than one token or accepted an invalid token")
	}
}
func TestWSLRequirements(t *testing.T) {
	for _, tc := range []struct {
		output string
		want   bool
	}{
		{"  NAME STATE VERSION\n* Ubuntu-24.04 Running 2\n", true},
		{"Ubuntu-24.04 Arrêté 2", true},
		{"Ubuntu-24.04 Running 1", false},
		{"Ubuntu-22.04 Running 2", false},
		{"OtherUbuntu-24.04 Running 2", false},
		{"WSL is not installed", false},
	} {
		if got := ubuntuWSL2(tc.output); got != tc.want {
			t.Errorf("%q: %v", tc.output, got)
		}
	}
	utf16 := strings.Join(strings.Split("Ubuntu-24.04 Running 2", ""), "\x00")
	if !ubuntuWSL2(utf16) {
		t.Fatal("Windows UTF-16 output rejected")
	}
}
func TestConsentIsRequired(t *testing.T) {
	a := newApplication()
	a.demo = true
	a.state.phase = review
	a.act(a.snapshot())
	if a.snapshot().phase != review || a.cancel != nil {
		t.Fatal("installation started without consent")
	}
}
func TestCanceledSignInCannotInstall(t *testing.T) {
	a := newApplication()
	a.demo = true
	a.state.phase = signingIn
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	a.enroll(ctx)
	if s := a.snapshot(); s.phase != failed || s.status != "Setup stopped" {
		t.Fatalf("unexpected state: %+v", s)
	}
}
func TestInvalidTokenCannotStartInstaller(t *testing.T) {
	if runInstaller(context.Background(), "';calc;#") == nil {
		t.Fatal("unsafe token accepted")
	}
}
