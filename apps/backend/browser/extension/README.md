# TARS Browser Bridge

Lets TARS see and control your **normal, already-signed-in Chrome** instead
of only its own separate automation browser. Optional -- if you never
install this, TARS keeps using the separate automation Chrome exactly as
before (see `browser/session.py`).

## One-time setup (a few minutes)

1. Start the TARS backend once. On first run it writes a random pairing
   token to:
   ```
   %LOCALAPPDATA%\TARS\browser_bridge\token.txt
   ```
   Open that file in Notepad and copy its contents (one long string).

2. In Chrome, go to `chrome://extensions`, turn on **Developer mode**
   (top-right toggle), click **Load unpacked**, and select this folder
   (`apps/backend/browser/extension`).

3. Click the new TARS Browser Bridge icon in Chrome's toolbar (or right-click
   it and choose **Options**). Paste the token from step 1 into the
   **Pairing token** field, leave the port at `17722` unless you changed it,
   and click **Pair & connect**.

4. That's it. The extension reconnects automatically every time Chrome
   starts, using the token you already saved -- you do not need to repeat
   this unless you remove the extension or delete the token file.

## What it can and can't do

- It only acts when TARS's backend sends it one of a fixed, named list of
  commands (list tabs, navigate, click/type a described element, read
  text/links/tables, download) -- see `background.js`'s `ACTIONS` table.
  There is no "run arbitrary code" command.
- It only talks to `127.0.0.1` (your own machine) and only to the TARS
  backend that already knows your pairing token -- no website's page can
  reach it (see `bridge_server.py`'s module docstring for exactly why).
- It does not read or send your passwords, cookies, or browsing history to
  anything. It reads a page's visible content/structure only when TARS
  explicitly asks, for the one action you requested.
- Consequential actions (purchases, payments, sending messages, deleting
  data) still go through TARS's existing confirmation system regardless of
  which browser executes them.

## Uninstalling / disabling

Remove the extension from `chrome://extensions`, or just don't pair it --
either way, TARS falls back to its own separate automation Chrome
automatically, with no other change needed.
