# Doom tab: how it runs locally, and what to check on ctmphone.online

This is the working configuration from the local dev machine, where the Doom tab renders correctly. Compare each section against production starting from the top. The first difference you find is usually the cause.

## 1. The embed URL

`templates/phone.html`, tab bar:

```html
<button class="tab tab-embed" data-embed="doom"
        data-src="https://ustymukhman.github.io/WebDOOM/public/">&#128126; Doom</button>
```

Notes:

- The path ends in `/public/`. `https://ustymukhman.github.io/WebDOOM/` (without `/public/`) is a 404. The page that serves the game is the `/public/` directory.
- Do **not** use `https://silentspacemarine.com`. That page renders a fixed 930px-wide, width-derived ~4:3 CRT inside its own `overflow: hidden` container. Its HUD is clipped below the visible area no matter how tall your iframe is. That was the original "heavily cropped" bug.
- This host sends no `X-Frame-Options` and no restrictive `Content-Security-Policy`, so it can be framed.

## 2. The iframe element

Built in the tab loader in `templates/phone.html`:

```html
<iframe class="embed-frame"
        data-embed="doom"
        src="https://ustymukhman.github.io/WebDOOM/public/"
        title="doom"
        allow="fullscreen; gamepad; autoplay; clipboard-write; pointer-lock; accelerometer; gyroscope; web-share"
        allowfullscreen></iframe>
```

`allowfullscreen` is present. `pointer-lock` is required for mouse-look. If either is missing, the game may load but the mouse will not capture.

## 3. The frame must fill its container

`static/style.css`:

```css
.embed-frame {
  display: block;
  width: 100%;
  flex: 1 1 auto;
  height: 100%;
  min-height: 0;      /* important */
  border: none;
  background: #0b0f14;
}

.data-content.is-embed {
  padding: 0;
  overflow: hidden;
  background: #0b0f14;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
```

Why `min-height: 0` matters: this iframe lives inside a flex column. The default `min-height: auto` on a flex item refuses to shrink below its content, so a tall game pushes the bottom of the frame below the visible panel and the parent clips it. `min-height: 0` lets the frame adopt exactly the leftover height.

There is **no** `aspect-ratio` or `max-width` rule on the Doom frame locally. An earlier version constrained it to 4:3 and centered it. That letterboxed the game and looked worse. If your production CSS still has an `.embed-frame[data-embed="doom"]` block, delete it.

## 4. Response headers from the app

`app.py`, near the top:

```python
@app.after_request
def allow_embedded_webgl(response):
    response.headers.setdefault(
        "Permissions-Policy",
        "fullscreen=*, gamepad=*, autoplay=*, clipboard-write=*, "
        "accelerometer=*, gyroscope=*",
    )
    return response
```

Verify it is actually reaching the browser on production:

```bash
curl -sI https://ctmphone.online/ | grep -i permissions-policy
```

A `Permissions-Policy` header that omits `fullscreen` or `gamepad` will break the embed even though the frame loads.

Also check that no other middleware or proxy is adding a conflicting `Content-Security-Policy` with a restrictive `frame-src` or `child-src`. A CSP that does not list `https://ustymukhman.github.io` will silently block the iframe. This is the single most common cause of a blank game panel on a public host.

```bash
curl -sI https://ctmphone.online/ | grep -i -E 'content-security-policy|x-frame-options'
```

The app itself does **not** set `X-Frame-Options`. If a host does (Cloudflare, a reverse proxy, some PaaS), it applies to your own page, not the third-party iframe, so it is usually harmless here. Confirm anyway.

## 5. HTTPS differences that matter

Locally the app runs on `http://localhost:8080`. Production is `https://ctmphone.online`.

- `localhost` is a **secure context** even over plain HTTP, so WebGL, pointer lock, and fullscreen all work without a certificate.
- A public site must be HTTPS for the same features. Confirm there is no mixed content and that the certificate is valid.
- Doom does not need WebGL to render 2D here, but the pointer lock and fullscreen APIs are gated on a secure context. If the game draws but the mouse does not capture, check the padlock and the page protocol.

## 6. Third-party host reachability

If the iframe shows nothing at all, check whether the game host is reachable from production and from your network:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://ustymukhman.github.io/WebDOOM/public/
```

Expect `200`. If your production network, a corporate proxy, or a content blocker filters `github.io`, the frame will be blank while local works behind a different network. Test the URL in a browser on the same machine and network as the app's users.

## 7. Browser-side checks

Open the Doom tab on production, then look at DevTools:

- **Console**: `Refused to frame ... because it violates Content-Security-Policy` means a CSP is blocking it. See section 4.
- **Console**: `The page does not have a permission policy allowing this feature` means `fullscreen` or `pointer-lock` is missing from `allow` or `Permissions-Policy`. See sections 2 and 4.
- **Network**: a request to `ustymukhman.github.io/WebDOOM/public/` that returns `200` but never renders means CSS sizing is the problem. See section 3.
- **Elements**: select `.embed-frame` and confirm its rendered height is greater than zero and it is not `display: none`. A zero-height frame is a flex sizing bug, not a network bug.

## 8. Fastest triage

Work down this list in order. Stop at the first failure.

1. `curl -sI https://ctmphone.online/ | grep -i content-security-policy`. If it exists and does not allow `frame-src https://ustymukhman.github.io`, that is the bug.
2. Load `https://ustymukhman.github.io/WebDOOM/public/` directly in a new tab. If it does not play, the problem is network or the host, not your app.
3. Compare the tab's `data-src` to section 1. A stale `silentspacemarine.com` will look cropped but never blank.
4. Re-check `.embed-frame` for leftover `aspect-ratio` or `max-width` rules from the earlier 4:3 experiment.
5. Confirm `min-height: 0` is present. Without it, a wide short panel clips the frame bottom.

## What is different from the Retro and Pac-Man tabs

- Retro is **same-origin**: `/static/retro.html`. It drives a canvas directly and needs no iframe permissions. Hosted emulators such as EmulatorJS and Webretro failed inside a cross-origin frame with `Cannot read properties of undefined (reading 'GLctx')` because their threading path could not create a WebGL context in the iframe. That is why Retro is local.
- Pac-Man and Breakout are third-party iframes like Doom and rely on the same `embed-frame` sizing rules. If one of them works on production and Doom does not, the difference is the specific host, not your CSS.