# Front-end third-party dependency

- Library: markdown-it 15.0.2, UMD browser build.
- Upstream: https://github.com/markdown-it/markdown-it
- Package: `npm pack markdown-it@15.0.2 --ignore-scripts`.
- Source in the package: `dist/browser/markdown-it.umd.min.js`.
- Local filename: `markdown-it.min.js`.
- File SHA-256: `635972b985228e8af9f0143647c68616b7a3bb09f6946e7e4a52e43dcf5e7be5`.
- License: MIT; original notice in `markdown-it.LICENSE`.

The browser loads this bundled file from the local Python service. No CDN or
Node.js installation is needed at runtime. `markdown.js` disables raw HTML,
uses markdown-it's default link validation, and displays model metadata as text.
