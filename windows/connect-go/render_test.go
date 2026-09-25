package main

import (
	"image"
	"image/png"
	"os"
	"path/filepath"
	"testing"
	"time"

	"gioui.org/gpu/headless"
	"gioui.org/io/input"
	"gioui.org/layout"
	"gioui.org/op"
	"gioui.org/unit"
)

// Opt-in, real Gio renderer: no web mockup and no account or installer side effects.
func TestRenderScreens(t *testing.T) {
	dir := os.Getenv("PETABYTE_RENDER_DIR")
	if dir == "" {
		t.Skip("set PETABYTE_RENDER_DIR to capture the actual Gio UI")
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		phase phase
		step  int
		size  image.Point
		scale float32
	}{
		{"welcome", ready, 0, image.Pt(980, 780), 1},
		{"review", review, 0, image.Pt(980, 900), 1},
		{"signin", signingIn, 1, image.Pt(980, 780), 1},
		{"installing", installing, 2, image.Pt(980, 780), 1},
		{"complete", complete, 3, image.Pt(980, 780), 1},
		{"error", failed, 0, image.Pt(980, 780), 1},
		{"minimum", review, 0, image.Pt(760, 620), 1},
		{"hidpi", ready, 0, image.Pt(1960, 1560), 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w, err := headless.NewWindow(tc.size.X, tc.size.Y)
			if err != nil {
				t.Fatal(err)
			}
			defer w.Release()
			a := newApplication()
			a.state.phase = tc.phase
			a.state.step = tc.step
			if tc.phase != ready {
				a.state.gpu = "NVIDIA GeForce RTX 4090"
				a.state.wsl = "Ubuntu 24.04 ready"
			}
			switch tc.phase {
			case review:
				a.state.status = "Compatibility checks passed"
				a.state.detail = "The next step links your account and installs the background agent. Administrator permission is required."
			case signingIn:
				a.state.status = "Continue in your browser"
				a.state.detail = "Sign in at petabyte.market and connect this PC. This request expires after 5 minutes."
				a.state.browserURL = apiURL + "/install"
			case installing:
				a.state.status = "Installing the Petabyte agent…"
				a.state.detail = "This can take several minutes. WSL will restart. Do not close this window or turn off your PC."
			case complete:
				a.state.status = "Agent installer completed"
				a.state.detail = "Online status and earnings are shown in your dashboard. This window does not monitor the agent."
			case failed:
				a.state.gpu = "Driver not detected"
				a.state.status = "This PC needs a little preparation"
				a.state.detail = "An NVIDIA GPU with a working Windows driver is required. Install or update the NVIDIA driver, then retry."
			}
			var ops op.Ops
			var router input.Router
			gtx := layout.Context{Ops: &ops, Source: router.Source(), Now: time.Now(), Metric: unit.Metric{PxPerDp: tc.scale, PxPerSp: tc.scale}, Constraints: layout.Exact(tc.size)}
			a.drawUI(gtx, newTheme())
			if err = w.Frame(&ops); err != nil {
				t.Fatal(err)
			}
			img := image.NewRGBA(image.Rectangle{Max: tc.size})
			if err = w.Screenshot(img); err != nil {
				t.Fatal(err)
			}
			f, err := os.Create(filepath.Join(dir, tc.name+".png"))
			if err != nil {
				t.Fatal(err)
			}
			err = png.Encode(f, img)
			closeErr := f.Close()
			if err != nil {
				t.Fatal(err)
			}
			if closeErr != nil {
				t.Fatal(closeErr)
			}
		})
	}
}
