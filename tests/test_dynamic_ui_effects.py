"""Shared 3D motion remains consistent, dynamic, and accessibility-safe."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shared_surfaces_receive_depth_and_pointer_driven_highlight():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "--depth-rest:" in styles
    assert "--depth-hover:" in styles
    assert ".card, .stat, .dry-run-hero, .section-title, .trend-box" in styles
    assert "rotateX(var(--tilt-x)) rotateY(var(--tilt-y))" in styles
    assert "radial-gradient(circle at var(--surface-x) var(--surface-y)" in styles
    assert "function initDynamicSurfaces()" in script
    assert "const surfaceSelector = '.card, .stat, .dry-run-hero, .section-title, .trend-box'" in script
    assert "surface.style.setProperty('--surface-x'" in script
    assert "initDynamicSurfaces();" in script


def test_controls_have_pressed_feedback_and_delegated_ripple():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert ".btn:active:not(:disabled)" in styles
    assert "@keyframes surface-ripple" in styles
    assert "event.target.closest?.('.btn, .nav a, .theme-swatch')" in script
    assert "ripple.addEventListener('animationend'" in script


def test_motion_layer_respects_reduced_motion_and_coarse_pointers():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "@media (max-width: 760px), (hover: none), (pointer: coarse)" in styles
    assert "(prefers-reduced-motion: reduce)" in script
    assert "(hover: hover) and (pointer: fine)" in script
