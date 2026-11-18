# Fast Windows UI Automation Guardrails

- Keep `include_image_content=False` on `take_screenshot` (`weightless-windows-vision`); rely on the low-res `0.5x` overview default for screen scans, and follow up immediately with `take_screenshot(region=[x, y, w, h])` (`1.0x` crisp PNG) or `scale=1.0` whenever small text or details are unreadable.
- Check `focus_window`'s returned `app_memory` first and save verified coordinates, hotkeys, and UI quirks via `update_app_memory` so future sessions skip rediscovery.
- Prefer `read_text_from_screen(region=...)` over `take_screenshot` + `read_file` for text, price, score, or button-state verification.
- Batch multi-target UI sequences (`batch_clicks`) and pair deterministic transitions with `settle_seconds` + `screenshot_after=True` (using `region=...` / `show_grid=False` / `image_format="jpeg"` when coordinates are already mapped).
- Issue `show_status_bubble` in parallel with action calls, and skip redundant `list_windows` / `focus_window` calls when the target window is already active.
- After an emergency `3x Esc` (`WH_KEYBOARD_LL`, 1.8s) abort, stop immediately and only pass `reset_abort=True` to `show_status_bubble(status_type="intent")` on an explicit new user instruction.
