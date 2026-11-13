# xterm.js 6.0.0 + addon-fit 0.11.0 (vendored)

- Source: npm packages `@xterm/xterm@6.0.0` (`lib/xterm.mjs`, `css/xterm.css`, `LICENSE`)
  and `@xterm/addon-fit@0.11.0` (`lib/addon-fit.mjs`, `LICENSE` → `LICENSE-addon-fit`),
  downloaded from registry.npmjs.org on 2026-11-13 (both published 2025-12-22).
- License: MIT (see `LICENSE`, `LICENSE-addon-fit`), Copyright (c) The xterm.js authors,
  SourceLair Private Company, Christopher Jeffrey.
- Project: https://github.com/xtermjs/xterm.js
- SHA-256:
  - `xterm.mjs`     `b336ec65a086c056d4804b3d4c2347da5663d3f23c3f25be866467bd8857ad59`
  - `xterm.css`     `854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6`
  - `addon-fit.mjs` `2d87e1bddc73be9111de8beee5370c3bb7aac9c94e18e6f245f02ca741ef1769`
- Unmodified (the `sourceMappingURL` comments point to maps that are not vendored).
  xterm.js creates `<style>` elements at runtime, so only the `/terminal` page allows
  inline styles (ADR 0005). To upgrade: replace the files, update this README and the
  paths in `js/terminal/tab.js`; `tests/ui_web/test_static.py` checks the hashes.
