use std::f32::consts::PI;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tiny_skia::{Color, Paint, Path, PathBuilder, Pixmap, Transform};
use windows::core::w;
use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, POINT, SIZE, WPARAM};
use windows::Win32::Graphics::Gdi::{
    CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC, ReleaseDC, SelectObject,
    BITMAPINFO, BITMAPINFOHEADER, BI_RGB, BLENDFUNCTION, DIB_RGB_COLORS,
};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DispatchMessageW, GetSystemMetrics, PeekMessageW,
    RegisterClassExW, ShowWindow, TranslateMessage, UpdateLayeredWindow,
    MSG, PM_REMOVE, SM_CXSCREEN, SM_CYSCREEN, SW_HIDE, SW_SHOWNOACTIVATE, ULW_ALPHA, WNDCLASSEXW,
    WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP,
};
use crate::audio::RmsMeter;

pub const OVERLAY_W: u32 = 120;
pub const OVERLAY_H: u32 = 34;
pub const TASKBAR_CLEAR: i32 = 24;

const NUM_DOTS: usize = 16;
const DOT_R: f32 = 2.0;
const DOT_MAX_H: f32 = 14.0;
const DOT_GAP: f32 = 1.5;

const SPINNER_DOTS: usize = 8;
const SPINNER_R: f32 = 9.0;
const SPINNER_DOT_R: f32 = 2.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum OverlayMode {
    Idle = 0,
    Recording = 1,
    Processing = 2,
}

pub struct OverlayManager {
    mode_atomic: Arc<AtomicU8>,
    is_running: Arc<AtomicBool>,
}

impl OverlayManager {
    pub fn new(rms_meter: RmsMeter) -> Self {
        let mode_atomic = Arc::new(AtomicU8::new(OverlayMode::Idle as u8));
        let is_running = Arc::new(AtomicBool::new(true));

        let mode_clone = mode_atomic.clone();
        let running_clone = is_running.clone();

        std::thread::spawn(move || {
            run_overlay_loop(mode_clone, running_clone, rms_meter);
        });

        Self {
            mode_atomic,
            is_running,
        }
    }

    pub fn set_mode(&self, mode: OverlayMode) {
        self.mode_atomic.store(mode as u8, Ordering::SeqCst);
    }

    pub fn stop(&self) {
        self.is_running.store(false, Ordering::SeqCst);
    }
}

fn make_rounded_rect(x: f32, y: f32, w: f32, h: f32, r: f32) -> Option<Path> {
    let mut pb = PathBuilder::new();
    let r = r.min(w / 2.0).min(h / 2.0);
    pb.move_to(x + r, y);
    pb.line_to(x + w - r, y);
    pb.quad_to(x + w, y, x + w, y + r);
    pb.line_to(x + w, y + h - r);
    pb.quad_to(x + w, y + h, x + w - r, y + h);
    pb.line_to(x + r, y + h);
    pb.quad_to(x, y + h, x, y + h - r);
    pb.line_to(x, y + r);
    pb.quad_to(x, y, x + r, y);
    pb.close();
    pb.finish()
}

fn make_circle(cx: f32, cy: f32, r: f32) -> Option<Path> {
    make_rounded_rect(cx - r, cy - r, r * 2.0, r * 2.0, r)
}

/// Per-dot wobble in ~0.1..1.0, varying over time. Two incommensurate sines so
/// the dots don't read as one traveling wave.
fn dot_wobble(i: usize, phase: f32) -> f32 {
    let fi = i as f32;
    0.55 + 0.28 * (phase * 1.9 + fi * 0.7).sin() + 0.17 * (phase * 3.1 + fi * 1.9).sin()
}

struct OverlayRenderer {
    dot_h: [f32; NUM_DOTS],
    dot_env: [f32; NUM_DOTS],
    dot_xs: [f32; NUM_DOTS],
    wobble_phase: f32,
    spinner_angle: f32,
    t_frac: f32,
    pixmap: Pixmap,
}

