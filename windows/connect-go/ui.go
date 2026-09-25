package main

import (
	"fmt"
	"image"
	"image/color"

	"gioui.org/font"
	"gioui.org/font/gofont"
	"gioui.org/layout"
	"gioui.org/op/clip"
	"gioui.org/op/paint"
	"gioui.org/text"
	"gioui.org/unit"
	"gioui.org/widget"
	"gioui.org/widget/material"
)

type C = layout.Context
type D = layout.Dimensions

var (
	bg     = rgb(0x0D1119)
	rail   = rgb(0x121823)
	panel  = rgb(0x171F2C)
	line   = rgb(0x2B3749)
	fg     = rgb(0xF1F5FA)
	muted  = rgb(0xA7B4C7)
	accent = rgb(0x55C7FA)
	green  = rgb(0x72DFB2)
	amber  = rgb(0xFFD18A)
)

func rgb(v uint32) color.NRGBA {
	return color.NRGBA{R: byte(v >> 16), G: byte(v >> 8), B: byte(v), A: 255}
}
func newTheme() *material.Theme {
	t := material.NewTheme()
	t.Shaper = text.NewShaper(text.WithCollection(gofont.Collection()))
	t.Palette.Bg, t.Palette.Fg, t.Palette.ContrastBg, t.Palette.ContrastFg = bg, fg, accent, bg
	return t
}
func label(th *material.Theme, size unit.Sp, value string, col color.NRGBA, bold bool) layout.Widget {
	return func(gtx C) D {
		l := material.Label(th, size, value)
		l.Color = col
		if bold {
			l.Font.Weight = font.Bold
		}
		return l.Layout(gtx)
	}
}
func gap(h unit.Dp) layout.FlexChild { return layout.Rigid(layout.Spacer{Height: h}.Layout) }
func column(gtx C, children ...layout.FlexChild) D {
	return layout.Flex{Axis: layout.Vertical}.Layout(gtx, children...)
}
func card(gtx C, col color.NRGBA, padding unit.Dp, content layout.Widget) D {
	return layout.Background{}.Layout(gtx, func(gtx C) D {
		paint.FillShape(gtx.Ops, col, clip.UniformRRect(image.Rectangle{Max: gtx.Constraints.Min}, gtx.Dp(12)).Op(gtx.Ops))
		return D{Size: gtx.Constraints.Min}
	}, func(gtx C) D { return layout.UniformInset(padding).Layout(gtx, content) })
}
func button(gtx C, th *material.Theme, b *widget.Clickable, title string, primary bool, enabled bool) D {
	if !enabled {
		gtx = gtx.Disabled()
	}
	style := material.Button(th, b, title)
	style.CornerRadius, style.TextSize = 8, 14
	style.Font.Weight = font.Bold
	style.Inset = layout.Inset{Top: 14, Bottom: 14, Left: 18, Right: 18}
	if !primary {
		style.Background = line
		style.Color = fg
	}
	return style.Layout(gtx)
}
func (a *application) drawUI(gtx C, th *material.Theme) D {
	paint.Fill(gtx.Ops, bg)
	s := a.snapshot()
	return layout.Flex{}.Layout(gtx,
		layout.Rigid(func(gtx C) D {
			gtx.Constraints.Min.X, gtx.Constraints.Max.X = gtx.Dp(214), gtx.Dp(214)
			return layout.Background{}.Layout(gtx, func(gtx C) D {
				paint.FillShape(gtx.Ops, rail, clip.Rect{Max: gtx.Constraints.Min}.Op())
				return D{Size: gtx.Constraints.Min}
			}, func(gtx C) D {
				return layout.Inset{Top: 30, Bottom: 22, Left: 22, Right: 18}.Layout(gtx, func(gtx C) D {
					return column(gtx,
						layout.Rigid(label(th, 26, "petabyte", fg, true)), gap(5),
						layout.Rigid(label(th, 12, "CONNECT  /  WINDOWS", accent, true)), gap(46),
						layout.Rigid(label(th, 11, "YOUR SETUP", muted, true)), gap(22),
						layout.Rigid(func(gtx C) D { return a.steps(gtx, th, s) }),
						layout.Flexed(1, func(gtx C) D { return D{Size: gtx.Constraints.Min} }),
						layout.Rigid(label(th, 13, "Your GPU. Your choice.", fg, true)), gap(8),
						layout.Rigid(label(th, 12, "Review every change before you connect.", muted, false)), gap(18),
						layout.Rigid(func(gtx C) D { return button(gtx, th, &a.help, "Setup help ↗", false, true) }), gap(12),
						layout.Rigid(label(th, 11, "Petabyte Connect  v"+version, muted, false)),
					)
				})
			})
		}),
		layout.Flexed(1, func(gtx C) D {
			return layout.Inset{Top: 28, Bottom: 20, Left: 30, Right: 30}.Layout(gtx, func(gtx C) D {
				a.list.Axis = layout.Vertical
				return column(gtx,
					layout.Flexed(1, func(gtx C) D {
						return material.List(th, &a.list).Layout(gtx, 1, func(gtx C, _ int) D { return a.content(gtx, th, s) })
					}),
					gap(14),
					layout.Rigid(func(gtx C) D { return a.actions(gtx, th, s) }),
				)
			})
		}),
	)
}
func (a *application) steps(gtx C, th *material.Theme, s viewState) D {
	names := []string{"Check this PC", "Link your account", "Install the agent", "Review your GPU"}
	desc := []string{"Compatibility", "Browser sign-in", "With your permission", "Open the dashboard"}
	var rows []layout.FlexChild
	for i, name := range names {
		i, name := i, name
		rows = append(rows, layout.Rigid(func(gtx C) D {
			col, num := muted, fmt.Sprint(i+1)
			if i < s.step {
				col, num = green, "✓"
			} else if i == s.step {
				col = accent
			}
			return layout.Inset{Bottom: 24}.Layout(gtx, func(gtx C) D {
				return layout.Flex{Alignment: layout.Middle}.Layout(gtx,
					layout.Rigid(func(gtx C) D { return card(gtx, line, 8, label(th, 12, num, col, true)) }),
					layout.Rigid(layout.Spacer{Width: 10}.Layout),
					layout.Flexed(1, func(gtx C) D {
						return column(gtx, layout.Rigid(label(th, 13, name, col, true)), gap(4), layout.Rigid(label(th, 11, desc[i], muted, false)))
					}),
				)
			})
		}))
	}
	return column(gtx, rows...)
}
func (a *application) content(gtx C, th *material.Theme, s viewState) D {
	title, subtitle, badge := "Put your GPU to work.", "Connect this Windows PC to the Petabyte compute marketplace.", "SETUP ASSISTANT"
	switch s.phase {
	case checking:
		title, subtitle = "Let’s check your PC.", "We’re checking the local requirements for the Windows agent."
	case review:
		title, subtitle = "You’re ready to connect.", "Review what changes on this PC, then sign in to your account."
	case signingIn:
		title, subtitle = "Finish linking your account.", "Your sign-in stays in your browser. Return here when you’re done."
	case installing:
		title, subtitle = "Setting up your agent.", "Keep this window open while the installer finishes."
	case complete:
		title, subtitle, badge = "Installation finished.", "Open your dashboard to confirm your GPU is online and review availability.", "NEXT: CHECK YOUR DASHBOARD"
	case failed:
		title, subtitle, badge = "Let’s get you unstuck.", "Follow the guidance below, then check this PC again.", "SETUP NEEDS ATTENTION"
	}
	if a.demo {
		badge = "PREVIEW MODE  ·  NO CHANGES TO YOUR PC"
	}
	return column(gtx,
		layout.Rigid(label(th, 11, badge, accent, true)), gap(16),
		layout.Rigid(label(th, 30, title, fg, true)), gap(10),
		layout.Rigid(label(th, 14, subtitle, muted, false)), gap(24),
		layout.Rigid(func(gtx C) D {
			return layout.Flex{}.Layout(gtx,
				layout.Flexed(1, func(gtx C) D {
					return card(gtx, panel, 18, func(gtx C) D {
						return column(gtx, layout.Rigid(label(th, 11, "NVIDIA GPU", muted, true)), gap(9), layout.Rigid(label(th, 15, s.gpu, fg, true)))
					})
				}),
				layout.Rigid(layout.Spacer{Width: 12}.Layout),
				layout.Flexed(1, func(gtx C) D {
					return card(gtx, panel, 18, func(gtx C) D {
						return column(gtx, layout.Rigid(label(th, 11, "IDLE MINING", muted, true)), gap(9), layout.Rigid(label(th, 15, "Disabled", green, true)))
					})
				}),
			)
		}), gap(14),
		layout.Rigid(func(gtx C) D {
			return card(gtx, panel, 20, func(gtx C) D {
				return column(gtx,
					layout.Rigid(label(th, 16, "What you’re setting up", fg, true)), gap(18),
					layout.Rigid(a.infoRow(th, "01", "A Windows + WSL 2 agent", "Uses Ubuntu 24.04, Docker and the Petabyte background service.")), gap(16),
					layout.Rigid(a.infoRow(th, "02", "Available for compute rentals", "The agent starts at Windows sign-in. Rentals use your GPU and electricity.")), gap(16),
					layout.Rigid(a.infoRow(th, "03", "Idle mining stays off", "Earnings depend on actual rentals. Setup does not guarantee income.")),
				)
			})
		}),
	)
}

