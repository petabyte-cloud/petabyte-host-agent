// Petabyte Connect is a native Windows setup assistant for the WSL agent.
package main

import (
	"context"
	"os"
	"sync"

	"gioui.org/app"
	"gioui.org/op"
	"gioui.org/unit"
	"gioui.org/widget"
)

var version = "0.2.0"
var previewMode = "false" // Set only for the separate, non-installing preview build.

const apiURL = "https://petabyte.market"

type phase int

const (
	ready phase = iota
	checking
	review
	signingIn
	installing
	complete
	failed
)

type viewState struct {
	phase                                phase
	step                                 int
	status, detail, gpu, wsl, browserURL string
	needsAdmin                           bool
}
type application struct {
	mu                                sync.Mutex
	state                             viewState
	window                            *app.Window
	cancel                            context.CancelFunc
	primary, secondary, help, privacy widget.Clickable
	consent                           widget.Bool
	list                              widget.List
	demo                              bool
}

func newApplication() *application {
	return &application{state: viewState{phase: ready, gpu: "Not checked", wsl: "Not checked",
		status: "Start with a quick compatibility check.", detail: "Checking your PC does not install or change anything."}}
}
func (a *application) snapshot() viewState {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.state
}
func (a *application) update(f func(*viewState)) {
	a.mu.Lock()
	f(&a.state)
	a.mu.Unlock()
	if a.window != nil {
		a.window.Invalidate()
	}
}
func (a *application) stop() {
	if a.cancel != nil {
		a.cancel()
		a.cancel = nil
	}
}
func main() {
	a := newApplication()
	a.demo = previewMode == "true"
	for _, arg := range os.Args[1:] {
		switch arg {
		case "--selftest", "-selftest", "--dryrun", "-dryrun":
			a.demo = true
		}
	}
	go func() {
		a.window = new(app.Window)
		a.window.Option(app.Title("Petabyte Connect"), app.Size(unit.Dp(980), unit.Dp(780)),
			app.MinSize(unit.Dp(760), unit.Dp(620)))
		if err := a.loop(); err != nil {
			os.Exit(1)
		}
		os.Exit(0)
	}()
	app.Main()
}
func (a *application) loop() error {
	th := newTheme()
	var ops op.Ops
	for {
		switch e := a.window.Event().(type) {
		case app.Win32ViewEvent:
			setWindowIcon(e.HWND)
		case app.DestroyEvent:
			a.stop()
			return e.Err
		case app.FrameEvent:
			gtx := app.NewContext(&ops, e)
			s := a.snapshot()
			if a.primary.Clicked(gtx) {
				a.act(s)
			}
			if a.secondary.Clicked(gtx) {
				if s.phase == signingIn || s.phase == checking {
					a.stop()
				} else if s.phase == review {
					a.consent.Value = false
					a.update(func(s *viewState) {
						s.phase = ready
						s.status = "Setup has not started."
						s.detail = "You can check this PC again whenever you are ready."
					})
				}
			}
			if a.help.Clicked(gtx) {
				a.browse(apiURL + "/install")
			}
			if a.privacy.Clicked(gtx) {
				a.browse(apiURL + "/privacy")
			}
			a.drawUI(gtx, th)
			e.Frame(gtx.Ops)
		}
	}
}
func (a *application) browse(url string) {
	if a.demo {
		return
	}
	go func() {
		if err := openBrowser(url); err != nil {
			a.update(func(s *viewState) { s.detail = "Could not open your browser. Visit petabyte.market/install for help." })
		}
	}()
}
func (a *application) act(s viewState) {
	switch s.phase {
	case ready, failed:
		if s.needsAdmin && !a.demo {
			if err := restartElevated(); err != nil {
				a.update(func(s *viewState) { s.detail = "Administrator access was not granted. You can retry when ready." })
			} else {
				os.Exit(0)
			}
			return
		}
		a.stop()
		a.consent.Value = false
		ctx, cancel := context.WithCancel(context.Background())
		a.cancel = cancel
		a.update(func(s *viewState) {
			*s = viewState{phase: checking, gpu: "Checking…", wsl: "Checking…", status: "Checking your NVIDIA GPU and WSL 2…", detail: "This only reads device and Windows configuration."}
		})
		go a.check(ctx)
	case review:
		if !a.consent.Value {
			return
		}
		a.stop()
		ctx, cancel := context.WithCancel(context.Background())
		a.cancel = cancel
		a.update(func(s *viewState) {
			s.phase = signingIn
			s.step = 1
			s.status = "Continue in your browser"
			s.detail = "Sign in at petabyte.market and connect this PC. This request expires after 5 minutes."
		})
		go a.enroll(ctx)
	case signingIn:
		if s.browserURL != "" {
			a.browse(s.browserURL)
		}
	case complete:
		a.browse(apiURL + "/account")
	}
}