impl OverlayRenderer {
    fn new() -> Self {
        let mut dot_env = [0.0; NUM_DOTS];
        let mut dot_xs = [0.0; NUM_DOTS];

        let total_w = (NUM_DOTS as f32) * (DOT_R * 2.0) + ((NUM_DOTS - 1) as f32) * DOT_GAP;
        let start_x = ((OVERLAY_W as f32) - total_w) / 2.0;

        for i in 0..NUM_DOTS {
            let frac = (i as f32) / ((NUM_DOTS - 1) as f32);
            let pos = -PI / 2.0 + frac * PI;
            dot_env[i] = 0.25 + 0.75 * pos.cos().powi(2);
            dot_xs[i] = start_x + (i as f32) * (DOT_R * 2.0 + DOT_GAP) + DOT_R;
        }

        Self {
            dot_h: [DOT_R; NUM_DOTS],
            dot_env,
            dot_xs,
            wobble_phase: 0.0,
            spinner_angle: 0.0,
            t_frac: 1.0,
            pixmap: Pixmap::new(OVERLAY_W, OVERLAY_H).unwrap(),
        }
    }

    fn update_and_render(&mut self, mode: OverlayMode, audio_rms: f32) -> &[u8] {
        self.pixmap.fill(Color::TRANSPARENT);

        // 1. Draw Pill Background (Dark Translucent 90% black)
        let mut bg_paint = Paint::default();
        bg_paint.set_color(Color::from_rgba8(0, 0, 0, 230));
        bg_paint.anti_alias = true;

        if let Some(path) = make_rounded_rect(0.0, 0.0, OVERLAY_W as f32, OVERLAY_H as f32, (OVERLAY_H as f32) / 2.0) {
            self.pixmap.fill_path(&path, &bg_paint, tiny_skia::FillRule::Winding, Transform::identity(), None);
        }

        let mid_y = (OVERLAY_H as f32) / 2.0;

        match mode {
            OverlayMode::Recording => {
                self.t_frac = 0.0;
                let level = (audio_rms * 14.0).min(1.0);
                self.wobble_phase = (self.wobble_phase + 0.16) % (2.0 * PI * 100.0);

                // Update dot heights
                for i in 0..NUM_DOTS {
                    let target = DOT_R
                        + (DOT_MAX_H - DOT_R) * self.dot_env[i] * level * dot_wobble(i, self.wobble_phase);
                    self.dot_h[i] += (target - self.dot_h[i]) * 0.55;
                }

                // Render 16 indigo (#818cf8) capsules
                let mut dot_paint = Paint::default();
                dot_paint.set_color(Color::from_rgba8(0x81, 0x8c, 0xf8, 255));
                dot_paint.anti_alias = true;

                for i in 0..NUM_DOTS {
                    let cx = self.dot_xs[i];
                    let h = self.dot_h[i].max(DOT_R);
                    if let Some(path) = make_rounded_rect(cx - DOT_R, mid_y - h, DOT_R * 2.0, h * 2.0, DOT_R) {
                        self.pixmap.fill_path(&path, &dot_paint, tiny_skia::FillRule::Winding, Transform::identity(), None);
                    }
                }
            }
            OverlayMode::Processing => {
                self.spinner_angle -= 0.25;
                if self.t_frac < 1.0 {
                    self.t_frac = (self.t_frac + 0.12).min(1.0);
                    for i in 0..NUM_DOTS {
                        self.dot_h[i] += (DOT_R - self.dot_h[i]) * 0.45;
                    }
                }

                let spin_alpha = (255.0 * self.t_frac) as u8;

                // Render 8 orbital orange spinner dots
                let spin_cx = (OVERLAY_W as f32) / 2.0;
                let spin_cy = mid_y;

                for j in 0..SPINNER_DOTS {
                    let angle = self.spinner_angle + (j as f32) * (2.0 * PI / (SPINNER_DOTS as f32));
                    let px = spin_cx + angle.cos() * SPINNER_R;
                    let py = spin_cy + angle.sin() * SPINNER_R;

                    let frac = (j as f32) / (SPINNER_DOTS as f32);
                    let r = (0xFB as f32 * (1.0 - frac) + 0x20 as f32 * frac) as u8;
                    let g = (0x92 as f32 * (1.0 - frac) + 0x10 as f32 * frac) as u8;
                    let b = (0x3C as f32 * (1.0 - frac) + 0x08 as f32 * frac) as u8;

                    let dot_rad = (SPINNER_DOT_R - (j as f32) * 0.18).max(1.0);

                    let mut spin_paint = Paint::default();
                    spin_paint.set_color(Color::from_rgba8(r, g, b, spin_alpha));
                    spin_paint.anti_alias = true;

                    if let Some(path) = make_circle(px, py, dot_rad) {
                        self.pixmap.fill_path(&path, &spin_paint, tiny_skia::FillRule::Winding, Transform::identity(), None);
                    }
                }

                // Cross-fade fading out dots if transitioning
                if self.t_frac < 1.0 {
                    let dot_alpha = (255.0 * (1.0 - self.t_frac)) as u8;
                    let mut dot_paint = Paint::default();
                    dot_paint.set_color(Color::from_rgba8(0x81, 0x8c, 0xf8, dot_alpha));
                    dot_paint.anti_alias = true;

                    for i in 0..NUM_DOTS {
                        let cx = self.dot_xs[i];
                        let h = self.dot_h[i].max(DOT_R);
                        if let Some(path) = make_rounded_rect(cx - DOT_R, mid_y - h, DOT_R * 2.0, h * 2.0, DOT_R) {
                            self.pixmap.fill_path(&path, &dot_paint, tiny_skia::FillRule::Winding, Transform::identity(), None);
                        }
                    }
                }
            }
            OverlayMode::Idle => {
                for i in 0..NUM_DOTS {
                    self.dot_h[i] += (DOT_R - self.dot_h[i]) * 0.3;
                }
            }
        }

        self.pixmap.data()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wobble_is_bounded_and_moves() {
        // Stays in a sane multiplier range for every dot at every phase.
        let mut per_dot_min = [f32::MAX; NUM_DOTS];
        let mut per_dot_max = [f32::MIN; NUM_DOTS];
        for step in 0..2000 {
            let phase = step as f32 * 0.16;
            for i in 0..NUM_DOTS {
                let w = dot_wobble(i, phase);
                assert!((0.0..=1.05).contains(&w), "dot {i} phase {phase} -> {w}");
                per_dot_min[i] = per_dot_min[i].min(w);
                per_dot_max[i] = per_dot_max[i].max(w);
            }
        }
        // Every dot must actually animate (the old frozen sawtooth would fail this).
        for i in 0..NUM_DOTS {
            assert!(per_dot_max[i] - per_dot_min[i] > 0.5, "dot {i} barely moves");
        }
        // No dot is systematically taller than its neighbours: the static
        // (i * 17) % 100 ramp used to bake in a peak at dots 5 and 11.
        let means: Vec<f32> = (0..NUM_DOTS)
            .map(|i| {
                (0..2000).map(|s| dot_wobble(i, s as f32 * 0.16)).sum::<f32>() / 2000.0
            })
            .collect();
        let lo = means.iter().cloned().fold(f32::MAX, f32::min);
        let hi = means.iter().cloned().fold(f32::MIN, f32::max);
        assert!(hi - lo < 0.05, "uneven long-run heights: {means:?}");
    }
}

unsafe extern "system" fn overlay_wnd_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    DefWindowProcW(hwnd, msg, wparam, lparam)
}

