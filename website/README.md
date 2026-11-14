# API Autopilot website

The product page. Plain HTML, CSS and a little JavaScript: there is no build
step and nothing to install.

## Preview

Open `index.html` in a browser, or serve the folder:

```bash
cd website
python3 -m http.server 8080    # then open http://localhost:8080
```

## Hosting

Live at **https://api-autopilot.vercel.app**, deployed on Vercel from this
folder (Root Directory: `website`, Framework: Other, no build command).

Any static host works. Because the site lives in a subfolder, tell the host
where it is:

| Host | Setting |
|---|---|
| Vercel | Root Directory: `website`. Framework: Other. No build command. |
| Netlify | Base directory: `website`. Publish directory: `website`. No build command. |
| Cloudflare Pages | Root directory: `website`. Build output directory: `/`. No build command. |
| GitHub Pages | Serves only the repo root or `/docs`, so deploy with a GitHub Action that uploads `website/`. |

To move the site to its own repository later, copy this folder. Nothing in it
depends on the rest of the project.

## Files

- `index.html`: all content. Icons are embedded at the top of `<body>`.
- `styles.css`: design. Colours are the variables at the top of the file.
- `script.js`: mobile menu, the copy button and scroll fade-ins. The page
  works without it.
- `favicon.svg`: browser tab icon (the logo on a dark rounded square).
- `logo.svg` / `logo-on-light.svg`: the logo for dark and light backgrounds.
  The mark is a slash (an API path) with an orange dot (the point at its end);
  the wordmark is `api/autopilot`, with `api/` in Geist Mono, dimmed.

## Colours

The site uses one palette, Signal: warm near-black `#181716`, warm off-white
`#EAE2D3` and signal orange `#FF6229`. All colours are variables at the top of
`styles.css` (`--bg`, `--fg`, `--accent` and so on), so changing the palette
only means editing that block, plus the `theme-color` tag in `index.html`
(the browser bar colour on phones).

## Style notes

The site should read as a developer tool: Geist for text, Geist Mono for
anything code-like, section labels written as code comments (`// how it works`),
and real CLI output wherever possible. The hero terminal is shortened output
from an actual demo run with the Groq planner; keep it truthful if the CLI's
output changes.

Layout references: entire.com, supabase.com and getdedica.com. From them:
a two-line headline with the description beside it and real product output on
the first screen; bento grids with small product visuals instead of rows of
identical cards; section headings as one sentence with a dimmed second half
(about 36px, weight 500); thin frame lines down the content edges; and a
side-by-side "typical test vs Autopilot" comparison for non-technical readers.
No fake logos, testimonials or numbers: every figure comes from a real run.

## Keeping it accurate

- The commands under "Get started" and the init output in "Setup" are real; if
  the CLI's commands or output change, update them here too.
- The canonical address (`https://api-autopilot.vercel.app/`) is in the
  `<head>` of `index.html`; change it there if the site moves.
- Fonts load from Google Fonts. To avoid calling Google from visitors'
  browsers, download the two families and serve them from this folder.

## Credits

- Icons: [Phosphor Icons](https://phosphoricons.com), MIT licence.
- Fonts: Geist and Geist Mono, SIL Open Font Licence.
