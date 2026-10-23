# Emporium Print Studio

A Mac app for custom wallpaper: browse your high-res designs, set up a wall, edit (flip, tile, colour, HSL colour ranges),
make a client proof PDF, and produce the print-ready TIFF. Everything runs on your own computer.
Your original design files are only ever read, never changed.

## Install (Mac with Apple Silicon: M1, M2, M3, M4)

1. Open the **Releases** page of this repository and download `Emporium-Print-Studio-mac-arm64.zip` from the **Latest build**.
2. Unzip it and drag **Emporium Print Studio** into your Applications folder.
3. The first time only: right-click the app, choose **Open**, then **Open** again. (The app is not signed with a paid Apple
   developer account, so macOS asks once. If macOS says the app is "damaged", open Terminal and run
   `xattr -cr "/Applications/Emporium Print Studio.app"`, then try again.)
4. The app asks you to choose your design folder. Sub-folders become categories. A small preview of every design is made for
   browsing; the first run takes a while if you have thousands of files, and you can start browsing while it works.

## Everyday use

- **Browse and filter** by category (your top-level folders), orientation, style, colour, or search.
- **Edit details** of a design (pencil icon on a card): name, material/code, style tags, colours, notes.
  **Select** lets you tag many designs at once.
- Pick a design, enter the wall size, adjust, make a **Proof PDF** (opens in Preview), tick "Client approved", **Send to print**.
  The TIFF and proof are saved in your print folder (default: Documents/Emporium Print Files), with a job sheet.
- **Texture** (panel on the right): choose a texture folder in Settings, then pick a texture. It is placed under the artwork
  and blended with **Multiply**. Set its size on the wall, **Repeat** or **Mirror** tiling, and opacity 0-100
  (100 = full strength, 0 = none). It shows in the preview, the proof and the print file, and is listed on the job sheet.
- **Resolution**: 144 dpi, 96 dpi, or **Image default**, which prints at the artwork's own resolution at the chosen size
  (no stretching or squeezing of pixels).
- **Settings** (gear icon): design folders, texture folder, print folder, office network sharing, logos for proofs.

New files you add to your design folders are found the next time the app starts, or press **Rescan now**.
If you rename or move a file, the app recognises it and keeps its tags.

## Sharing with your team on the office network

In Settings, set a PIN (at least 4 characters) and switch on **Share on my office network**. Your team opens the address
shown there in a browser, types the PIN, and can browse, make proofs and send jobs. They cannot change settings or tags.
Your Mac must be on with the app open. macOS may ask whether to allow incoming connections: choose Allow.
Sharing is off by default and is refused until a PIN is set.

## Where things are kept

| What | Where |
| --- | --- |
| Your designs | wherever you keep them (never modified) |
| Catalogue, your tags, thumbnails, settings, logos | `~/Library/Application Support/Emporium` (Settings > About shows it) |
| Proofs, print files, job records | your print folder |

Back up the print folder and the `catalog.db` file (it holds the tags you typed in). The thumbnails are rebuilt automatically.

## Emailing the printer a download link (optional)

Off by default. It needs a public web address for the link, so it does not fit an app that only runs on your Mac.
If you later want it, see `docs/HOSTING-online-later.md` and the `email` and `public_url` settings in `config.json`
(in the data folder).

## For developers

- `app/` holds the engine (`server.py`, `library.py`, `core.py`, `render.py`, `share.py`), the screen (`app/web/index.html`)
  and the app window (`desktop.py`).
- Run the checks: `pip install -r requirements.txt && python app/selftest.py`
- Run without the window: `python app/desktop.py --headless` prints a link to open in a browser.
- The Mac app is built by GitHub Actions (`.github/workflows/build.yml`) on every push to `main`.
- Never commit designs, `config.json`, secrets or print files. `.gitignore` blocks the common ones. The repository is public.
