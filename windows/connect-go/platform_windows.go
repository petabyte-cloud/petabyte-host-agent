package main

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
)

func hiddenCommand(ctx context.Context, exe string, args ...string) *exec.Cmd {
	c := exec.CommandContext(ctx, exe, args...)
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	return c
}
func systemExe(name string) string {
	dir, err := windows.GetSystemDirectory()
	if err != nil {
		return name
	}
	return filepath.Join(dir, name)
}
func powershell() string { return systemExe(`WindowsPowerShell\v1.0\powershell.exe`) }
func (a *application) checkPC(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	osv := windows.RtlGetVersion()
	if osv.MajorVersion < 10 || osv.BuildNumber < 19041 {
		return errors.New("Windows 10 version 2004 or newer is required. Update Windows, restart, and check again.")
	}
	// Resolve the vendor tool from Windows' system directory, not the app's working directory.
	gpu, err := hiddenCommand(ctx, systemExe("nvidia-smi.exe"), "--query-gpu=name", "--format=csv,noheader").Output()
	if err != nil || strings.TrimSpace(string(gpu)) == "" {
		a.update(func(s *viewState) { s.gpu = "Driver not detected" })
		return errors.New("An NVIDIA GPU with a working Windows driver is required. Install or update the NVIDIA driver, then retry.")
	}
	a.update(func(s *viewState) { s.gpu = strings.Join(strings.Fields(string(gpu)), " ") })
	wsl, err := hiddenCommand(ctx, systemExe("wsl.exe"), "--list", "--verbose").Output()
	if err != nil || !ubuntuWSL2(string(wsl)) {
		a.update(func(s *viewState) { s.wsl = "Preparation needed" })
		return errors.New("Set up WSL 2 with Ubuntu 24.04 first, including its first-run user setup. Open Setup help for instructions, then check again. This avoids an invisible setup prompt.")
	}
	a.update(func(s *viewState) { s.wsl = "Ubuntu 24.04 ready" })
	if !windows.GetCurrentProcessToken().IsElevated() {
		a.update(func(s *viewState) { s.needsAdmin = true })
		return errors.New("Windows needs administrator permission to install the background service. Choose Restart as administrator, approve the Windows prompt, then check this PC again.")
	}
	return ctx.Err()
}
func shellOpen(verb, target string) error {
	v, err := windows.UTF16PtrFromString(verb)
	if err != nil {
		return err
	}
	t, err := windows.UTF16PtrFromString(target)
	if err != nil {
		return err
	}
	return windows.ShellExecute(0, v, t, nil, nil, windows.SW_SHOWNORMAL)
}
func openBrowser(url string) error { return shellOpen("open", url) }
func restartElevated() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return shellOpen("runas", exe)
}

// goversioninfo puts the manifest at resource 1 and the icon group at 2.
// Gio v0.7 loads group 1 by default; set both title-bar and taskbar icons explicitly.
func setWindowIcon(hwnd uintptr) {
	if hwnd == 0 {
		return
	}
	var h windows.Handle
	err := windows.GetModuleHandleEx(2, nil, &h) // UNCHANGED_REFCOUNT
	if err != nil {
		return
	}
	user32 := windows.NewLazySystemDLL("user32.dll")
	loadImage := user32.NewProc("LoadImageW")
	// The Gio window thread is waiting for this view event to finish. Posting avoids
	// deadlocking it with a synchronous cross-thread SendMessage.
	postMessage := user32.NewProc("PostMessageW")
	for _, size := range []struct{ kind, pixels uintptr }{{0, 16}, {1, 32}} {
		icon, _, _ := loadImage.Call(uintptr(h), 2, 1, size.pixels, size.pixels, 0x8000)
		if icon != 0 {
			postMessage.Call(hwnd, 0x0080, size.kind, icon)
		}
	}
}

// The script is fetched over HTTPS by the existing enrollment endpoint. Keep the token
// in the child environment instead of interpolating it into executable PowerShell text.
const installerCommand = `$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $env:PETABYTE_IDLE_MINING='false'; try { $script=Invoke-RestMethod -Uri ($env:PETABYTE_CONNECT_ORIGIN+'/i/'+$env:PETABYTE_CONNECT_TOKEN+'.ps1'); Remove-Item Env:PETABYTE_CONNECT_TOKEN; Invoke-Expression $script; if (-not $?) { exit 1 }; exit 0 } catch { exit 1 }`

func runInstaller(ctx context.Context, token string) error {
	if !tokenPattern.MatchString(token) {
		return errors.New("The sign-in response was invalid. Sign in again.")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Minute)
	defer cancel()
	cmd := hiddenCommand(ctx, powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", installerCommand)
	// Remove inherited values before setting the installer policy.
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		switch strings.ToUpper(name) {
		case "PETABYTE_CONNECT_TOKEN", "PETABYTE_CONNECT_ORIGIN", "PETABYTE_IDLE_MINING":
			continue
		}
		cmd.Env = append(cmd.Env, entry)
	}
	cmd.Env = append(cmd.Env, "PETABYTE_CONNECT_TOKEN="+token, "PETABYTE_CONNECT_ORIGIN="+apiURL, "PETABYTE_IDLE_MINING=false")
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return errors.New("The installer was interrupted or exceeded 30 minutes.")
		}
		return errors.New("The installer reported an error. Check your internet connection and WSL setup.")
	}
	return nil
}
