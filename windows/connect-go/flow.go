package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

var tokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{8,128}$`)

func pause(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
func (a *application) fail(ctx context.Context, title, detail string) {
	if ctx.Err() != nil {
		title, detail = "Setup stopped", "No installation was started. Check this PC again when you are ready."
	}
	a.update(func(s *viewState) { s.phase = failed; s.status = title; s.detail = detail; s.browserURL = "" })
}
func (a *application) check(ctx context.Context) {
	if a.demo {
		if pause(ctx, time.Second) != nil {
			a.fail(ctx, "", "")
			return
		}
		a.update(func(s *viewState) { s.gpu = "NVIDIA GPU (preview)"; s.wsl = "Ubuntu 24.04 (preview)" })
	} else {
		if err := a.checkPC(ctx); err != nil {
			a.fail(ctx, "This PC needs a little preparation", err.Error())
			return
		}
	}
	if ctx.Err() != nil {
		a.fail(ctx, "", "")
		return
	}
	a.update(func(s *viewState) {
		s.phase = review
		s.status = "Compatibility checks passed"
		s.detail = "The next step links your account and installs the background agent. Administrator permission is required."
	})
}
func (a *application) enroll(ctx context.Context) {
	token := ""
	var err error
	if a.demo {
		err = pause(ctx, 2*time.Second)
	} else {
		token, err = a.deviceAuth(ctx)
	}
	if err != nil {
		a.fail(ctx, "Sign-in did not finish", err.Error())
		return
	}
	if ctx.Err() != nil {
		a.fail(ctx, "", "")
		return
	}
	a.update(func(s *viewState) {
		s.phase = installing
		s.step = 2
		s.browserURL = ""
		s.status = "Installing the Petabyte agent…"
		s.detail = "This can take several minutes. WSL will restart. Do not close this window or turn off your PC."
	})
	if a.demo {
		err = pause(ctx, 2*time.Second)
	} else {
		err = runInstaller(ctx, token)
	}
	if err != nil {
		// Installation may already have made changes; never describe it as rolled back.
		a.update(func(s *viewState) {
			s.phase = failed
			s.status = "Installation did not finish"
			s.detail = "Some components may already be installed. " + err.Error() + " Open Setup help before retrying."
		})
		return
	}
	a.update(func(s *viewState) {
		s.phase = complete
		s.step = 3
		s.status = "Agent installer completed"
		s.detail = "Online status and earnings are shown in your dashboard. This window does not monitor the agent."
	})
	if a.demo {
		a.update(func(s *viewState) {
			s.status = "Preview completed"
			s.detail = "No account was linked and no software was installed."
		})
	}
}

// The browser contract uses GET /callback?token=...&state=... on loopback only.
// No token is echoed, logged, or interpolated into a shell command.
func callbackHandler(state string, result chan<- string) http.Handler {
	var once sync.Once
	mux := http.NewServeMux()
	mux.HandleFunc("/callback", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			http.Error(w, "Method not allowed", 405)
			return
		}
		token := r.URL.Query().Get("token")
		if subtle.ConstantTimeCompare([]byte(r.URL.Query().Get("state")), []byte(state)) != 1 || !tokenPattern.MatchString(token) {
			http.Error(w, "Invalid or expired sign-in request. Return to Petabyte Connect.", 400)
			return
		}
		accepted := false
		once.Do(func() { result <- token; accepted = true })
		if !accepted {
			http.Error(w, "This sign-in has already been received. Return to Petabyte Connect.", 409)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Account linked · Petabyte</title><body style="background:#0d1119;color:#f1f5fa;font:16px system-ui;display:grid;min-height:90vh;place-items:center"><main style="max-width:460px;padding:32px"><p style="color:#55c7fa">PETABYTE CONNECT</p><h1>Account linked.</h1><p>Return to the Petabyte Connect window to follow installation progress.</p><p>You can close this tab.</p></main></body></html>`)
	})
	return mux
}
func (a *application) deviceAuth(ctx context.Context) (string, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", errors.New("Could not start local sign-in. Close other setup windows and retry.")
	}
	defer ln.Close()
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return "", errors.New("Could not create a secure sign-in request. Please retry.")
	}
	state := hex.EncodeToString(nonce)
	result := make(chan string, 1)
	srv := &http.Server{Handler: callbackHandler(state, result), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 10 * time.Second, MaxHeaderBytes: 8192}
	serverErr := make(chan error, 1)
	go func() { serverErr <- srv.Serve(ln) }()
	defer srv.Close()
	query := url.Values{"desktop_cb": {ln.Addr().String()}, "state": {state}, "src": {"bundle"}}
	link := apiURL + "/install?" + query.Encode() + "#account"
	a.update(func(s *viewState) { s.browserURL = link })
	if err := openBrowser(link); err != nil {
		a.update(func(s *viewState) {
			s.detail = "Your browser could not open. Choose Reopen browser to try again, or cancel setup."
		})
	}
	timer := time.NewTimer(5 * time.Minute)
	defer timer.Stop()
	select {
	case token := <-result:
		return token, nil
	case <-ctx.Done():
		return "", ctx.Err()
	case <-timer.C:
		return "", errors.New("The 5-minute sign-in window expired. Retry to open a fresh request.")
	case <-serverErr:
		return "", errors.New("Local sign-in stopped unexpectedly. Please retry.")
	}
}

// Parse WSL's localized table using only the stable distro name and version column.
func ubuntuWSL2(output string) bool {
	output = strings.ReplaceAll(output, "\x00", "")
	for _, row := range strings.Split(output, "\n") {
		fields := strings.Fields(strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(row), "*")))
		if len(fields) >= 3 && fields[0] == "Ubuntu-24.04" && fields[len(fields)-1] == "2" {
			return true
		}
	}
	return false
}
