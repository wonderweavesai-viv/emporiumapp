# Hosting guide: from free testing to a proper setup

Phones need to reach the app over the internet, over HTTPS, and the printer needs to be able to open the
download links in the emails. There are two stages.

## Stage 1: free, for building and testing (your Mac is the server)

Your masters stay on your Mac, nothing is uploaded anywhere, and it costs nothing. **Tailscale Funnel** gives
your Mac a permanent public HTTPS address (it ends in `.ts.net`) without a domain name or router settings.
Tailscale says Funnel is available on all plans and is still marked beta. Check tailscale.com for their current
free-plan terms when you sign up.

### What you need
- Your Mac, switched on, awake and online whenever the sales team or the printer use the app.
- A Tailscale account (free to create at tailscale.com).
- A Gmail address to send the printer emails from.

### Steps
1. **Install the app.** Follow README.md: install Python, run `python3 -m pip install -r requirements.txt`,
   put your artworks in `library/masters/`, then run `python3 server.py scan` once. With 1000+ large files this
   first scan takes a while. You can stop it (Ctrl+C) and start it again, and it carries on where it stopped.
2. **Install Tailscale** on the Mac from tailscale.com/download, open it and sign in.
3. **Switch on HTTPS.** In a browser go to login.tailscale.com/admin/dns. Make sure *MagicDNS* is on, then
   press *Enable HTTPS*.
4. **Start the app** in Terminal: `python3 server.py` (leave this window open).
5. **Publish it.** Open a second Terminal window and run: `tailscale funnel --bg 8080`
   (If the command is not found, use `/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --bg 8080`.)
   It prints your address, like `https://your-mac.your-tailnet.ts.net`. The first time, it may ask you to
   approve Funnel in your browser. A new address can take up to 10 minutes to start working.
6. **Make a Gmail app password** for the printer emails. Google Account, then Security, switch on 2-Step
   Verification, then search for "App passwords" and create one called Emporium. You get 16 letters.
7. **Create `config.json`.** Copy `config.example.json` to `config.json` and fill it in:
   - `public_url`: the Funnel address from step 5.
   - `access_pin`: choose a PIN. Without one, anyone who finds the address can browse your designs.
   - `email`: your Gmail as `smtp_user` and `from`, the app password as `smtp_password`, the printer's address
     under `to`, and yourself under `cc`.
   Keep `config.json` private. It holds the password.
8. **Restart the app** (Ctrl+C, then `python3 server.py`).
9. **Test it on a phone using mobile data**, not Wi-Fi. Open the address, enter the PIN, then run a job with
   your own address as the printer in `to`. Check that the email arrives and the TIFF link downloads.
10. **Keep the Mac awake.** System Settings, then Battery or Energy, then prevent automatic sleeping when the
    display is off. Or start the app with `caffeinate -s python3 server.py`.
11. **Add the sales team.** Send them the address. On a phone: Share, then *Add to Home Screen* (iPhone), or
    menu, then *Install app* (Android). Everyone uses the same PIN for now.

### Limits of this stage
- If the Mac sleeps, loses its connection or the Terminal windows are closed, the app is offline.
- Tailscale applies bandwidth limits to Funnel that you cannot change. A TIFF of several hundred MB is the
  heaviest thing that will go through it, and it also has to leave through your home internet's upload speed.
  Try one real big job before you rely on it. If it is too slow, ask your printer whether they can take the file
  from a shared Drive folder instead, while you move to stage 2.
- Backup plan if Funnel does not suit you: Cloudflare's free quick tunnel (`cloudflared tunnel --url
  http://localhost:8080`) works the same way, but Cloudflare says it is for testing only and the address changes
  every time you restart it, so `public_url` must be updated each time.

## Stage 2: a proper always-on server (not free, roughly a few dollars a month)

Do this when the sales team uses it daily. A small cloud server (a Linux virtual machine from a provider such
as Hetzner or DigitalOcean) runs the app 24/7 on a fast connection, so the Mac can sleep and printer downloads
are quick. Two things change:

1. **Masters need to live on the server or in cloud storage.** A library of 1000+ print-ready files can be
   hundreds of GB, which decides the cost. Right-click your artwork folder in Finder, choose *Get Info* and tell
   me the total size. The server then needs a disk that big, or the masters sit in cloud storage (for example
   Cloudflare R2 or Backblaze B2) and the app fetches each one when a job runs. The second needs a small code
   change, which I can do.
2. **Per-person sign-in** (each salesperson with their own login) instead of one shared PIN. This also gives
   you the "who made this job" record automatically.

Free cloud plans (Render, Railway, Fly and similar) are not suitable. They either sleep when idle or give too
little disk for the library. Oracle Cloud's "Always Free" servers have bigger disks, but I have not checked their
current terms and sign-up is sometimes refused, so I would not plan around it.

I would not start stage 2 until you have run real jobs through stage 1 for a week or two. That shows which
parts of the workflow need changing first.
