# Wispr Future Ideas & Roadmap Backlog

## 1. 3D Audio-Reactive Sphere / Orb Visualizer (Alternative Overlay Mode)

### Concept
Replace or supplement the 2D pill overlay with a floating 3D orb/sphere that morphs and pulses dynamically to speech in real-time.

### Proposed Architecture & Tech Stack Options
* **Option A: Hardware-Accelerated Lightweight Native 3D (`wgpu` / Direct3D 12 / Vulkan)**
  * **How:** Offscreen render target with transparent swapchain or DirectComposition / `UpdateLayeredWindow`.
  * **Vertex Deformation:** Deform a tessellated sphere mesh using real-time FFT frequency bins and RMS volume.
  * **Fragment Shaders:** Fresnel glow, fluid noise (Simplex / Perlin noise), raymarched iridescent glass, or particle field.
  * **Memory / Performance:** ~10-15 MB RAM, 60-120 FPS at minimal GPU utilization (<1%).

* **Option B: WebGL / Three.js Layer (WebView2 / Ultralight)**
  * **How:** Embed an ultra-lightweight frameless transparent web view hosting Three.js or custom GLSL shaders (e.g. vertex shader displacement + blooming orb).
  * **Pros:** Rapid shader prototyping, CSS backdrop filters.
  * **Cons:** Higher baseline RAM (~60-90 MB for WebView2 runtime).

### Interactions & States
* **Idle:** Gentle breathing/floating oscillation.
* **Recording (Audio Reactive):** Surface ripples, spikes, and color shifting proportional to voice frequencies and loudness.
* **Processing:** Fast spinning vortex / energetic particle swirling.
* **Done / Disperse:** Smooth particle dispersion or scale-down exit animation.