fn run_overlay_loop(
    mode_atomic: Arc<AtomicU8>,
    is_running: Arc<AtomicBool>,
    rms_meter: RmsMeter,
) {
    unsafe {
        let class_name = w!("WisprOverlayClass");
        let hinstance = GetModuleHandleW(None).unwrap();

        let wnd_class = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            lpfnWndProc: Some(overlay_wnd_proc),
            hInstance: hinstance.into(),
            lpszClassName: class_name,
            ..Default::default()
        };

        RegisterClassExW(&wnd_class);

        let screen_w = GetSystemMetrics(SM_CXSCREEN);
        let screen_h = GetSystemMetrics(SM_CYSCREEN);

        let x = (screen_w - OVERLAY_W as i32) / 2;
        let target_y = screen_h - OVERLAY_H as i32 - TASKBAR_CLEAR;
        let mut current_y = screen_h as f32;

        let hwnd = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            class_name,
            w!("WisprOverlay"),
            WS_POPUP,
            x,
            screen_h,
            OVERLAY_W as i32,
            OVERLAY_H as i32,
            HWND::default(),
            None,
            hinstance,
            None,
        ).unwrap();

        let screen_dc = GetDC(HWND::default());
        let mem_dc = CreateCompatibleDC(screen_dc);

        let bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: OVERLAY_W as i32,
                biHeight: -(OVERLAY_H as i32),
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };

        let mut dib_bits: *mut u8 = std::ptr::null_mut();
        let hbitmap = CreateDIBSection(
            screen_dc,
            &bmi,
            DIB_RGB_COLORS,
            &mut dib_bits as *mut _ as *mut *mut _,
            windows::Win32::Foundation::HANDLE::default(),
            0,
        ).unwrap();

        let old_bmp = SelectObject(mem_dc, hbitmap);

        let mut renderer = OverlayRenderer::new();
        let mut is_visible = false;

        let mut bgra_buffer = vec![0u8; (OVERLAY_W * OVERLAY_H * 4) as usize];
        let target_frame_duration = Duration::from_micros(16_667);

        while is_running.load(Ordering::Relaxed) {
            let frame_start = Instant::now();

            let mut msg = MSG::default();
            while PeekMessageW(&mut msg, HWND::default(), 0, 0, PM_REMOVE).as_bool() {
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }

            let mode_val = mode_atomic.load(Ordering::Relaxed);
            let mode = match mode_val {
                1 => OverlayMode::Recording,
                2 => OverlayMode::Processing,
                _ => OverlayMode::Idle,
            };

            let rms = rms_meter.get_level();

            if mode != OverlayMode::Idle {
                if !is_visible {
                    is_visible = true;
                    current_y = screen_h as f32;
                    let _ = ShowWindow(hwnd, SW_SHOWNOACTIVATE);
                }
                let dist = (target_y as f32) - current_y;
                current_y += dist * 0.28;
            } else if is_visible {
                let dist = (screen_h as f32) - current_y;
                current_y += dist * 0.32;
                if (screen_h as f32 - current_y).abs() < 1.5 {
                    is_visible = false;
                    let _ = ShowWindow(hwnd, SW_HIDE);
                }
            }

            if is_visible {
                let rgba_data = renderer.update_and_render(mode, rms);

                for (src, dst) in rgba_data.chunks_exact(4).zip(bgra_buffer.chunks_exact_mut(4)) {
                    let r = src[0];
                    let g = src[1];
                    let b = src[2];
                    let a = src[3];

                    dst[0] = ((b as u32 * a as u32) / 255) as u8;
                    dst[1] = ((g as u32 * a as u32) / 255) as u8;
                    dst[2] = ((r as u32 * a as u32) / 255) as u8;
                    dst[3] = a;
                }

                if !dib_bits.is_null() {
                    std::ptr::copy_nonoverlapping(bgra_buffer.as_ptr(), dib_bits, bgra_buffer.len());
                }

                let pt_dst = POINT {
                    x,
                    y: current_y as i32,
                };
                let size = SIZE {
                    cx: OVERLAY_W as i32,
                    cy: OVERLAY_H as i32,
                };
                let pt_src = POINT { x: 0, y: 0 };

                let blend = BLENDFUNCTION {
                    BlendOp: 0,
                    BlendFlags: 0,
                    SourceConstantAlpha: 255,
                    AlphaFormat: 1,
                };

                let _ = UpdateLayeredWindow(
                    hwnd,
                    screen_dc,
                    Some(&pt_dst),
                    Some(&size),
                    mem_dc,
                    Some(&pt_src),
                    COLORREF(0),
                    Some(&blend),
                    ULW_ALPHA,
                );
            }

            let elapsed = frame_start.elapsed();
            if elapsed < target_frame_duration {
                std::thread::sleep(target_frame_duration - elapsed);
            }
        }

        SelectObject(mem_dc, old_bmp);
        let _ = DeleteObject(hbitmap);
        let _ = DeleteDC(mem_dc);
        ReleaseDC(HWND::default(), screen_dc);
    }
}