// Keep status, consent and the next action visible when the details need scrolling.
func (a *application) actions(gtx C, th *material.Theme, s viewState) D {
	return column(gtx,
		layout.Rigid(func(gtx C) D {
			col := accent
			if s.phase == failed {
				col = amber
			}
			if s.phase == complete {
				col = green
			}
			return card(gtx, rgb(0x1B2939), 18, func(gtx C) D {
				children := []layout.FlexChild{layout.Rigid(label(th, 14, s.status, col, true)), gap(7), layout.Rigid(label(th, 13, s.detail, muted, false))}
				if s.phase == checking || s.phase == signingIn || s.phase == installing {
					children = append(children, gap(14), layout.Rigid(func(gtx C) D {
						gtx.Constraints.Min = image.Pt(gtx.Dp(20), gtx.Dp(20))
						gtx.Constraints.Max = gtx.Constraints.Min
						p := material.Loader(th)
						p.Color = accent
						return p.Layout(gtx)
					}))
				} else if s.phase == review {
					children = append(children, gap(10), layout.Rigid(label(th, 12, "WSL 2: "+s.wsl, green, false)))
				}
				return column(gtx, children...)
			})
		}), gap(16),
		layout.Rigid(func(gtx C) D {
			if s.phase != review {
				return D{}
			}
			return column(gtx,
				layout.Rigid(label(th, 12, "Installation restarts WSL and may interrupt other Linux work. Save that work first. Closing this app later does not stop the installed agent.", muted, false)), gap(10),
				layout.Rigid(func(gtx C) D {
					c := material.CheckBox(th, &a.consent, "I agree to install the agent and make this GPU available for rentals.")
					c.TextSize = 13
					return c.Layout(gtx)
				}), gap(12),
			)
		}),
		layout.Rigid(func(gtx C) D {
			caption, enabled := "Check this PC", true
			switch s.phase {
			case checking:
				caption, enabled = "Checking…", false
			case review:
				caption, enabled = "Sign in & install", a.consent.Value
			case signingIn:
				caption, enabled = "Reopen browser ↗", s.browserURL != ""
			case installing:
				caption, enabled = "Installing…", false
			case complete:
				caption = "Open dashboard ↗"
			case failed:
				caption = "Check again"
				if s.needsAdmin {
					caption = "Restart as administrator"
				}
			}
			return layout.Flex{Alignment: layout.Middle}.Layout(gtx,
				layout.Rigid(func(gtx C) D { return button(gtx, th, &a.primary, caption, true, enabled) }),
				layout.Rigid(layout.Spacer{Width: 12}.Layout),
				layout.Rigid(func(gtx C) D {
					if s.phase != checking && s.phase != signingIn && s.phase != review {
						return D{}
					}
					return button(gtx, th, &a.secondary, "Cancel", false, true)
				}),
			)
		}), gap(18),
		layout.Rigid(func(gtx C) D {
			return layout.Flex{Alignment: layout.Middle}.Layout(gtx,
				layout.Flexed(1, label(th, 11, "petabyte.market  ·  Windows 10 / 11 (64-bit)", muted, false)),
				layout.Rigid(func(gtx C) D {
					b := material.Button(th, &a.privacy, "Privacy ↗")
					b.Background = bg
					b.Color = muted
					b.TextSize = 11
					return b.Layout(gtx)
				}),
			)
		}),
	)
}
func (a *application) infoRow(th *material.Theme, number, title, detail string) layout.Widget {
	return func(gtx C) D {
		return layout.Flex{}.Layout(gtx,
			layout.Rigid(label(th, 12, number+"   ", accent, true)),
			layout.Flexed(1, func(gtx C) D {
				return column(gtx, layout.Rigid(label(th, 13, title, fg, true)), gap(4), layout.Rigid(label(th, 12, detail, muted, false)))
			}),
		)
	}
}
